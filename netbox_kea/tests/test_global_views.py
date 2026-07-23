# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for Phase 6: unified combined multi-server views.

These views aggregate data (leases, reservations, subnets) across multiple Kea
servers so that operators can see everything in one place.

URL map under test
------------------
GET /plugins/kea/combined/                       → CombinedDashboardView
GET /plugins/kea/combined/leases4/               → CombinedLeases4View
GET /plugins/kea/combined/leases6/               → CombinedLeases6View
GET /plugins/kea/combined/subnets4/              → CombinedSubnets4View
GET /plugins/kea/combined/subnets6/              → CombinedSubnets6View
GET /plugins/kea/combined/reservations4/         → CombinedReservations4View
GET /plugins/kea/combined/reservations6/         → CombinedReservations6View
GET /plugins/kea/combined/shared-networks4/      → CombinedSharedNetworks4View
GET /plugins/kea/combined/shared-networks6/      → CombinedSharedNetworks6View

All views:
- Extend _CombinedViewMixin which injects all_servers, selected_server_pks, server_qs, active_tab
- Use concurrent.futures.ThreadPoolExecutor for parallel fetching
- Gracefully handle unreachable servers (warning, not 500)

These tests drive the **real** ``KeaClient``; only the HTTP boundary is stubbed
via ``kea_stub.stub_kea``, so the request payloads the views actually send to Kea
are exercised. The command chains each combined view issues (per queried server,
run concurrently) are:

* dashboard: none — the overview never calls Kea.
* status badge: ``version-get`` per enabled protocol (Online iff it doesn't raise).
* reservations: ``reservation-get-page`` (drained via ``iter_reservations``) then,
  when any reservations are found, ``lease{v}-get-all`` per unique subnet for the
  active-lease badge (NetBox IPAM badges hit the DB, not Kea).
* leases (q + by): ``lease{v}-get`` (by IP) or ``lease{v}-get-by-<field>`` then,
  per lease, ``reservation-get`` (by IP, then by MAC) for the reservation badge.
* leases (state only, no q): ``lease{v}-get-page`` (enumerate) + the same badge
  enrichment on the survivors.
* subnets: ``config-get`` + ``stat-lease{v}-get`` (stats are best-effort).
* shared networks: ``config-get``.

A transport failure is modelled by registering a ``requests.ConnectionError``
instance for a command (the stub raises it at the boundary), so the per-server
error handling runs through the real client instead of a mocked ``side_effect``.
"""

import requests
from django.test import TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress

from netbox_kea import constants
from netbox_kea.models import Server

from .kea_stub import _res_get, _res_page, stub_kea
from .utils import _PLUGINS_CONFIG, User, _make_db_server

# ---------------------------------------------------------------------------
# Kea response fixtures
# ---------------------------------------------------------------------------

_MOCK_RESERVATION_V4 = {
    "subnet-id": 1,
    "hw-address": "aa:bb:cc:dd:ee:ff",
    "ip-address": "10.0.0.100",
    "hostname": "host-v4",
}

_MOCK_RESERVATION_V6 = {
    "subnet-id": 1,
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


# ---------------------------------------------------------------------------
# Stub response builders (real KeaClient + HTTP-boundary stub)
# ---------------------------------------------------------------------------


def _leases(entries):
    """A multi-lease response for ``lease{v}-get-*`` / ``lease{v}-get-page``."""
    entries = list(entries)
    return {"result": 0, "arguments": {"count": len(entries), "leases": entries}}


def _lease_one(entry):
    """A single-lease response for ``lease{v}-get`` (by-IP lookup)."""
    return {"result": 0, "arguments": dict(entry)}


#: ``reservation-get-page`` with no hosts (source exhausted → empty reservation list).
_RES_EMPTY_PAGE = {"result": 3}
#: ``reservation-get`` with result 3 = no such reservation.
_RES_NOT_FOUND = {"result": 3}
#: ``lease{v}-get-all`` / ``lease{v}-get`` with result 3 = no such lease.
_LEASE_NONE = {"result": 3}
#: ``stat-lease{v}-get`` with no result-set (utilisation stats absent, non-fatal).
_STAT_EMPTY = {"result": 0, "arguments": {}}


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
        self.v4_server = _make_db_server(name="v4-server", dhcp4=True, dhcp6=False, has_control_agent=False)
        self.v6_server = _make_db_server(name="v6-server", dhcp4=False, dhcp6=True, has_control_agent=False)
        self.dual_server = _make_db_server(name="dual-server", dhcp4=True, dhcp6=True, has_control_agent=False)


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
        """Dashboard must not call Kea API (would slow down the page for many servers)."""
        with stub_kea({}) as kea:
            url = reverse("plugins:netbox_kea:combined")
            self.client.get(url)
        self.assertEqual(kea.commands(), [])

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
        """The combined base template should render links to all 8 tabs."""
        url = reverse("plugins:netbox_kea:combined")
        response = self.client.get(url)
        self.assertContains(response, "/combined/leases4/")
        self.assertContains(response, "/combined/leases6/")
        self.assertContains(response, "/combined/subnets4/")
        self.assertContains(response, "/combined/subnets6/")
        self.assertContains(response, "/combined/reservations4/")
        self.assertContains(response, "/combined/reservations6/")
        self.assertContains(response, "/combined/shared-networks4/")
        self.assertContains(response, "/combined/shared-networks6/")


# ---------------------------------------------------------------------------
# CombinedServerStatusBadgeView  GET /plugins/kea/combined/server-status-badge/<pk>/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedServerStatusBadge(_CombinedViewBase):
    """GET /plugins/kea/combined/server-status-badge/<pk>/ — HTMX status fragment."""

    def _url(self, server):
        return reverse("plugins:netbox_kea:combined_server_status_badge", args=[server.pk])

    def test_online_server_returns_200_with_online_text(self):
        """Reachable server should return 200 and contain 'Online'."""
        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "Kea DHCPv4/2.x"}}}):
            response = self.client.get(self._url(self.v4_server))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Online")

    def test_offline_server_returns_200_with_offline_text(self):
        """Unreachable server should return 200 and contain 'Offline' (no 500)."""
        with stub_kea({"version-get": requests.ConnectionError("connection refused")}):
            response = self.client.get(self._url(self.v4_server))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Offline")

    def test_requires_auth(self):
        """Unauthenticated request must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url(self.v4_server))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_v4_only_server_shows_v4_badge(self):
        """DHCPv4-only server should show DHCPv4 status badge only."""
        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "Kea DHCPv4/2.x"}}}):
            response = self.client.get(self._url(self.v4_server))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DHCPv4")
        self.assertNotContains(response, "DHCPv6")

    def test_v6_only_server_shows_v6_badge(self):
        """DHCPv6-only server should show DHCPv6 status badge only."""
        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "Kea DHCPv6/2.x"}}}):
            response = self.client.get(self._url(self.v6_server))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "DHCPv4")
        self.assertContains(response, "DHCPv6")

    def test_dual_stack_shows_both_badges(self):
        """Dual-stack server should show both DHCPv4 and DHCPv6 status badges."""
        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "Kea/2.x"}}}):
            response = self.client.get(self._url(self.dual_server))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DHCPv4")
        self.assertContains(response, "DHCPv6")

    def test_nonexistent_server_returns_404(self):
        """Request for a server that does not exist must return 404 (before any Kea call)."""
        url = reverse("plugins:netbox_kea:combined_server_status_badge", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# CombinedReservations4View  GET /plugins/kea/combined/reservations4/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservations4View(_CombinedViewBase):
    """GET /plugins/kea/combined/reservations4/ — all DHCPv4 reservations."""

    def test_get_returns_200(self):
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}):
            url = reverse("plugins:netbox_kea:combined_reservations4")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_reservations4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_queries_all_v4_servers(self):
        """Without a server filter, every dhcp4-enabled server is queried."""
        with stub_kea(
            {"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V4)]), "lease4-get-all": _LEASE_NONE}
        ) as kea:
            url = reverse("plugins:netbox_kea:combined_reservations4")
            self.client.get(url)
        # v4_server + dual_server have dhcp4=True → at least 2 get-page calls
        self.assertGreaterEqual(kea.commands().count("reservation-get-page"), 2)

    def test_server_filter_limits_queried_servers(self):
        """?server=<pk> filters down to exactly that one server."""
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}) as kea:
            url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}"
            self.client.get(url)
        self.assertEqual(kea.commands().count("reservation-get-page"), 1)

    def test_results_include_server_name(self):
        """Each row in the merged table must carry the originating server name."""
        with stub_kea({"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V4)]), "lease4-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v4-server")

    def test_unreachable_server_returns_200_with_warning(self):
        """A server that raises an exception must not cause a 500."""
        with stub_kea({"reservation-get-page": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_reservations4")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_no_v4_servers_returns_200_empty_table(self):
        """If no dhcp4-enabled servers exist, the view renders with an empty table (no Kea traffic)."""
        Server.objects.filter(dhcp4=True).update(dhcp4=False)
        with stub_kea({}) as kea:
            url = reverse("plugins:netbox_kea:combined_reservations4")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_context_active_tab(self):
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}):
            url = reverse("plugins:netbox_kea:combined_reservations4")
            response = self.client.get(url)
        self.assertEqual(response.context["active_tab"], "reservations4")

    def test_export_returns_csv(self):
        """?export=table returns a CSV download of the v4 reservations table."""
        with stub_kea({"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V4)]), "lease4-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}&export=table"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_search_form_in_context(self):
        """Response context must contain search_form for the search card to render."""
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}):
            url = reverse("plugins:netbox_kea:combined_reservations4")
            response = self.client.get(url)
        self.assertIn("search_form", response.context)

    def test_search_by_hostname_filters_results(self):
        """?q=host-v4 returns only records whose hostname matches."""
        rec_match = dict(_MOCK_RESERVATION_V4)  # hostname="host-v4"
        rec_nomatch = dict(_MOCK_RESERVATION_V4)
        rec_nomatch["hostname"] = "other-host"
        rec_nomatch["ip-address"] = "10.0.0.200"
        with stub_kea({"reservation-get-page": _res_page([rec_match, rec_nomatch]), "lease4-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}&q=host-v4"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.100")
        self.assertNotContains(response, "10.0.0.200")

    def test_search_by_ip_filters_results(self):
        """?q=10.0.0.100 returns only the matching record."""
        rec = dict(_MOCK_RESERVATION_V4)
        rec2 = dict(_MOCK_RESERVATION_V4)
        rec2["ip-address"] = "10.0.0.200"
        rec2["hostname"] = "other"
        with stub_kea({"reservation-get-page": _res_page([rec, rec2]), "lease4-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}&q=10.0.0.100"
            response = self.client.get(url)
        self.assertContains(response, "10.0.0.100")
        self.assertNotContains(response, "10.0.0.200")

    def test_search_by_subnet_id_filters_results(self):
        """?subnet_id=2 returns only records in subnet 2."""
        rec1 = dict(_MOCK_RESERVATION_V4)  # subnet-id=1
        rec2 = dict(_MOCK_RESERVATION_V4)
        rec2["subnet-id"] = 2
        rec2["ip-address"] = "10.0.0.200"
        with stub_kea({"reservation-get-page": _res_page([rec1, rec2]), "lease4-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}&subnet_id=2"
            response = self.client.get(url)
        self.assertNotContains(response, "10.0.0.100")
        self.assertContains(response, "10.0.0.200")

    def test_search_no_match_returns_empty_table(self):
        """?q=zzz with no matching records renders an empty table (no 500)."""
        with stub_kea({"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V4)]), "lease4-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.v4_server.pk}&q=zzz-no-match"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# CombinedReservations6View  GET /plugins/kea/combined/reservations6/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservations6View(_CombinedViewBase):
    """GET /plugins/kea/combined/reservations6/ — all DHCPv6 reservations."""

    def test_get_returns_200(self):
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}):
            url = reverse("plugins:netbox_kea:combined_reservations6")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_reservations6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_queries_all_v6_servers(self):
        with stub_kea(
            {"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V6)]), "lease6-get-all": _LEASE_NONE}
        ) as kea:
            url = reverse("plugins:netbox_kea:combined_reservations6")
            self.client.get(url)
        self.assertGreaterEqual(kea.commands().count("reservation-get-page"), 2)

    def test_server_filter_limits_queried_servers(self):
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}) as kea:
            url = reverse("plugins:netbox_kea:combined_reservations6") + f"?server={self.v6_server.pk}"
            self.client.get(url)
        self.assertEqual(kea.commands().count("reservation-get-page"), 1)

    def test_unreachable_server_returns_200_with_warning(self):
        with stub_kea({"reservation-get-page": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_reservations6")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_export_returns_csv(self):
        """?export=table returns a CSV download of the v6 reservations table."""
        with stub_kea({"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V6)]), "lease6-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations6") + f"?server={self.v6_server.pk}&export=table"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_search_form_in_context(self):
        """Response context must contain search_form."""
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}):
            url = reverse("plugins:netbox_kea:combined_reservations6")
            response = self.client.get(url)
        self.assertIn("search_form", response.context)

    def test_search_by_hostname_filters_results(self):
        """?q=host-v6 returns only records whose hostname matches."""
        rec_match = dict(_MOCK_RESERVATION_V6)  # hostname="host-v6"
        rec_nomatch = {
            "subnet-id": 1,
            "duid": "00:01:aa:bb",
            "ip-addresses": ["2001:db8::2"],
            "hostname": "other-v6",
        }
        with stub_kea({"reservation-get-page": _res_page([rec_match, rec_nomatch]), "lease6-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations6") + f"?server={self.v6_server.pk}&q=host-v6"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "host-v6")
        self.assertNotContains(response, "other-v6")

    def test_search_by_duid_filters_results(self):
        """?q=00:01:aa:bb returns only the matching DUID record."""
        rec = dict(_MOCK_RESERVATION_V6)  # duid="00:01:aa:bb"
        rec2 = {
            "subnet-id": 1,
            "duid": "ff:ff:ff:ff",
            "ip-addresses": ["2001:db8::99"],
            "hostname": "other",
        }
        with stub_kea({"reservation-get-page": _res_page([rec, rec2]), "lease6-get-all": _LEASE_NONE}):
            url = reverse("plugins:netbox_kea:combined_reservations6") + f"?server={self.v6_server.pk}&q=00:01:aa:bb"
            response = self.client.get(url)
        self.assertContains(response, "00:01:aa:bb")
        self.assertNotContains(response, "ff:ff:ff:ff")


# ---------------------------------------------------------------------------
# CombinedLeases4View  GET /plugins/kea/combined/leases4/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeases4View(_CombinedViewBase):
    """GET /plugins/kea/combined/leases4/ — DHCPv4 leases with cross-server search."""

    def test_get_without_search_returns_200(self):
        with stub_kea({}) as kea:
            url = reverse("plugins:netbox_kea:combined_leases4")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_leases4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_no_kea_calls_without_search_query(self):
        with stub_kea({}) as kea:
            url = reverse("plugins:netbox_kea:combined_leases4")
            self.client.get(url)
        self.assertEqual(kea.commands(), [])

    def test_search_broadcasts_to_all_v4_servers(self):
        with stub_kea(
            {"lease4-get-by-hostname": _leases([dict(_MOCK_LEASE_V4)]), "reservation-get": _RES_NOT_FOUND}
        ) as kea:
            url = reverse("plugins:netbox_kea:combined_leases4") + f"?q=lease-host-v4&by={constants.BY_HOSTNAME}"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(kea.commands().count("lease4-get-by-hostname"), 2)

    def test_search_with_server_filter_limits_calls(self):
        with stub_kea({"lease4-get-by-hostname": _leases([])}) as kea:
            url = (
                reverse("plugins:netbox_kea:combined_leases4")
                + f"?server={self.v4_server.pk}&q=test&by={constants.BY_HOSTNAME}"
            )
            self.client.get(url)
        # One server, no leases returned → exactly one lease search, no enrichment.
        self.assertEqual(kea.commands(), ["lease4-get-by-hostname"])

    def test_unreachable_server_returns_200_with_warning(self):
        with stub_kea({"lease4-get-by-hostname": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_leases4") + f"?q=test&by={constants.BY_HOSTNAME}"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_merged_results_include_server_name(self):
        with stub_kea({"lease4-get-by-hostname": _leases([dict(_MOCK_LEASE_V4)]), "reservation-get": _RES_NOT_FOUND}):
            url = (
                reverse("plugins:netbox_kea:combined_leases4")
                + f"?server={self.v4_server.pk}&q=test&by={constants.BY_HOSTNAME}"
            )
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v4-server")

    def test_context_active_tab(self):
        with stub_kea({"lease4-get-by-hostname": _leases([])}):
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
        with stub_kea({}) as kea:
            url = reverse("plugins:netbox_kea:combined_leases6")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_leases6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_no_kea_calls_without_search_query(self):
        with stub_kea({}) as kea:
            url = reverse("plugins:netbox_kea:combined_leases6")
            self.client.get(url)
        self.assertEqual(kea.commands(), [])

    def test_search_broadcasts_to_all_v6_servers(self):
        with stub_kea(
            {"lease6-get-by-hostname": _leases([dict(_MOCK_LEASE_V6)]), "reservation-get": _RES_NOT_FOUND}
        ) as kea:
            url = reverse("plugins:netbox_kea:combined_leases6") + f"?q=lease-host-v6&by={constants.BY_HOSTNAME}"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(kea.commands().count("lease6-get-by-hostname"), 2)

    def test_unreachable_server_returns_200_with_warning(self):
        with stub_kea({"lease6-get-by-hostname": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_leases6") + f"?q=test&by={constants.BY_HOSTNAME}"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_merged_results_include_server_name(self):
        with stub_kea({"lease6-get-by-hostname": _leases([dict(_MOCK_LEASE_V6)]), "reservation-get": _RES_NOT_FOUND}):
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

    def test_get_returns_200(self):
        with stub_kea({"config-get": _MOCK_CONFIG_V4, "stat-lease4-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets4")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_subnets4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_queries_all_v4_servers(self):
        """Without a filter, every dhcp4-enabled server is queried for config-get."""
        with stub_kea({"config-get": _MOCK_CONFIG_V4, "stat-lease4-get": _STAT_EMPTY}) as kea:
            url = reverse("plugins:netbox_kea:combined_subnets4")
            self.client.get(url)
        # v4_server + dual_server → at least 2 config-get calls
        self.assertGreaterEqual(kea.commands().count("config-get"), 2)

    def test_server_filter_limits_queried_servers(self):
        with stub_kea({"config-get": _MOCK_CONFIG_V4, "stat-lease4-get": _STAT_EMPTY}) as kea:
            url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.v4_server.pk}"
            self.client.get(url)
        # 1 server → config-get (subnets) then stat-lease4-get (utilisation).
        self.assertEqual(kea.commands(), ["config-get", "stat-lease4-get"])

    def test_unreachable_server_returns_200_with_warning(self):
        with stub_kea({"config-get": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_subnets4")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_results_include_server_name(self):
        with stub_kea({"config-get": _MOCK_CONFIG_V4, "stat-lease4-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.v4_server.pk}"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v4-server")

    def test_context_active_tab(self):
        with stub_kea({"config-get": _MOCK_CONFIG_V4, "stat-lease4-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets4")
            response = self.client.get(url)
        self.assertEqual(response.context["active_tab"], "subnets4")

    def test_export_returns_csv(self):
        """?export=table returns a CSV download of the subnet table."""
        with stub_kea({"config-get": _MOCK_CONFIG_V4, "stat-lease4-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.v4_server.pk}&export=table"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_search_form_in_context(self):
        """Response context must contain search_form."""
        with stub_kea({"config-get": _MOCK_CONFIG_V4, "stat-lease4-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets4")
            response = self.client.get(url)
        self.assertIn("search_form", response.context)

    def test_search_by_cidr_filters_results(self):
        """?q=10.0 returns subnets whose CIDR contains '10.0', excludes others."""
        config_with_two_subnets = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [
                            {"id": 1, "subnet": "10.0.1.0/24"},
                            {"id": 2, "subnet": "192.168.0.0/24"},
                        ],
                        "shared-networks": [],
                    }
                },
            }
        ]
        with stub_kea({"config-get": config_with_two_subnets, "stat-lease4-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.v4_server.pk}&q=10.0"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.1.0/24")
        self.assertNotContains(response, "192.168.0.0/24")

    def test_search_by_subnet_id_filters_results(self):
        """?subnet_id=2 returns only the subnet with id=2."""
        config_with_two_subnets = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [
                            {"id": 1, "subnet": "10.0.1.0/24"},
                            {"id": 2, "subnet": "192.168.0.0/24"},
                        ],
                        "shared-networks": [],
                    }
                },
            }
        ]
        with stub_kea({"config-get": config_with_two_subnets, "stat-lease4-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.v4_server.pk}&subnet_id=2"
            response = self.client.get(url)
        self.assertNotContains(response, "10.0.1.0/24")
        self.assertContains(response, "192.168.0.0/24")

    def test_search_no_match_returns_empty_table(self):
        """?q=zzz with no matching subnets renders 200 with empty table."""
        with stub_kea({"config-get": _MOCK_CONFIG_V4, "stat-lease4-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.v4_server.pk}&q=zzz-no-match"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# CombinedSubnets6View  GET /plugins/kea/combined/subnets6/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedSubnets6View(_CombinedViewBase):
    """GET /plugins/kea/combined/subnets6/ — DHCPv6 subnets across all servers."""

    def test_get_returns_200(self):
        with stub_kea({"config-get": _MOCK_CONFIG_V6, "stat-lease6-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets6")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_subnets6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_queries_all_v6_servers(self):
        with stub_kea({"config-get": _MOCK_CONFIG_V6, "stat-lease6-get": _STAT_EMPTY}) as kea:
            url = reverse("plugins:netbox_kea:combined_subnets6")
            self.client.get(url)
        self.assertGreaterEqual(kea.commands().count("config-get"), 2)

    def test_unreachable_server_returns_200_with_warning(self):
        with stub_kea({"config-get": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_subnets6")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_export_returns_csv(self):
        """?export=table returns a CSV download of the v6 subnet table."""
        with stub_kea({"config-get": _MOCK_CONFIG_V6, "stat-lease6-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets6") + f"?server={self.v6_server.pk}&export=table"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_search_form_in_context(self):
        """Response context must contain search_form."""
        with stub_kea({"config-get": _MOCK_CONFIG_V6, "stat-lease6-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets6")
            response = self.client.get(url)
        self.assertIn("search_form", response.context)

    def test_search_by_cidr_filters_results(self):
        """?q=2001:db8 returns only matching v6 subnets."""
        config_with_two_subnets = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp6": {
                        "subnet6": [
                            {"id": 1, "subnet": "2001:db8::/32"},
                            {"id": 2, "subnet": "fd00::/8"},
                        ],
                        "shared-networks": [],
                    }
                },
            }
        ]
        with stub_kea({"config-get": config_with_two_subnets, "stat-lease6-get": _STAT_EMPTY}):
            url = reverse("plugins:netbox_kea:combined_subnets6") + f"?server={self.v6_server.pk}&q=2001:db8"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2001:db8::/32")
        self.assertNotContains(response, "fd00::/8")


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

    def test_reserved_badge_appears_when_reservation_exists(self):
        """A lease with a matching reservation must show the 'Reserved' badge."""
        with stub_kea(
            {
                "lease4-get": _lease_one(_MOCK_LEASE_V4_ENRICHMENT),
                "reservation-get": _res_get(_MOCK_RESERVATION_ENRICHED),
            }
        ):
            response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserved")

    def test_create_reservation_link_when_no_reservation(self):
        """A lease without a matching reservation must show a create-reservation link."""
        with stub_kea({"lease4-get": _lease_one(_MOCK_LEASE_V4_ENRICHMENT), "reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reservations4/add")

    def test_netbox_ip_synced_link_when_ip_in_netbox(self):
        """When the lease IP exists in NetBox IPAM, a 'Synced' link must appear."""
        IPAddress.objects.create(address="10.20.0.5/32")
        with stub_kea({"lease4-get": _lease_one(_MOCK_LEASE_V4_ENRICHMENT), "reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced")

    def test_netbox_ip_sync_button_when_ip_not_in_netbox(self):
        """When the lease IP is not in NetBox IPAM, a 'Sync' button must appear."""
        with stub_kea({"lease4-get": _lease_one(_MOCK_LEASE_V4_ENRICHMENT), "reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sync")

    def test_combined_default_columns_include_reserved_and_netbox_ip(self):
        """GlobalLeaseTable4 default columns must include reserved and netbox_ip."""
        with stub_kea({"lease4-get": _LEASE_NONE}):
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

    def test_active_lease_badge_when_lease_exists(self):
        """A reservation with an active lease must show 'Active Lease' badge."""
        with stub_kea(
            {
                "reservation-get-page": _res_page([dict(_MOCK_RESERVATION_ENRICHED)]),
                "lease4-get-all": _leases([{"ip-address": "10.20.0.5"}]),
            }
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Lease")

    def test_no_lease_badge_when_no_active_lease(self):
        """A reservation without active lease must show 'No Lease' badge."""
        with stub_kea(
            {"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_ENRICHED)]), "lease4-get-all": _leases([])}
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Lease")

    def test_lease_column_header_present(self):
        """GlobalReservationTable4 must render a 'Lease' column header."""
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lease")

    def test_netbox_ip_synced_link_when_ip_in_netbox(self):
        """Reservation IP in NetBox IPAM → 'Synced' link in combined table."""
        IPAddress.objects.create(address="10.20.0.5/32")
        with stub_kea(
            {"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_ENRICHED)]), "lease4-get-all": _leases([])}
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced")

    def test_edit_action_links_use_server_pk(self):
        """Each combined reservation row must have an edit link pointing to the correct server."""
        with stub_kea(
            {"reservation-get-page": _res_page([dict(_MOCK_RESERVATION_ENRICHED)]), "lease4-get-all": _leases([])}
        ):
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
    "hostname": "enriched-host-v6",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeases6Enrichment(_CombinedViewBase):
    """Combined DHCPv6 lease view must include the same badge enrichment as DHCPv4."""

    def _lease_url(self, q="2001:db8::5", by="ip"):
        return reverse("plugins:netbox_kea:combined_leases6") + f"?q={q}&by={by}&server={self.v6_server.pk}"

    def test_reserved_badge_appears_when_reservation_exists(self):
        """A v6 lease with a matching reservation must show the 'Reserved' badge."""
        with stub_kea(
            {
                "lease6-get": _lease_one(_MOCK_LEASE_V6_ENRICHMENT),
                "reservation-get": _res_get(_MOCK_RESERVATION_V6_ENRICHED),
            }
        ):
            response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserved")

    def test_create_reservation_link_when_no_reservation(self):
        """A v6 lease without a matching reservation must show a create-reservation link."""
        with stub_kea({"lease6-get": _lease_one(_MOCK_LEASE_V6_ENRICHMENT), "reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reservations6/add")

    def test_netbox_ip_synced_link_when_ip_in_netbox(self):
        """When the v6 lease IP exists in NetBox IPAM, a 'Synced' link must appear."""
        IPAddress.objects.create(address="2001:db8::5/128")
        with stub_kea({"lease6-get": _lease_one(_MOCK_LEASE_V6_ENRICHMENT), "reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced")

    def test_netbox_ip_sync_button_when_ip_not_in_netbox(self):
        """When the v6 lease IP is not in NetBox IPAM, a 'Sync' button must appear."""
        with stub_kea({"lease6-get": _lease_one(_MOCK_LEASE_V6_ENRICHMENT), "reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._lease_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sync")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservations6Enrichment(_CombinedViewBase):
    """Combined DHCPv6 reservation view must include the same badge enrichment as DHCPv4."""

    def _url(self):
        return reverse("plugins:netbox_kea:combined_reservations6") + f"?server={self.v6_server.pk}"

    def test_active_lease_badge_when_lease_exists(self):
        """A v6 reservation with an active lease must show 'Active Lease' badge."""
        with stub_kea(
            {
                "reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V6_ENRICHED)]),
                "lease6-get-all": _leases([{"ip-address": "2001:db8::5"}]),
            }
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Lease")

    def test_no_lease_badge_when_no_active_lease(self):
        """A v6 reservation without active lease must show 'No Lease' badge."""
        with stub_kea(
            {
                "reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V6_ENRICHED)]),
                "lease6-get-all": _leases([]),
            }
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Lease")

    def test_edit_action_links_use_server_pk(self):
        """Each combined v6 reservation row must have an edit link pointing to the correct server."""
        with stub_kea(
            {
                "reservation-get-page": _res_page([dict(_MOCK_RESERVATION_V6_ENRICHED)]),
                "lease6-get-all": _leases([]),
            }
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        expected = f"/plugins/kea/servers/{self.v6_server.pk}/reservations6/"
        self.assertContains(response, expected)


# ---------------------------------------------------------------------------
# CombinedLeases state filter  (added alongside lease-state sprint)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeasesStateFilter(_CombinedViewBase):
    """State filter in combined leases view must narrow results after merging."""

    _ACTIVE_LEASE = {
        "ip-address": "10.0.0.1",
        "hostname": "active-host",
        "subnet-id": 1,
        "state": 0,
        "valid-lft": 3600,
        "cltt": 1_234_567_890,
        "hw-address": "aa:bb:cc:dd:ee:ff",
    }
    _DECLINED_LEASE = {
        "ip-address": "10.0.0.2",
        "hostname": "declined-host",
        "subnet-id": 1,
        "state": 1,
        "valid-lft": 3600,
        "cltt": 1_234_567_890,
        "hw-address": "aa:bb:cc:dd:ee:00",
    }

    def test_state_filter_excludes_other_states(self):
        """state=1 (Declined) must exclude Active leases from merged results."""
        with stub_kea(
            {
                "lease4-get-by-hostname": _leases([dict(self._ACTIVE_LEASE), dict(self._DECLINED_LEASE)]),
                "reservation-get": _RES_NOT_FOUND,
            }
        ):
            url = (
                reverse("plugins:netbox_kea:combined_leases4")
                + f"?server={self.v4_server.pk}&q=host&by={constants.BY_HOSTNAME}&state=1"
            )
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "active-host")
        self.assertContains(response, "declined-host")

    def test_state_filter_none_returns_all(self):
        """Empty state (no filter) must return both Active and Declined leases."""
        with stub_kea(
            {
                "lease4-get-by-hostname": _leases([dict(self._ACTIVE_LEASE), dict(self._DECLINED_LEASE)]),
                "reservation-get": _RES_NOT_FOUND,
            }
        ):
            url = (
                reverse("plugins:netbox_kea:combined_leases4")
                + f"?server={self.v4_server.pk}&q=host&by={constants.BY_HOSTNAME}"
            )
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "active-host")
        self.assertContains(response, "declined-host")

    def test_export_table_returns_csv(self):
        """?export=table returns a CSV download when search results are available."""
        with stub_kea(
            {"lease4-get-by-hostname": _leases([dict(self._ACTIVE_LEASE)]), "reservation-get": _RES_NOT_FOUND}
        ):
            url = (
                reverse("plugins:netbox_kea:combined_leases4")
                + f"?server={self.v4_server.pk}&q=host&by={constants.BY_HOSTNAME}&export=table"
            )
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_export_without_query_returns_empty_csv(self):
        """?export=table without a search query returns an empty CSV (no 500)."""
        with stub_kea({}):
            url = reverse("plugins:netbox_kea:combined_leases4") + "?export=table"
            response = self.client.get(url)
        self.assertIn(response.status_code, (200, 400))  # must not 500

    def test_state_only_no_q_returns_200(self):
        """?state=1 without q must return 200 (uses lease4-get-page)."""
        with stub_kea({"lease4-get-page": _leases([])}):
            url = reverse("plugins:netbox_kea:combined_leases4") + f"?server={self.v4_server.pk}&state=1"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_state_only_no_q_calls_get_page(self):
        """?state=1 without q must call lease4-get-page (enumerate all)."""
        with stub_kea({"lease4-get-page": _leases([])}) as kea:
            url = reverse("plugins:netbox_kea:combined_leases4") + f"?server={self.v4_server.pk}&state=1"
            self.client.get(url)
        self.assertIn("lease4-get-page", kea.commands())

    def test_state_only_no_q_filters_by_state(self):
        """?state=1 without q must show declined leases and exclude active ones."""
        with stub_kea(
            {
                "lease4-get-page": _leases([dict(self._ACTIVE_LEASE), dict(self._DECLINED_LEASE)]),
                "reservation-get": _RES_NOT_FOUND,
            }
        ):
            url = reverse("plugins:netbox_kea:combined_leases4") + f"?server={self.v4_server.pk}&state=1"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "active-host")
        self.assertContains(response, "declined-host")

    def test_state_only_no_q_server_error_graceful(self):
        """?state=1 without q handles unreachable server gracefully (no 500)."""
        with stub_kea({"lease4-get-page": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_leases4") + f"?server={self.v4_server.pk}&state=1"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Kea config fixture with shared networks
# ---------------------------------------------------------------------------

_MOCK_CONFIG_WITH_SHARED_NET_V4 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [],
                "shared-networks": [
                    {
                        "name": "test-shared-net-v4",
                        "description": "v4 shared network",
                        "subnet4": [{"id": 2, "subnet": "10.1.0.0/24"}],
                    }
                ],
            }
        },
    }
]

_MOCK_CONFIG_WITH_SHARED_NET_V6 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp6": {
                "subnet6": [],
                "shared-networks": [
                    {
                        "name": "test-shared-net-v6",
                        "description": "v6 shared network",
                        "subnet6": [{"id": 3, "subnet": "2001:db8:1::/48"}],
                    }
                ],
            }
        },
    }
]


# ---------------------------------------------------------------------------
# CombinedSharedNetworks4View  GET /plugins/kea/combined/shared-networks4/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedSharedNetworks4View(_CombinedViewBase):
    """GET /plugins/kea/combined/shared-networks4/ — DHCPv4 shared networks across all servers."""

    def test_get_returns_200(self):
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V4}):
            url = reverse("plugins:netbox_kea:combined_shared_networks4")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_shared_networks4")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_queries_all_v4_servers(self):
        """Without a filter, every dhcp4-enabled server is queried for config-get."""
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V4}) as kea:
            url = reverse("plugins:netbox_kea:combined_shared_networks4")
            self.client.get(url)
        # v4_server + dual_server → at least 2 config-get calls
        self.assertGreaterEqual(kea.commands().count("config-get"), 2)

    def test_unreachable_server_returns_200_with_warning(self):
        with stub_kea({"config-get": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_shared_networks4")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_results_include_server_name(self):
        """Shared network rows must include the server name they came from."""
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V4}):
            url = reverse("plugins:netbox_kea:combined_shared_networks4") + f"?server={self.v4_server.pk}"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v4-server")

    def test_context_active_tab(self):
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V4}):
            url = reverse("plugins:netbox_kea:combined_shared_networks4")
            response = self.client.get(url)
        self.assertEqual(response.context["active_tab"], "shared_networks4")

    def test_shared_network_name_in_response(self):
        """The shared network name must appear in the rendered table."""
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V4}):
            url = reverse("plugins:netbox_kea:combined_shared_networks4") + f"?server={self.v4_server.pk}"
            response = self.client.get(url)
        self.assertContains(response, "test-shared-net-v4")

    def test_export_returns_csv(self):
        """?export=table returns a CSV download of shared network data."""
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V4}):
            url = reverse("plugins:netbox_kea:combined_shared_networks4") + f"?server={self.v4_server.pk}&export=table"
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))


# ---------------------------------------------------------------------------
# CombinedSharedNetworks6View  GET /plugins/kea/combined/shared-networks6/
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedSharedNetworks6View(_CombinedViewBase):
    """GET /plugins/kea/combined/shared-networks6/ — DHCPv6 shared networks across all servers."""

    def test_get_returns_200(self):
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V6}):
            url = reverse("plugins:netbox_kea:combined_shared_networks6")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:combined_shared_networks6")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_queries_all_v6_servers(self):
        """Without a filter, every dhcp6-enabled server is queried."""
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V6}) as kea:
            url = reverse("plugins:netbox_kea:combined_shared_networks6")
            self.client.get(url)
        self.assertGreaterEqual(kea.commands().count("config-get"), 2)

    def test_unreachable_server_returns_200_with_warning(self):
        with stub_kea({"config-get": requests.ConnectionError("refused")}):
            url = reverse("plugins:netbox_kea:combined_shared_networks6")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_context_active_tab(self):
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V6}):
            url = reverse("plugins:netbox_kea:combined_shared_networks6")
            response = self.client.get(url)
        self.assertEqual(response.context["active_tab"], "shared_networks6")

    def test_shared_network_name_in_response(self):
        """The v6 shared network name must appear in the rendered table."""
        with stub_kea({"config-get": _MOCK_CONFIG_WITH_SHARED_NET_V6}):
            url = reverse("plugins:netbox_kea:combined_shared_networks6") + f"?server={self.v6_server.pk}"
            response = self.client.get(url)
        self.assertContains(response, "test-shared-net-v6")
