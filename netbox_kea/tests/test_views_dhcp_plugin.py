# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the "Sync to DHCP plugin" views (tab, sync-now action, drift).

Helper/guard tests run anywhere; the end-to-end import-through-the-view tests are
gated on ``netbox_dhcp`` being installed.  Only the Kea HTTP transport is stubbed
(``stub_kea``), never the ``KeaClient`` itself, the ORM, or the DHCP-plugin models.
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from django.apps import apps
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from netbox_kea.kea import KeaClient, KeaException
from netbox_kea.views import dhcp_plugin_sync as dps

from .kea_stub import _res_page, _subnet_list, stub_kea
from .utils import _make_db_server

DHCP_PLUGIN = "netbox_dhcp"
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


def _request_version(body: dict) -> int:
    """Return the DHCP family one stubbed request targets, from its ``service``."""
    service = body.get("service") or []
    return 6 if service and service[0] == "dhcp6" else 4


def _sync_responses(
    conf_by_version: dict[int, dict],
    hosts_by_version: dict[int, list] | None = None,
    *,
    reservations_available: bool = True,
) -> dict:
    """Kea responses for the DHCP-plugin sync path, answered per requested family.

    Registered by command name for :func:`stub_kea`, so the real ``KeaClient`` builds
    the requests and validates every response envelope. ``config-get`` and
    ``reservation-get-page`` are callables because one stub must answer both families,
    which a Control Agent distinguishes by ``service``.
    """
    hosts = hosts_by_version or {}

    def config_get(body):
        version = _request_version(body)
        return {"result": 0, "arguments": {f"Dhcp{version}": conf_by_version.get(version, {})}}

    def reservation_get_page(body):
        if not reservations_available:
            # host_cmds not loaded: the real client turns result 2 into a KeaException.
            return {"result": 2, "text": "command not supported"}
        return _res_page(hosts.get(_request_version(body), []))

    responses: dict = {"config-get": config_get, "reservation-get-page": reservation_get_page}
    for version, conf in conf_by_version.items():
        responses[f"subnet{version}-list"] = _subnet_list(version, conf.get(f"subnet{version}", []))
    return responses


class ExtractDhcpConfTest(SimpleTestCase):
    """`_extract_dhcp_conf` pulls the right block and *raises* on malformed shapes."""

    def test_extracts_dhcp4_block(self):
        resp = [{"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}]
        self.assertEqual(dps._extract_dhcp_conf(resp, 4), {"subnet4": []})

    def test_wrong_result_code_returns_none(self):
        # A non-zero Kea result is a legitimate "no config", not a contract failure.
        self.assertIsNone(dps._extract_dhcp_conf([{"result": 1, "arguments": {"Dhcp4": {}}}], 4))

    def test_missing_block_returns_none(self):
        # The version's block simply being absent is legitimate "no config".
        self.assertIsNone(dps._extract_dhcp_conf([{"result": 0, "arguments": {}}], 6))

    def test_non_list_raises(self):
        # Malformed *shape* must surface, not be silently downgraded to "no config".
        with self.assertRaises(RuntimeError):
            dps._extract_dhcp_conf({"not": "a list"}, 4)

    def test_empty_list_raises(self):
        with self.assertRaises(RuntimeError):
            dps._extract_dhcp_conf([], 4)

    def test_non_dict_item_raises(self):
        with self.assertRaises(RuntimeError):
            dps._extract_dhcp_conf(["not-a-dict"], 4)

    def test_non_dict_arguments_raises(self):
        with self.assertRaises(RuntimeError):
            dps._extract_dhcp_conf([{"result": 0, "arguments": ["not", "a", "dict"]}], 4)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TabVisibilityTest(TestCase):
    """The DHCP-plugin tab hides itself unless the plugin is installed AND opted in."""

    def test_tab_hidden_when_plugin_unavailable(self):
        server = _make_db_server(sync_dhcp_plugin_enabled=True)
        with patch.object(dps.dhcp_plugin, "is_available", return_value=False, autospec=True):
            self.assertFalse(dps._tab_enabled(server))

    def test_tab_hidden_when_not_opted_in(self):
        server = _make_db_server(sync_dhcp_plugin_enabled=False)
        with patch.object(dps.dhcp_plugin, "is_available", return_value=True, autospec=True):
            self.assertFalse(dps._tab_enabled(server))

    def test_tab_shown_when_available_and_opted_in(self):
        server = _make_db_server(sync_dhcp_plugin_enabled=True)
        with patch.object(dps.dhcp_plugin, "is_available", return_value=True, autospec=True):
            self.assertTrue(dps._tab_enabled(server))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class SyncNowGuardTest(TestCase):
    """The sync-now action refuses when the plugin is absent or not opted in."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_superuser("dps-user", "dps@example.com", "pw")
        self.client.force_login(self.user)
        self.server = _make_db_server(sync_dhcp_plugin_enabled=True)
        self.url = reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[self.server.pk])

    def test_refuses_when_plugin_unavailable(self):
        with patch.object(dps.dhcp_plugin, "is_available", return_value=False, autospec=True):
            resp = self.client.post(self.url, follow=True)
        self.assertContains(resp, "not installed")

    def test_refuses_when_not_opted_in(self):
        self.server.sync_dhcp_plugin_enabled = False
        self.server.save(update_fields=["sync_dhcp_plugin_enabled"])
        with patch.object(dps.dhcp_plugin, "is_available", return_value=True, autospec=True):
            resp = self.client.post(self.url, follow=True)
        self.assertContains(resp, "Enable &#x27;Sync to DHCP plugin&#x27;")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class SyncNowEndToEndTest(TestCase):
    """Full path: POST sync-now → real KeaClient over a stubbed transport → real rows."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise unittest.SkipTest(f"{DHCP_PLUGIN} not installed")
        super().setUpClass()

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_superuser("dps-e2e", "e2e@example.com", "pw")
        self.client.force_login(self.user)
        self.server = _make_db_server(
            name=f"kea-dps-{timezone.now().timestamp()}",
            sync_dhcp_plugin_enabled=True,
            dhcp4=True,
            dhcp6=False,
        )
        self.url = reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[self.server.pk])

    def test_post_imports_and_reports(self):
        conf = {
            4: {
                "subnet4": [
                    {
                        "id": 1,
                        "subnet": "10.88.0.0/24",
                        "pools": [{"pool": "10.88.0.10-10.88.0.99"}],
                        "option-data": [{"code": 3, "name": "routers", "data": "10.88.0.1", "space": "dhcp4"}],
                    },
                ]
            }
        }
        with stub_kea(_sync_responses(conf)):
            resp = self.client.post(self.url, follow=True)

        self.assertContains(resp, "1 subnets created")
        self.assertContains(resp, "1 options created")
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        self.assertTrue(Subnet.objects.filter(prefix__prefix="10.88.0.0/24").exists())
        Option = apps.get_model(DHCP_PLUGIN, "Option")
        subnet = Subnet.objects.get(prefix__prefix="10.88.0.0/24")
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(Subnet)
        self.assertTrue(Option.objects.filter(assigned_object_type=ct, assigned_object_id=subnet.pk).exists())

    def test_post_imports_db_backed_reservations(self):
        # Subnet is in config-get; the reservation lives ONLY in the hosts DB
        # (reservation-get-page) — the case config-get-only import was missing.
        conf = {4: {"subnet4": [{"id": 1, "subnet": "10.89.0.0/24"}]}}
        hosts = {
            4: [{"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:89", "ip-address": "10.89.0.50", "hostname": "db-res"}]
        }
        with stub_kea(_sync_responses(conf, hosts)):
            resp = self.client.post(self.url, follow=True)

        self.assertContains(resp, "1 reservations created")
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        subnet = Subnet.objects.get(prefix__prefix="10.89.0.0/24")
        self.assertTrue(HostReservation.objects.filter(subnet=subnet, hostname="db-res").exists())

    def test_post_warns_when_reservations_unreadable(self):
        # host_cmds absent means that the typed Snapshot cannot be read.
        conf = {4: {"subnet4": [{"id": 1, "subnet": "10.90.0.0/24"}]}}
        with stub_kea(_sync_responses(conf, reservations_available=False)):
            resp = self.client.post(self.url, follow=True)
        self.assertContains(resp, "could not be read")

    def test_drift_view_renders_imported_status(self):
        self.server.dhcp6 = True
        self.server.save(update_fields=["dhcp6"])
        conf = {
            4: {"subnet4": [{"id": 1, "subnet": "10.88.0.0/24"}]},
            6: {"subnet6": [{"id": 1, "subnet": "2001:db8:88::/64"}]},
        }
        with stub_kea(_sync_responses(conf)):
            self.client.post(self.url, follow=True)
            tab_url = reverse("plugins:netbox_kea:server_dhcp_plugin", args=[self.server.pk])
            resp = self.client.get(tab_url)
        self.assertContains(resp, "Imported")
        self.assertContains(resp, "10.88.0.0/24")
        self.assertContains(resp, "2001:db8:88::/64")
        self.assertContains(resp, "DHCPv4")
        self.assertContains(resp, "DHCPv6")
        # Match the marker class on a table element, so a Bootstrap class change here
        # does not fail a test about the merged drift table.
        drift_tables = re.findall(r"<table[^>]*\bdrift-table\b[^>]*>", resp.content.decode())
        self.assertEqual(len(drift_tables), 1, drift_tables)
        self.assertContains(resp, "Shared Network grouping")
        self.assertContains(resp, "Direct-interface mapping")
        self.assertContains(resp, "DHCP option mapping")
        self.assertContains(resp, "require an aggregate Prefix")
        self.assertContains(resp, "DHCPServerInterface")
        self.assertContains(resp, "does not preserve Shared Network identity")
        self.assertContains(resp, "DHCP options are imported")
        self.assertContains(resp, "Unsupported options are skipped")
        self.assertNotContains(resp, "are not imported")


class _MalformedConfigClient(KeaClient):
    """Fake client whose ``config-get`` returns a malformed (non-list) shape."""

    def __init__(self):
        pass

    def command(self, command, service=None, arguments=None, check=(0,)):
        return {"not": "a list"}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class FetchConfigIntentTest(TestCase):
    """`_fetch_config_intent` surfaces a malformed config-get as a logged skip, not a crash."""

    def setUp(self):
        self.server = _make_db_server(dhcp4=True, dhcp6=False)

    def test_malformed_config_get_is_logged_and_skipped(self):
        with patch("netbox_kea.models.Server.get_client", return_value=_MalformedConfigClient(), autospec=True):
            with self.assertLogs("netbox_kea.views.dhcp_plugin_sync", level="WARNING") as cm:
                result = dps._fetch_config_intent(self.server, 4)
        # Malformed shape → RuntimeError raised in _extract_dhcp_conf, caught here as a
        # read failure: the version is skipped (None) and the problem is logged, not
        # silently downgraded to "no config".
        self.assertIsNone(result)
        self.assertTrue(any("config-get failed" in line for line in cm.output))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class SyncNowErrorHandlingTest(TestCase):
    """The sync action follows the exception contract and never leaks a traceback."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_superuser("dps-err", "err@example.com", "pw")
        self.client.force_login(self.user)
        self.server = _make_db_server(sync_dhcp_plugin_enabled=True, dhcp4=True, dhcp6=False)
        self.url = reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[self.server.pk])

    def test_kea_exception_routes_through_specific_handler(self):
        # A KeaException (or its PartialPersistError subclass) is handled by the
        # dedicated contract branch, logged distinctly, and shown as a generic error.
        with (
            patch.object(dps.dhcp_plugin, "is_available", return_value=True, autospec=True),
            patch.object(
                dps, "run_dhcp_plugin_import", side_effect=KeaException({"result": 1, "text": "boom"}), autospec=True
            ),
        ):
            with self.assertLogs("netbox_kea.views.dhcp_plugin_sync", level="ERROR") as cm:
                resp = self.client.post(self.url, follow=True)
        self.assertContains(resp, "An internal error occurred")
        self.assertTrue(any("Kea read/validation" in line for line in cm.output))

    def test_unexpected_exception_is_caught_not_leaked(self):
        # A non-contract error (e.g. a DB error) still hits the fallback: logged,
        # never surfaced as a 500 traceback.
        with (
            patch.object(dps.dhcp_plugin, "is_available", return_value=True, autospec=True),
            patch.object(dps, "run_dhcp_plugin_import", side_effect=RuntimeError("db gone"), autospec=True),
        ):
            resp = self.client.post(self.url, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "An internal error occurred")


class SummaryProblemsTest(SimpleTestCase):
    """Every non-zero problem count must be reported, not only the first one.

    The view used an ``if/elif/elif`` chain, so an unread Snapshot hid the
    quarantine and error counts and the operator saw an incomplete picture.
    """

    def _summary(self, **counts):
        from netbox_kea.integrations.dhcp_plugin import ImportSummary

        summary = ImportSummary()
        for name, value in counts.items():
            setattr(summary, name, value)
        return summary

    def test_no_problems_yields_no_notes(self):
        self.assertEqual(dps._summary_problems(self._summary()), [])

    def test_every_non_zero_count_is_reported_together(self):
        problems = dps._summary_problems(self._summary(reservations_unread=True, reservations_quarantined=2, errors=3))

        self.assertEqual(len(problems), 3)
        joined = " ".join(problems)
        self.assertIn("host_cmds", joined)
        self.assertIn("2 malformed reservation(s) were quarantined.", joined)
        self.assertIn("3 errors occurred.", joined)

    def test_quarantine_count_is_reported_alongside_an_unread_snapshot(self):
        problems = dps._summary_problems(self._summary(reservations_unread=True, reservations_quarantined=5))

        self.assertEqual(len(problems), 2)
        self.assertIn("5 malformed reservation(s) were quarantined.", " ".join(problems))

    def test_error_count_is_reported_alongside_quarantined_reservations(self):
        problems = dps._summary_problems(self._summary(reservations_quarantined=1, errors=4))

        self.assertEqual(len(problems), 2)
        self.assertIn("4 errors occurred.", " ".join(problems))
