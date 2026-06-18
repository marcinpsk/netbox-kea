# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the "Sync to DHCP plugin" views (tab, sync-now action, drift).

Helper/guard tests run anywhere; the end-to-end import-through-the-view tests are
gated on ``netbox_dhcp`` being installed.  Only the Kea HTTP boundary is faked
(a small fake client), never the ORM or the DHCP-plugin models.
"""

from __future__ import annotations

from unittest.mock import patch

from django.apps import apps
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from netbox_kea.kea import KeaClient, KeaException
from netbox_kea.views import dhcp_plugin_sync as dps

from .utils import _make_db_server

DHCP_PLUGIN = "netbox_dhcp"
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


class _FakeKeaClient(KeaClient):
    """Stand-in for KeaClient that answers ``config-get`` and ``reservation-get-page``.

    Subclasses the real client (and skips its session setup) so the genuine
    ``reservation_get_page``/``iter_reservations`` pagination logic runs against the
    faked ``command`` — only the Kea HTTP boundary is replaced, never the ORM.
    """

    def __init__(
        self,
        conf_by_version: dict[int, dict],
        hosts_by_version: dict[int, list] | None = None,
        reservations_available: bool = True,
    ):
        self._conf = conf_by_version
        self._hosts = hosts_by_version or {}
        self._reservations_available = reservations_available

    def command(self, command, service=None, arguments=None, check=(0,)):
        version = 6 if service and service[0] == "dhcp6" else 4
        if command == "config-get":
            return [{"result": 0, "arguments": {f"Dhcp{version}": self._conf.get(version, {})}}]
        if command == "reservation-get-page":
            if not self._reservations_available:
                # Simulate host_cmds not loaded (Kea result code 2).
                raise KeaException({"result": 2, "text": "command not supported", "arguments": None})
            hosts = self._hosts.get(version, [])
            return [{"result": 0, "arguments": {"hosts": hosts, "next": {"from": 0, "source-index": 0}}}]
        return [{"result": 0, "arguments": {}}]


class ExtractDhcpConfTest(SimpleTestCase):
    """`_extract_dhcp_conf` pulls the right block and rejects malformed shapes."""

    def test_extracts_dhcp4_block(self):
        resp = [{"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}]
        self.assertEqual(dps._extract_dhcp_conf(resp, 4), {"subnet4": []})

    def test_wrong_result_code_returns_none(self):
        self.assertIsNone(dps._extract_dhcp_conf([{"result": 1, "arguments": {"Dhcp4": {}}}], 4))

    def test_non_list_returns_none(self):
        self.assertIsNone(dps._extract_dhcp_conf({"not": "a list"}, 4))

    def test_missing_block_returns_none(self):
        self.assertIsNone(dps._extract_dhcp_conf([{"result": 0, "arguments": {}}], 6))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TabVisibilityTest(TestCase):
    """The DHCP-plugin tab hides itself unless the plugin is installed AND opted in."""

    def test_tab_hidden_when_plugin_unavailable(self):
        server = _make_db_server(sync_dhcp_plugin_enabled=True)
        with patch.object(dps.dhcp_plugin, "is_available", return_value=False):
            self.assertFalse(dps._tab_enabled(server))

    def test_tab_hidden_when_not_opted_in(self):
        server = _make_db_server(sync_dhcp_plugin_enabled=False)
        with patch.object(dps.dhcp_plugin, "is_available", return_value=True):
            self.assertFalse(dps._tab_enabled(server))

    def test_tab_shown_when_available_and_opted_in(self):
        server = _make_db_server(sync_dhcp_plugin_enabled=True)
        with patch.object(dps.dhcp_plugin, "is_available", return_value=True):
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
        with patch.object(dps.dhcp_plugin, "is_available", return_value=False):
            resp = self.client.post(self.url, follow=True)
        self.assertContains(resp, "not installed")

    def test_refuses_when_not_opted_in(self):
        self.server.sync_dhcp_plugin_enabled = False
        self.server.save(update_fields=["sync_dhcp_plugin_enabled"])
        with patch.object(dps.dhcp_plugin, "is_available", return_value=True):
            resp = self.client.post(self.url, follow=True)
        self.assertContains(resp, "Enable &#x27;Sync to DHCP plugin&#x27;")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class SyncNowEndToEndTest(TestCase):
    """Full path: POST sync-now → read (faked) Kea config → real DHCP-plugin rows."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise cls.skipException(f"{DHCP_PLUGIN} not installed")
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
        fake = _FakeKeaClient(conf)
        with patch("netbox_kea.models.Server.get_client", return_value=fake):
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
        fake = _FakeKeaClient(conf, hosts)
        with patch("netbox_kea.models.Server.get_client", return_value=fake):
            resp = self.client.post(self.url, follow=True)

        self.assertContains(resp, "1 reservations created")
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        subnet = Subnet.objects.get(prefix__prefix="10.89.0.0/24")
        self.assertTrue(HostReservation.objects.filter(subnet=subnet, hostname="db-res").exists())

    def test_post_warns_when_reservations_unreadable(self):
        # Finding 4: host_cmds absent → the import must say reservations couldn't be read.
        conf = {4: {"subnet4": [{"id": 1, "subnet": "10.90.0.0/24"}]}}
        fake = _FakeKeaClient(conf, reservations_available=False)
        with patch("netbox_kea.models.Server.get_client", return_value=fake):
            resp = self.client.post(self.url, follow=True)
        self.assertContains(resp, "could not be read")

    def test_drift_view_renders_imported_status(self):
        conf = {4: {"subnet4": [{"id": 1, "subnet": "10.88.0.0/24"}]}}
        fake = _FakeKeaClient(conf)
        with patch("netbox_kea.models.Server.get_client", return_value=fake):
            self.client.post(self.url, follow=True)
            tab_url = reverse("plugins:netbox_kea:server_dhcp_plugin", args=[self.server.pk])
            resp = self.client.get(tab_url)
        self.assertContains(resp, "Imported")
        self.assertContains(resp, "10.88.0.0/24")
        # The tab warns that Kea shared-networks are a different concept (not imported as such).
        self.assertContains(resp, "different concept")
