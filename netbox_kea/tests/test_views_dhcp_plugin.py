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

from netbox_kea.views import dhcp_plugin_sync as dps

from .utils import _make_db_server

DHCP_PLUGIN = "netbox_dhcp"
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


class _FakeKeaClient:
    """Minimal stand-in for KeaClient that answers ``config-get`` only."""

    def __init__(self, conf_by_version: dict[int, dict]):
        self._conf = conf_by_version

    def command(self, cmd, service=None, arguments=None):
        if cmd == "config-get":
            version = 6 if service and service[0] == "dhcp6" else 4
            return [{"result": 0, "arguments": {f"Dhcp{version}": self._conf.get(version, {})}}]
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
                    {"id": 1, "subnet": "10.88.0.0/24", "pools": [{"pool": "10.88.0.10-10.88.0.99"}]},
                ]
            }
        }
        fake = _FakeKeaClient(conf)
        with patch("netbox_kea.models.Server.get_client", return_value=fake):
            resp = self.client.post(self.url, follow=True)

        self.assertContains(resp, "1 subnets created")
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        self.assertTrue(Subnet.objects.filter(prefix__prefix="10.88.0.0/24").exists())

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
