# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for Phase 6: unified combined multi-server views.

These views aggregate data (leases, reservations, subnets) across multiple Kea
servers so that operators can see everything in one place.

URL map under test
------------------
GET /plugins/kea/combined/                 → CombinedDashboardView
GET /plugins/kea/combined/leases4/         → CombinedLeases4View
GET /plugins/kea/combined/leases6/         → CombinedLeases6View
GET /plugins/kea/combined/subnets4/        → CombinedSubnets4View
GET /plugins/kea/combined/subnets6/        → CombinedSubnets6View
GET /plugins/kea/combined/reservations4/   → CombinedReservations4View
GET /plugins/kea/combined/reservations6/   → CombinedReservations6View

All views:
- Extend _CombinedViewMixin which injects all_servers, selected_server_pks, server_qs, active_tab
- Use concurrent.futures.ThreadPoolExecutor for parallel fetching
- Gracefully handle unreachable servers (warning, not 500)
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea import constants
from netbox_kea.models import Server

User = get_user_model()
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

# ---------------------------------------------------------------------------
# Mock Kea response fixtures
# ---------------------------------------------------------------------------

_MOCK_RESERVATION_V4 = {
    "subnet-id": 1,
    "subnet_id": 1,
    "hw-address": "aa:bb:cc:dd:ee:ff",
    "ip-address": "10.0.0.100",
    "ip_address": "10.0.0.100",
    "hostname": "host-v4",
}

_MOCK_RESERVATION_V6 = {
    "subnet-id": 1,
    "subnet_id": 1,
    "duid": "00:01:aa:bb",
    "ip-addresses": ["2001:db8::1"],
    "hostname": "host-v6",
}

_MOCK_LEASE_V4 = {
    "ip-address": "10.0.0.1",
    "hw-address": "aa:bb:cc:dd:ee:ff",
    "hostname": "lease-host-v4",
    "subnet-id": 1,
    "valid-lft": 3600,
    "cltt": 1_234_567_890,
}

_MOCK_LEASE_V6 = {
    "ip-address": "2001:db8::1",
    "duid": "00:01:aa:bb",
    "hostname": "lease-host-v6",
    "subnet-id": 1,
    "valid-lft": 3600,
    "cltt": 1_234_567_890,
    "preferred-lft": 1800,
}

_MOCK_CONFIG_V4 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}],
                "shared-networks": [],
            }
        },
    }
]

_MOCK_CONFIG_V6 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp6": {
                "subnet6": [{"id": 1, "subnet": "2001:db8::/32"}],
                "shared-networks": [],
            }
        },
    }
]


def _make_server(**kwargs) -> Server:
    """Create a Server without triggering live connectivity checks."""
    defaults = {
        "name": "global-test",
        "server_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": False,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _CombinedViewBase(TestCase):
    """Create superuser + one v4-only, one v6-only, one dual-stack server."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="global_testuser",
            email="global@test.com",
            password="testpass",
        )
        self.client.force_login(self.user)
        self.v4_server = _make_server(name="v4-server", dhcp4=True, dhcp6=False)
        self.v6_server = _make_server(name="v6-server", dhcp4=False, dhcp6=True)
        self.dual_server = _make_server(name="dual-server", dhcp4=True, dhcp6=True)


# ---------------------------------------------------------------------------
# CombinedDashboardView  GET /plugins/kea/combined/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedDashboardView(_CombinedViewBase):
    """GET /plugins/kea/combined/ — server overview with tab navigation."""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:combined")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_lists_all_servers_by_name(self):
        url = reverse("plugins:netbox_kea:combined")
        response = self.client.get(url)
        self.assertContains(response, "v4-server")
        self.assertContains(response, "v6-server")
        self.assertContains(response, "dual-server")

    def test_no_kea_api_calls_on_dashboard(self):
        """Dashboard must not call Kea API (would slow down page for many servers)."""
        with patch("netbox_kea.models.KeaClient") as MockKeaClient:
            url = reverse("plugins:netbox_kea:combined")
            self.client.get(url)
        MockKeaClient.assert_not_called()

    def test_context_contains_all_servers(self):
        url = reverse("plugins:netbox_kea:combined")
        response = self.client.get(url)
        self.assertIn("all_servers", response.context)
        self.assertEqual(len(response.context["all_servers"]), 3)

    def test_context_active_tab_is_overview(self):
        url = reverse("plugins:netbox_kea:combined")
        response = self.client.get(url)
        self.assertEqual(response.context["active_tab"], "overview")

    def test_tab_navigation_links_present(self):
        """The combined base template should render links to all 7 tabs."""
        url = reverse("plugins:netbox_kea:combined")
        response = self.client.get(url)
        self.assertContains(response, "/combined/leases4/")
        self.assertContains(response, "/combined/leases6/")
        self.assertContains(response, "/combined/subnets4/")
        self.assertContains(response, "/combined/subnets6/")
        self.assertContains(response, "/combined/reservations4/")
        self.assertContains(response, "/combined/reservations6/")


# ---------------------------------------------------------------------------
# CombinedReservations4View  GET /plugins/kea/combined/reservations4/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservations4View(_CombinedViewBase):
    """GET /plugins/kea/combined/reservations4/ — all DHCPv4 reservations."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:combined_reservations4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_reservations4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_queries_all_v4_servers(self, MockKeaClient):
        """Without a server filter, every dhcp4-enabled server is queried."""
        MockKeaClient.return_value.reservation_get_page.return_value = (
            [dict(_MOCK_RESERVATION_V4)],
            0,
            0,
        )
        url = reverse("plugins:netbox_kea:combined_reservations4")
        self.client.get(url)
        # v4_server + dual_server have dhcp4=True → at least 2 calls
        self.assertGreaterEqual(MockKeaClient.return_value.reservation_get_page.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_server_filter_limits_queried_servers(self, MockKeaClient):
        """?server=<pk> filters down to exactly that one server."""
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}"
        self.client.get(url)
        self.assertEqual(MockKeaClient.return_value.reservation_get_page.call_count, 1)

    @patch("netbox_kea.models.KeaClient")
    def test_results_include_server_name(self, MockKeaClient):
        """Each row in the merged table must carry the originating server name."""
        rec = dict(_MOCK_RESERVATION_V4)
        MockKeaClient.return_value.reservation_get_page.return_value = ([rec], 0, 0)
        url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v4-server")

    @patch("netbox_kea.models.KeaClient")
    def test_unreachable_server_returns_200_with_warning(self, MockKeaClient):
        """A server that raises an exception must not cause a 500."""
        MockKeaClient.return_value.reservation_get_page.side_effect = Exception("refused")
        url = reverse("plugins:netbox_kea:combined_reservations4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_no_v4_servers_returns_200_empty_table(self, MockKeaClient):
        """If no dhcp4-enabled servers exist, the view renders with an empty table."""
        Server.objects.filter(dhcp4=True).update(dhcp4=False)
        url = reverse("plugins:netbox_kea:combined_reservations4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        MockKeaClient.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_context_active_tab(self, MockKeaClient):
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:combined_reservations4")
        response = self.client.get(url)
        self.assertEqual(response.context["active_tab"], "reservations4")


# ---------------------------------------------------------------------------
# CombinedReservations6View  GET /plugins/kea/combined/reservations6/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservations6View(_CombinedViewBase):
    """GET /plugins/kea/combined/reservations6/ — all DHCPv6 reservations."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:combined_reservations6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_reservations6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_queries_all_v6_servers(self, MockKeaClient):
        MockKeaClient.return_value.reservation_get_page.return_value = (
            [dict(_MOCK_RESERVATION_V6)],
            0,
            0,
        )
        url = reverse("plugins:netbox_kea:combined_reservations6")
        self.client.get(url)
        self.assertGreaterEqual(MockKeaClient.return_value.reservation_get_page.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_server_filter_limits_queried_servers(self, MockKeaClient):
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:combined_reservations6") + f"?server={self.v6_server.pk}"
        self.client.get(url)
        self.assertEqual(MockKeaClient.return_value.reservation_get_page.call_count, 1)

    @patch("netbox_kea.models.KeaClient")
    def test_unreachable_server_returns_200_with_warning(self, MockKeaClient):
        MockKeaClient.return_value.reservation_get_page.side_effect = Exception("refused")
        url = reverse("plugins:netbox_kea:combined_reservations6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# CombinedLeases4View  GET /plugins/kea/combined/leases4/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeases4View(_CombinedViewBase):
    """GET /plugins/kea/combined/leases4/ — DHCPv4 leases with cross-server search."""

    def test_get_without_search_returns_200(self):
        url = reverse("plugins:netbox_kea:combined_leases4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_leases4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_no_kea_calls_without_search_query(self):
        with patch("netbox_kea.models.KeaClient") as MockKeaClient:
            url = reverse("plugins:netbox_kea:combined_leases4")
            self.client.get(url)
        MockKeaClient.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_search_broadcasts_to_all_v4_servers(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"leases": [dict(_MOCK_LEASE_V4)]}}
        ]

        url = reverse("plugins:netbox_kea:combined_leases4") + f"?q=lease-host-v4&by={constants.BY_HOSTNAME}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(MockKeaClient.return_value.command.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_search_with_server_filter_limits_calls(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": []}}]

        url = (
            reverse("plugins:netbox_kea:combined_leases4")
            + f"?server={self.v4_server.pk}&q=test&by={constants.BY_HOSTNAME}"
        )
        self.client.get(url)
        self.assertEqual(MockKeaClient.return_value.command.call_count, 1)

    @patch("netbox_kea.models.KeaClient")
    def test_unreachable_server_returns_200_with_warning(self, MockKeaClient):
        MockKeaClient.return_value.command.side_effect = Exception("refused")

        url = reverse("plugins:netbox_kea:combined_leases4") + f"?q=test&by={constants.BY_HOSTNAME}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_merged_results_include_server_name(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"leases": [dict(_MOCK_LEASE_V4)]}}
        ]

        url = (
            reverse("plugins:netbox_kea:combined_leases4")
            + f"?server={self.v4_server.pk}&q=test&by={constants.BY_HOSTNAME}"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v4-server")

    @patch("netbox_kea.models.KeaClient")
    def test_context_active_tab(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": []}}]

        url = reverse("plugins:netbox_kea:combined_leases4") + f"?q=x&by={constants.BY_HOSTNAME}"
        response = self.client.get(url)
        self.assertEqual(response.context["active_tab"], "leases4")


# ---------------------------------------------------------------------------
# CombinedLeases6View  GET /plugins/kea/combined/leases6/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeases6View(_CombinedViewBase):
    """GET /plugins/kea/combined/leases6/ — DHCPv6 leases with cross-server search."""

    def test_get_without_search_returns_200(self):
        url = reverse("plugins:netbox_kea:combined_leases6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_leases6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_no_kea_calls_without_search_query(self):
        with patch("netbox_kea.models.KeaClient") as MockKeaClient:
            url = reverse("plugins:netbox_kea:combined_leases6")
            self.client.get(url)
        MockKeaClient.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_search_broadcasts_to_all_v6_servers(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"leases": [dict(_MOCK_LEASE_V6)]}}
        ]

        url = reverse("plugins:netbox_kea:combined_leases6") + f"?q=lease-host-v6&by={constants.BY_HOSTNAME}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(MockKeaClient.return_value.command.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_unreachable_server_returns_200_with_warning(self, MockKeaClient):
        MockKeaClient.return_value.command.side_effect = Exception("refused")

        url = reverse("plugins:netbox_kea:combined_leases6") + f"?q=test&by={constants.BY_HOSTNAME}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_merged_results_include_server_name(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"leases": [dict(_MOCK_LEASE_V6)]}}
        ]

        url = (
            reverse("plugins:netbox_kea:combined_leases6")
            + f"?server={self.v6_server.pk}&q=test&by={constants.BY_HOSTNAME}"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v6-server")


# ---------------------------------------------------------------------------
# CombinedSubnets4View  GET /plugins/kea/combined/subnets4/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedSubnets4View(_CombinedViewBase):
    """GET /plugins/kea/combined/subnets4/ — DHCPv4 subnets across all servers."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = _MOCK_CONFIG_V4
        url = reverse("plugins:netbox_kea:combined_subnets4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_subnets4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_queries_all_v4_servers(self, MockKeaClient):
        """Without a filter, every dhcp4-enabled server is queried for config-get."""
        MockKeaClient.return_value.command.return_value = _MOCK_CONFIG_V4
        url = reverse("plugins:netbox_kea:combined_subnets4")
        self.client.get(url)
        # v4_server + dual_server → 2 calls
        self.assertGreaterEqual(MockKeaClient.return_value.command.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_server_filter_limits_queried_servers(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = _MOCK_CONFIG_V4
        url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.v4_server.pk}"
        self.client.get(url)
        # 1 server filtered × 2 commands (config-get + stat-lease4-get) = 2
        self.assertEqual(MockKeaClient.return_value.command.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_unreachable_server_returns_200_with_warning(self, MockKeaClient):
        MockKeaClient.return_value.command.side_effect = Exception("refused")
        url = reverse("plugins:netbox_kea:combined_subnets4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_results_include_server_name(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = _MOCK_CONFIG_V4
        url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.v4_server.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v4-server")

    @patch("netbox_kea.models.KeaClient")
    def test_context_active_tab(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = _MOCK_CONFIG_V4
        url = reverse("plugins:netbox_kea:combined_subnets4")
        response = self.client.get(url)
        self.assertEqual(response.context["active_tab"], "subnets4")


# ---------------------------------------------------------------------------
# CombinedSubnets6View  GET /plugins/kea/combined/subnets6/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedSubnets6View(_CombinedViewBase):
    """GET /plugins/kea/combined/subnets6/ — DHCPv6 subnets across all servers."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = _MOCK_CONFIG_V6
        url = reverse("plugins:netbox_kea:combined_subnets6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_subnets6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_queries_all_v6_servers(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = _MOCK_CONFIG_V6
        url = reverse("plugins:netbox_kea:combined_subnets6")
        self.client.get(url)
        self.assertGreaterEqual(MockKeaClient.return_value.command.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_unreachable_server_returns_200_with_warning(self, MockKeaClient):
        MockKeaClient.return_value.command.side_effect = Exception("refused")
        url = reverse("plugins:netbox_kea:combined_subnets6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Badge enrichment parity: combined views must match per-server views
# ---------------------------------------------------------------------------


_MOCK_LEASE_V4_ENRICHMENT = {
    "ip-address": "10.20.0.5",
    "hw-address": "aa:bb:cc:dd:ee:01",
    "subnet-id": 1,
    "hostname": "enriched-host",
    "valid-lft": 3600,
    "cltt": 0,
    "state": 0,
}

_MOCK_RESERVATION_ENRICHED = {
    "subnet-id": 1,
    "hw-address": "aa:bb:cc:dd:ee:01",
    "ip-address": "10.20.0.5",
    "hostname": "enriched-host",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeases4Enrichment(_CombinedViewBase):
    """Combined DHCPv4 lease view must include the same badge enrichment as per-server view."""

    def _lease_url(self, q="10.20.0.5", by="ip"):
        return reverse("plugins:netbox_kea:combined_leases4") + f"?q={q}&by={by}&server={self.v4_server.pk}"

    def _mock_command(self, mock_client, leases, reservations=()):
        """Configure client mock: lease command + reservation_get_page."""

        def command_side_effect(cmd, **kwargs):
            if "lease" in cmd:
                args = kwargs.get("arguments", {})
                ip = args.get("ip-address")
                if ip:
                    matching = [entry for entry in leases if entry["ip-address"] == ip]
                    if not matching:
                        return [{"result": 3, "arguments": None}]
                    return [{"result": 0, "arguments": matching[0]}]
                return [{"result": 0, "arguments": {"leases": list(leases), "count": len(leases)}}]
            return [{"result": 2, "arguments": {}}]

        mock_client.command.side_effect = command_side_effect
        mock_client.reservation_get_page.return_value = (list(reservations), 0, 0)

    @patch("netbox_kea.models.KeaClient")
    def test_reserved_badge_appears_when_reservation_exists(self, MockKeaClient):
        """A lease with a matching reservation must show the 'Reserved' badge."""
        self._mock_command(
            MockKeaClient.return_value,
            leases=[dict(_MOCK_LEASE_V4_ENRICHMENT)],
            reservations=[dict(_MOCK_RESERVATION_ENRICHED)],
        )
        response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserved")

    @patch("netbox_kea.models.KeaClient")
    def test_create_reservation_link_when_no_reservation(self, MockKeaClient):
        """A lease without a matching reservation must show a create-reservation link."""
        self._mock_command(
            MockKeaClient.return_value,
            leases=[dict(_MOCK_LEASE_V4_ENRICHMENT)],
            reservations=[],
        )
        response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reservations4/add")

    @patch("netbox_kea.models.KeaClient")
    def test_netbox_ip_synced_link_when_ip_in_netbox(self, MockKeaClient):
        """When the lease IP exists in NetBox IPAM, a 'Synced' link must appear."""
        from ipam.models import IPAddress

        IPAddress.objects.create(address="10.20.0.5/32")
        self._mock_command(
            MockKeaClient.return_value,
            leases=[dict(_MOCK_LEASE_V4_ENRICHMENT)],
            reservations=[],
        )
        response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced")

    @patch("netbox_kea.models.KeaClient")
    def test_netbox_ip_sync_button_when_ip_not_in_netbox(self, MockKeaClient):
        """When the lease IP is not in NetBox IPAM, a 'Sync' button must appear."""
        self._mock_command(
            MockKeaClient.return_value,
            leases=[dict(_MOCK_LEASE_V4_ENRICHMENT)],
            reservations=[],
        )
        response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sync")

    @patch("netbox_kea.models.KeaClient")
    def test_combined_default_columns_include_reserved_and_netbox_ip(self, MockKeaClient):
        """GlobalLeaseTable4 default columns must include reserved and netbox_ip."""
        self._mock_command(MockKeaClient.return_value, leases=[], reservations=[])
        response = self.client.get(self._lease_url(q="nomatch"))
        self.assertEqual(response.status_code, 200)
        # Column headers should appear
        self.assertContains(response, "NetBox IP")
        self.assertContains(response, "Reserved")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservations4Enrichment(_CombinedViewBase):
    """Combined DHCPv4 reservation view must include the same badge enrichment as per-server."""

    def _url(self):
        return reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}"

    @patch("netbox_kea.models.KeaClient")
    def test_active_lease_badge_when_lease_exists(self, MockKeaClient):
        """A reservation with an active lease must show 'Active Lease' badge."""
        MockKeaClient.return_value.reservation_get_page.return_value = ([dict(_MOCK_RESERVATION_ENRICHED)], 0, 0)
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"leases": [{"ip-address": "10.20.0.5"}]}}
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Lease")

    @patch("netbox_kea.models.KeaClient")
    def test_no_lease_badge_when_no_active_lease(self, MockKeaClient):
        """A reservation without active lease must show 'No Lease' badge."""
        MockKeaClient.return_value.reservation_get_page.return_value = ([dict(_MOCK_RESERVATION_ENRICHED)], 0, 0)
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Lease")

    @patch("netbox_kea.models.KeaClient")
    def test_lease_column_header_present(self, MockKeaClient):
        """GlobalReservationTable4 must render a 'Lease' column header."""
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lease")

    @patch("netbox_kea.models.KeaClient")
    def test_netbox_ip_synced_link_when_ip_in_netbox(self, MockKeaClient):
        """Reservation IP in NetBox IPAM → 'Synced' link in combined table."""
        from ipam.models import IPAddress

        IPAddress.objects.create(address="10.20.0.5/32")
        MockKeaClient.return_value.reservation_get_page.return_value = ([dict(_MOCK_RESERVATION_ENRICHED)], 0, 0)
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced")

    @patch("netbox_kea.models.KeaClient")
    def test_edit_action_links_use_server_pk(self, MockKeaClient):
        """Each combined reservation row must have an edit link pointing to the correct server."""
        MockKeaClient.return_value.reservation_get_page.return_value = ([dict(_MOCK_RESERVATION_ENRICHED)], 0, 0)
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        expected = f"/plugins/kea/servers/{self.v4_server.pk}/reservations4/"
        self.assertContains(response, expected)


# ---------------------------------------------------------------------------
# Badge enrichment parity: DHCPv6 combined views  (issue #10)
# ---------------------------------------------------------------------------

_MOCK_LEASE_V6_ENRICHMENT = {
    "ip-address": "2001:db8::5",
    "duid": "00:01:aa:bb",
    "subnet-id": 1,
    "hostname": "enriched-host-v6",
    "valid-lft": 3600,
    "cltt": 0,
    "state": 0,
    "preferred-lft": 1800,
}

_MOCK_RESERVATION_V6_ENRICHED = {
    "subnet-id": 1,
    "duid": "00:01:aa:bb",
    "ip-addresses": ["2001:db8::5"],
    "ip_address": "2001:db8::5",
    "hostname": "enriched-host-v6",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeases6Enrichment(_CombinedViewBase):
    """Combined DHCPv6 lease view must include the same badge enrichment as DHCPv4."""

    def _lease_url(self, q="2001:db8::5", by="ip"):
        return reverse("plugins:netbox_kea:combined_leases6") + f"?q={q}&by={by}&server={self.v6_server.pk}"

    def _mock_command(self, mock_client, leases, reservations=()):
        def command_side_effect(cmd, **kwargs):
            if "lease" in cmd:
                args = kwargs.get("arguments", {})
                ip = args.get("ip-address")
                if ip:
                    matching = [entry for entry in leases if entry["ip-address"] == ip]
                    if not matching:
                        return [{"result": 3, "arguments": None}]
                    return [{"result": 0, "arguments": matching[0]}]
                return [{"result": 0, "arguments": {"leases": list(leases), "count": len(leases)}}]
            return [{"result": 2, "arguments": {}}]

        mock_client.command.side_effect = command_side_effect
        mock_client.reservation_get_page.return_value = (list(reservations), 0, 0)

    @patch("netbox_kea.models.KeaClient")
    def test_reserved_badge_appears_when_reservation_exists(self, MockKeaClient):
        """A v6 lease with a matching reservation must show the 'Reserved' badge."""
        self._mock_command(
            MockKeaClient.return_value,
            leases=[dict(_MOCK_LEASE_V6_ENRICHMENT)],
            reservations=[dict(_MOCK_RESERVATION_V6_ENRICHED)],
        )
        response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserved")

    @patch("netbox_kea.models.KeaClient")
    def test_create_reservation_link_when_no_reservation(self, MockKeaClient):
        """A v6 lease without a matching reservation must show a create-reservation link."""
        self._mock_command(
            MockKeaClient.return_value,
            leases=[dict(_MOCK_LEASE_V6_ENRICHMENT)],
            reservations=[],
        )
        response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reservations6/add")

    @patch("netbox_kea.models.KeaClient")
    def test_netbox_ip_synced_link_when_ip_in_netbox(self, MockKeaClient):
        """When the v6 lease IP exists in NetBox IPAM, a 'Synced' link must appear."""
        from ipam.models import IPAddress

        IPAddress.objects.create(address="2001:db8::5/128")
        self._mock_command(
            MockKeaClient.return_value,
            leases=[dict(_MOCK_LEASE_V6_ENRICHMENT)],
            reservations=[],
        )
        response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced")

    @patch("netbox_kea.models.KeaClient")
    def test_netbox_ip_sync_button_when_ip_not_in_netbox(self, MockKeaClient):
        """When the v6 lease IP is not in NetBox IPAM, a 'Sync' button must appear."""
        self._mock_command(
            MockKeaClient.return_value,
            leases=[dict(_MOCK_LEASE_V6_ENRICHMENT)],
            reservations=[],
        )
        response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sync")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservations6Enrichment(_CombinedViewBase):
    """Combined DHCPv6 reservation view must include the same badge enrichment as DHCPv4."""

    def _url(self):
        return reverse("plugins:netbox_kea:combined_reservations6") + f"?server={self.v6_server.pk}"

    @patch("netbox_kea.models.KeaClient")
    def test_active_lease_badge_when_lease_exists(self, MockKeaClient):
        """A v6 reservation with an active lease must show 'Active Lease' badge."""
        MockKeaClient.return_value.reservation_get_page.return_value = (
            [dict(_MOCK_RESERVATION_V6_ENRICHED)],
            0,
            0,
        )
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"leases": [{"ip-address": "2001:db8::5"}]}}
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Lease")

    @patch("netbox_kea.models.KeaClient")
    def test_no_lease_badge_when_no_active_lease(self, MockKeaClient):
        """A v6 reservation without active lease must show 'No Lease' badge."""
        MockKeaClient.return_value.reservation_get_page.return_value = (
            [dict(_MOCK_RESERVATION_V6_ENRICHED)],
            0,
            0,
        )
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Lease")

    @patch("netbox_kea.models.KeaClient")
    def test_edit_action_links_use_server_pk(self, MockKeaClient):
        """Each combined v6 reservation row must have an edit link pointing to the correct server."""
        MockKeaClient.return_value.reservation_get_page.return_value = (
            [dict(_MOCK_RESERVATION_V6_ENRICHED)],
            0,
            0,
        )
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        expected = f"/plugins/kea/servers/{self.v6_server.pk}/reservations6/"
        self.assertContains(response, expected)
