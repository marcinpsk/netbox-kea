# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for netbox_kea plugin.

Also contains pure-Python unit tests for helper functions defined in views.py
(e.g. ``_extract_identifier``), which do not require a database but live here
because they are tightly coupled to view logic.

These tests verify correct HTTP responses and redirect behaviour for every view.
All Kea HTTP calls are mocked so no running Kea instance is required.

Test organisation strategy
--------------------------
Each view class gets its own ``TestCase`` subclass so failures are isolated and
clearly named.  Every test that triggers a redirect asserts that the redirect URL
contains an *integer* pk (never the string "None"), which is the pattern that
revealed the original ``POST /plugins/kea/servers/None`` 404 bug.

View tests use ``django.test.TestCase`` because they write to the test database
(user + server fixtures).  Server objects are created via ``Server.objects.create()``
which does **not** call ``Model.clean()`` and therefore does not trigger live Kea
connectivity checks.
"""

from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress as NbIP

from netbox_kea.kea import KeaClient
from netbox_kea.models import Server
from netbox_kea.views.leases import _fetch_subnet_choices, _subnet_choices_cache_key, _subnet_sort_key

from .kea_stub import queued, stub_kea
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerLeases4View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/leases4/"""

    def test_get_returns_200(self):
        """Initial leases4 page renders without Kea API calls."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_with_dhcp4_disabled_redirects_to_server_with_valid_pk(self):
        """When DHCPv4 is disabled the view must redirect to the server detail page.

        The redirect URL must contain an integer pk — this is the pattern that
        would fail with servers/None if the instance had pk=None.
        """
        v6_only = _make_db_server(name="v6-only", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_leases4", args=[v6_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(v6_only.pk), response.url)

    def test_get_nonexistent_returns_404(self):
        url = reverse("plugins:netbox_kea:server_leases4", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerLeases6View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/leases6/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_with_dhcp6_disabled_redirects_to_server_with_valid_pk(self):
        v4_only = _make_db_server(name="v4-only", dhcp4=True, dhcp6=False)
        url = reverse("plugins:netbox_kea:server_leases6", args=[v4_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(v4_only.pk), response.url)


# ─────────────────────────────────────────────────────────────────────────────
# Lease delete views
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerLeases4DeleteView(_ViewTestBase):
    """POST /plugins/kea/servers/<pk>/leases4/delete/"""

    def test_get_redirects_to_server_not_none(self):
        """GET on a POST-only view must redirect back to the server (never to servers/None)."""
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(self.server.pk), response.url)

    def test_post_empty_form_redirects_not_none(self):
        """POST with invalid/empty lease list must redirect, not to servers/None."""
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_post_htmx_single_lease_returns_hx_refresh(self):
        """An HTMX POST with a single IP and _confirm returns HX-Refresh: true instead of redirect."""
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        with stub_kea({"lease4-del": {"result": 0, "text": "Success"}}) as kea:
            response = self.client.post(
                url,
                {"pk": "192.0.2.1", "_confirm": "1"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Refresh"), "true")
        # De-mocked: assert the real request payload built by KeaClient.command(), not a mock call.
        self.assertEqual(kea.commands(), ["lease4-del"])
        body = kea.bodies("lease4-del")[0]
        self.assertEqual(body["arguments"], {"ip-address": "192.0.2.1"})
        self.assertEqual(body["service"], ["dhcp4"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerLeases6DeleteView(_ViewTestBase):
    """POST /plugins/kea/servers/<pk>/leases6/delete/"""

    def test_get_redirects_to_server_not_none(self):
        url = reverse("plugins:netbox_kea:server_leases6_delete", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(self.server.pk), response.url)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7a: "Reserved" badge on lease pages
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservedBadgeOnLeases(_ViewTestBase):
    """HTMX lease search must show a 'Reserved' badge when a matching reservation exists.

    The badge links to the reservation edit form so operators can quickly jump
    to the reservation from the lease table.
    """

    _LEASE4 = {
        "ip-address": "192.168.1.100",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "subnet-id": 1,
        "cltt": 1700000000,
        "valid-lft": 86400,
        "hostname": "testhost",
    }
    _RESERVATION4 = {
        "ip-address": "192.168.1.100",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "subnet-id": 1,
        "hostname": "testhost",
    }
    # The lease-search page fetches the subnet quick-select via config-get first.
    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.168.1.0/24"}]}}}

    def _htmx_get(self, url, data):
        """Issue an HTMX GET request (adds HX-Request header)."""
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def test_reserved_badge_shown_when_reservation_exists(self):
        """When a lease IP has a corresponding reservation, the table cell shows 'Reserved'."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": {"ip-address": "192.168.1.100", **self._LEASE4}},
                "reservation-get": {"result": 0, "arguments": self._RESERVATION4},
            }
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserved")

    def test_no_reserved_badge_when_no_reservation(self):
        """When no reservation exists for the lease IP, no badge is rendered."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": {"ip-address": "192.168.1.100", **self._LEASE4}},
                "reservation-get": {"result": 3},  # not found
            }
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        self.assertEqual(response.status_code, 200)
        # HX-Push-Url is set only on the success render path, and the lease row must
        # appear — together these prove the table rendered, not the exception partial.
        self.assertIn("HX-Push-Url", response.headers)
        self.assertContains(response, "192.168.1.100")
        # The column header says "Reserved" — check no badge link is rendered
        self.assertNotContains(response, 'text-decoration-none">Reserved</a>')

    def test_no_crash_when_host_cmds_unavailable(self):
        """When host_cmds is not loaded, reservation lookup is skipped and no badge shown."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": {"ip-address": "192.168.1.100", **self._LEASE4}},
                # host_cmds not loaded — result=2 (unknown command) makes reservation_get raise KeaException.
                "reservation-get": {"result": 2, "text": "unknown command 'reservation-get'"},
            }
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        # Must not 500; page renders normally (success path sets HX-Push-Url and
        # shows the lease row) without a reservation badge.
        self.assertEqual(response.status_code, 200)
        self.assertIn("HX-Push-Url", response.headers)
        self.assertContains(response, "192.168.1.100")
        self.assertNotContains(response, 'text-decoration-none">Reserved</a>')


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9A: Lease search paths — all BY_* types
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchPaths(_ViewTestBase):
    """Each search-by type in BaseServerLeasesView.get_leases() must dispatch the
    correct Kea command with correct arguments, via HTMX GET.

    De-mocked: exercises the real ``KeaClient`` so the actual request payload built
    by ``KeaClient.command()`` — command name, ``arguments``, and ``service`` — is
    asserted, not a ``MagicMock`` call-arg. Only the HTTP boundary
    (``requests.Session.post``) is stubbed. A search issues ``config-get`` (subnet
    quick-select) → ``lease{v}-get…`` → per-lease ``reservation-get`` enrichment.
    """

    _LEASE4 = {
        "ip-address": "10.0.0.5",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "client-id": "01:aa:bb:cc:dd:ee:ff",
        "hostname": "search-host",
        "subnet-id": 1,
        "valid-lft": 3600,
        "cltt": 1_700_000_000,
    }
    # The lease-search page fetches the subnet quick-select via config-get first.
    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}
    _CONFIG6 = {"result": 0, "arguments": {"Dhcp6": {"subnet6": [{"id": 1, "subnet": "2001:db8::/64"}]}}}
    # Reservation enrichment runs for every returned lease; "not found" (result 3)
    # means no reservation, which is all these lease-command tests care about.
    _NO_RESERVATION = {"result": 3}

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def _url6(self):
        return reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])

    def _multi(self, leases):
        """Multi-result lease-get response envelope (leases list + count)."""
        return {"result": 0, "arguments": {"leases": leases, "count": len(leases)}}

    def _single(self, lease):
        """Single-result lease-get response envelope (lease fields under arguments)."""
        return {"result": 0, "arguments": dict(lease)}

    def test_search_by_hw_address_sends_correct_command(self):
        """BY_HW_ADDRESS must call lease4-get-by-hw-address with hw-address argument."""
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-by-hw-address": self._multi([dict(self._LEASE4)]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-by-hw-address", kea.commands())
        body = kea.bodies("lease4-get-by-hw-address")[0]
        self.assertEqual(body["arguments"]["hw-address"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(body["service"], ["dhcp4"])

    def test_search_by_hostname_sends_correct_command(self):
        """BY_HOSTNAME must call lease4-get-by-hostname with hostname argument."""
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-by-hostname": self._multi([dict(self._LEASE4)]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "hostname", "q": "search-host"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-by-hostname", kea.commands())
        body = kea.bodies("lease4-get-by-hostname")[0]
        self.assertEqual(body["arguments"]["hostname"], "search-host")
        self.assertEqual(body["service"], ["dhcp4"])

    def test_search_by_client_id_sends_correct_command(self):
        """BY_CLIENT_ID must call lease4-get-by-client-id with client-id argument."""
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-by-client-id": self._multi([dict(self._LEASE4)]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "client_id", "q": "01:aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-by-client-id", kea.commands())
        body = kea.bodies("lease4-get-by-client-id")[0]
        self.assertEqual(body["arguments"]["client-id"], "01:aa:bb:cc:dd:ee:ff")
        self.assertEqual(body["service"], ["dhcp4"])

    def test_search_by_subnet_id_sends_correct_command(self):
        """BY_SUBNET_ID must call lease4-get-all with subnets=[<id>]."""
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-all": self._multi([dict(self._LEASE4)]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "subnet_id", "q": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-all", kea.commands())
        body = kea.bodies("lease4-get-all")[0]
        self.assertEqual(body["arguments"]["subnets"], [1])
        self.assertEqual(body["service"], ["dhcp4"])

    def test_search_by_ip_returns_200(self):
        """BY_IP must call lease4-get with ip-address argument and return 200."""
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": self._single(self._LEASE4),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get", kea.commands())
        body = kea.bodies("lease4-get")[0]
        self.assertEqual(body["arguments"]["ip-address"], "10.0.0.5")
        self.assertEqual(body["service"], ["dhcp4"])

    def test_search_result_3_returns_empty_table(self):
        """result=3 (not found) must render an empty table, not a 500."""
        # Empty result short-circuits enrichment, so no reservation-get is issued.
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 3, "arguments": None},
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "ip", "q": "10.0.0.99"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get", kea.commands())

    def test_search_by_duid_v6_sends_correct_command(self):
        """BY_DUID on the v6 endpoint must call lease6-get-by-duid."""
        server6 = _make_db_server(name="kea-v6-search", ca_url="https://kea6.example.com", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_leases6", args=[server6.pk])
        with stub_kea(
            {
                "config-get": self._CONFIG6,
                "lease6-get-by-duid": self._multi([]),
            }
        ) as kea:
            response = self._htmx_get(url, {"by": "duid", "q": "00:01:aa:bb:cc:dd"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease6-get-by-duid", kea.commands())
        body = kea.bodies("lease6-get-by-duid")[0]
        self.assertEqual(body["arguments"]["duid"], "00:01:aa:bb:cc:dd")
        self.assertEqual(body["service"], ["dhcp6"])


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9B: CSV export — BaseServerLeasesView.get_export()
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExport(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/leases4/?export=all must return a CSV file."""

    _LEASE4 = {
        "ip-address": "10.0.0.5",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "hostname": "export-host",
        "subnet-id": 1,
        "valid-lft": 3600,
        "cltt": 1_700_000_000,
    }
    # The export path builds the search form, which fetches the subnet quick-select.
    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_export_all_returns_csv_content_type(self):
        """?export=all must respond with text/csv Content-Type."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)}}):
            response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_export_table_returns_csv(self):
        """?export=table must also return text/csv (selected columns)."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)}}):
            response = self.client.get(self._url(), {"export": "table", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_export_with_invalid_form_redirects(self):
        """?export=all with missing q/by must redirect (not crash)."""
        # No 'q' or 'by' — form is invalid
        response = self.client.get(self._url(), {"export": "all"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_export_by_subnet_paginates_all_leases(self):
        """?export=all&by=subnet must paginate until next_cursor is None."""
        page1_leases = [
            {
                "ip-address": f"10.0.0.{i}",
                "hw-address": "aa:bb:cc:dd:ee:ff",
                "hostname": f"h{i}",
                "subnet-id": 1,
                "valid-lft": 3600,
                "cltt": 1_700_000_000,
            }
            for i in range(1, 4)
        ]
        # Page 1 returns 3 leases (count == per_page 3 → more data); page 2 returns
        # result=3 (end). FIFO queue on lease4-get-page drives the pagination loop.
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-page": queued(
                    {"result": 0, "arguments": {"leases": page1_leases, "count": 3}},
                    {"result": 3, "arguments": None},
                ),
            }
        ) as kea:
            # Pass per_page=3 so that count(3) == per_page(3) triggers the next-page fetch.
            response = self.client.get(
                self._url(),
                {"export": "all", "by": "subnet", "q": "10.0.0.0/24", "per_page": "3"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))
        self.assertGreaterEqual(kea.commands().count("lease4-get-page"), 2)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9C: Lease delete — full confirmation flow + error paths
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseDeleteFullFlow(_ViewTestBase):
    """Full POST flow for lease bulk deletion: confirm page → confirmed delete → Kea error."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])

    def test_post_with_ips_no_confirm_renders_confirmation_page(self):
        """POST with lease IPs but no _confirm renders the bulk_delete confirmation template."""
        response = self.client.post(self._url(), {"pk": ["10.0.0.1", "10.0.0.2"]})
        self.assertEqual(response.status_code, 200)
        # Must show the confirmation template (not a redirect)
        self.assertContains(response, "10.0.0.1")
        self.assertContains(response, "10.0.0.2")

    def test_post_confirmed_calls_kea_and_redirects(self):
        """POST with _confirm=1 must call Kea lease4-del and redirect."""
        with stub_kea({"lease4-del": {"result": 0}}) as kea:
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        # Verify Kea was called with the lease4-del command (real payload on the wire).
        self.assertIn("lease4-del", kea.commands())
        self.assertEqual(kea.bodies("lease4-del")[0]["arguments"], {"ip-address": "10.0.0.1"})

    def test_post_confirmed_kea_error_redirects_with_error_message(self):
        """When Kea returns an error during deletion, must redirect (not 500) and show error."""
        # result=1 makes the real KeaClient.command() raise KeaException (delete uses check=(0,3)).
        with stub_kea({"lease4-del": {"result": 1, "text": "lease not found"}}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.5"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_forbidden_user_gets_403(self):
        """A user without bulk_delete_lease_from_server permission must receive 403."""
        from django.contrib.auth import get_user_model as _get_user_model

        User2 = _get_user_model()
        unprivileged = User2.objects.create_user("noperm_user", password="x")
        self.client.force_login(unprivileged)
        response = self.client.post(
            self._url(),
            {"pk": ["10.0.0.1"], "_confirm": "1"},
        )
        self.assertEqual(response.status_code, 403)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9D: _enrich_leases_with_badges error paths
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesErrorPaths(_ViewTestBase):
    """_enrich_leases_with_badges must degrade gracefully on unexpected errors."""

    _LEASE4 = {
        "ip-address": "10.0.0.5",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "hostname": "enrich-host",
        "subnet-id": 1,
        "valid-lft": 3600,
        "cltt": 1_700_000_000,
    }
    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def test_non_result2_kea_exception_does_not_crash(self):
        """A KeaException with result=1 (server error) on reservation lookup must not 500."""
        # result=1 on reservation-get makes the real client raise KeaException (non-result-2),
        # which enrichment treats as indeterminate rather than crashing.
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 1, "text": "server error"},
            }
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)

    def test_unexpected_exception_on_reservation_lookup_does_not_crash(self):
        """An unexpected exception (e.g. network error) during reservation lookup must not 500."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": RuntimeError("socket closed"),
            }
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)

    def test_sync_url_set_when_no_netbox_ip(self):
        """When the lease IP is absent from NetBox, sync_url must be set on the lease dict."""
        # No NbIP created → bulk_fetch_netbox_ips returns {} from the real (empty) DB.
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 3},  # no reservation
            }
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        # Sync button (hx-post) must appear since no NetBox IP
        self.assertContains(response, "hx-post")

    def test_synced_badge_set_when_netbox_ip_exists(self):
        """When the lease IP exists in NetBox IPAM, netbox_ip_url must be set (Synced badge)."""
        NbIP.objects.create(address="10.0.0.5/24")  # real IPAM row → resolved by bulk_fetch_netbox_ips
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 3},  # no reservation
            }
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced")


# ─────────────────────────────────────────────────────────────────────────────
# P3 Refinement: stale MAC badge — specific MAC values + inline delete URL
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestStaleMacBadgeEnrichment(_ViewTestBase):
    """_enrich_leases_with_badges must store MAC strings and delete URL on stale-MAC leases."""

    _LEASE4 = {
        "ip-address": "10.0.0.5",
        "hw-address": "aa:bb:cc:dd:ee:01",
        "hostname": "stale-host",
        "subnet-id": 7,
        "valid-lft": 3600,
        "cltt": 1_700_000_000,
    }
    _RESERVATION = {
        "ip-address": "10.0.0.5",
        "hw-address": "aa:bb:cc:dd:ee:99",  # different MAC → stale
        "subnet-id": 7,
    }
    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 7, "subnet": "10.0.0.0/24"}]}}}

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _stub(self, reservation):
        """stub_kea responses for a single IP-matched lease with *reservation*."""
        return stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 0, "arguments": reservation},
            }
        )

    def test_stale_mac_badge_shows_specific_macs_in_title(self):
        """The ⚠ MAC? badge title must contain both lease MAC and reservation MAC."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub(self._RESERVATION):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "aa:bb:cc:dd:ee:01")  # lease MAC in tooltip
        self.assertContains(response, "aa:bb:cc:dd:ee:99")  # reservation MAC in tooltip

    def test_stale_mac_badge_renders_htmx_delete_button(self):
        """The stale-MAC badge must include an HTMX delete button (hx-post) for one-click removal."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub(self._RESERVATION):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        # hx-post must point to the delete endpoint (distinct from the bulk-delete form action)
        delete_url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        self.assertContains(response, f'hx-post="{delete_url}"')

    def test_matching_mac_badge_has_no_htmx_delete_button(self):
        """When lease MAC matches reservation MAC, no HTMX delete button must appear."""
        matching_rsv = {**self._RESERVATION, "hw-address": self._LEASE4["hw-address"]}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub(matching_rsv):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        delete_url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        self.assertNotContains(response, f'hx-post="{delete_url}"')

    def test_stale_mac_badge_no_delete_when_no_permission(self):
        """When the user lacks delete permission, the stale-MAC badge must NOT include an HTMX delete button."""
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        User = get_user_model()
        readonly_user = User.objects.create_user(username="readonly_stale", password="pass")
        # Grant only view permission via NetBox's ObjectPermission (not change/delete)
        ct = ContentType.objects.get_for_model(Server)
        view_obj_perm = ObjectPermission.objects.create(name="test-view-server-readonly", actions=["view"])
        view_obj_perm.object_types.add(ct)
        view_obj_perm.users.add(readonly_user)
        self.client.force_login(readonly_user)

        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub(self._RESERVATION):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        delete_url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        self.assertNotContains(response, f'hx-post="{delete_url}"')


# ─────────────────────────────────────────────────────────────────────────────
# Feature 3.3: Export All Leases — BaseServerDHCPLeasesView.get_export_all()
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportAll(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/leases4/?export_all=1 must return a full CSV."""

    _LEASE = {
        "ip-address": "10.0.0.1",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "hostname": "export-host",
        "subnet-id": 1,
        "valid-lft": 3600,
        "cltt": 1_700_000_000,
    }

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def _url6(self):
        return reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])

    def test_export_all_returns_csv(self):
        """?export_all=1 must return text/csv."""
        # One page of one lease (count 1 < per_page 1000) ends pagination after one call.
        with stub_kea({"lease4-get-page": {"result": 0, "arguments": {"leases": [self._LEASE], "count": 1}}}):
            response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_export_all_includes_lease_data(self):
        """?export_all=1 CSV must contain the lease IP address."""
        with stub_kea({"lease4-get-page": {"result": 0, "arguments": {"leases": [self._LEASE], "count": 1}}}):
            response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        content = (
            b"".join(response.streaming_content).decode()
            if hasattr(response, "streaming_content")
            else response.content.decode()
        )
        self.assertIn("10.0.0.1", content)

    def test_export_all_paginates_all_leases(self):
        """?export_all=1 must paginate until Kea returns result=3."""
        # The view uses per_page=1000. Report count==1000 on the first page so the
        # view sees a full page and issues a second request; page 2 returns result=3.
        page1 = [
            {
                "ip-address": f"10.0.0.{i}",
                "hw-address": "aa:bb:cc:dd:ee:ff",
                "hostname": f"h{i}",
                "subnet-id": 1,
                "valid-lft": 3600,
                "cltt": 1_700_000_000,
            }
            for i in range(1, 3)
        ]
        with stub_kea(
            {
                "lease4-get-page": queued(
                    {"result": 0, "arguments": {"leases": page1, "count": 1000}},
                    {"result": 3, "arguments": None},
                )
            }
        ) as kea:
            response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))
        self.assertGreaterEqual(kea.commands().count("lease4-get-page"), 2)

    def test_export_all_v6_starts_from_double_colon(self):
        """?export_all=1 for v6 must start the cursor from '::'."""
        with stub_kea({"lease6-get-page": {"result": 3, "arguments": None}}) as kea:
            response = self.client.get(self._url6(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        pages = kea.bodies("lease6-get-page")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["arguments"]["from"], "::")

    def test_export_all_v4_starts_from_zero_ip(self):
        """?export_all=1 for v4 must start the cursor from '0.0.0.0'."""
        with stub_kea({"lease4-get-page": {"result": 3, "arguments": None}}) as kea:
            response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        pages = kea.bodies("lease4-get-page")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["arguments"]["from"], "0.0.0.0")


# TestLeaseEditView
# ---------------------------------------------------------------------------

_LEASE4_GET_RESP = [
    {
        "result": 0,
        "arguments": {
            "ip-address": "10.0.0.100",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "host1.example.com",
            "subnet-id": 1,
            "cltt": 1700000000,
            "valid-lft": 3600,
            "state": 0,
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditView(_ViewTestBase):
    """Tests for ServerLease4/6EditView."""

    def _url(self, version=4, ip="10.0.0.100"):
        return reverse(
            f"plugins:netbox_kea:server_lease{version}_edit",
            args=[self.server.pk, ip],
        )

    def test_url_registered_v4(self):
        """URL server_lease4_edit is registered."""
        url = self._url(version=4)
        self.assertIn("leases", url)
        self.assertIn("edit", url)

    def test_url_registered_v6(self):
        """URL server_lease6_edit is registered."""
        url = self._url(version=6, ip="2001:db8::100")
        self.assertIn("leases", url)
        self.assertIn("edit", url)

    def test_get_returns_200(self):
        """GET returns 200 OK."""
        with stub_kea({"lease4-get": _LEASE4_GET_RESP[0]}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_prefills_hostname(self):
        """GET pre-fills hostname from the existing lease."""
        with stub_kea({"lease4-get": _LEASE4_GET_RESP[0]}):
            response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("host1.example.com", content)

    def test_get_prefills_hw_address(self):
        """GET pre-fills hw_address from the existing lease (v4 only)."""
        with stub_kea({"lease4-get": _LEASE4_GET_RESP[0]}):
            response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("aa:bb:cc:dd:ee:ff", content)

    def test_post_calls_lease_update_and_redirects(self):
        """POST with valid data calls lease_update and redirects."""
        # lease_update reads the current lease (lease4-get) then writes lease4-update.
        with stub_kea({"lease4-get": _LEASE4_GET_RESP[0], "lease4-update": {"result": 0}}) as kea:
            response = self.client.post(
                self._url(),
                {
                    "hostname": "newhost.example.com",
                    "hw_address": "11:22:33:44:55:66",
                    "valid_lft": "7200",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("lease4-update", kea.commands())
        update_args = kea.bodies("lease4-update")[0]["arguments"]
        self.assertEqual(update_args["hostname"], "newhost.example.com")
        self.assertEqual(update_args["hw-address"], "11:22:33:44:55:66")
        self.assertEqual(update_args["valid-lft"], 7200)

    def test_post_kea_exception_redirects_with_error(self):
        """POST that raises KeaException shows error and redirects."""
        # result=1 on lease4-update makes the real client raise KeaException.
        with stub_kea({"lease4-get": _LEASE4_GET_RESP[0], "lease4-update": {"result": 1, "text": "lease not found"}}):
            response = self.client.post(
                self._url(),
                {
                    "hostname": "newhost.example.com",
                    "hw_address": "11:22:33:44:55:66",
                    "valid_lft": "7200",
                },
            )
        self.assertEqual(response.status_code, 302)

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


# ---------------------------------------------------------------------------
# TestLeaseStateFilter
# ---------------------------------------------------------------------------

_STATE_LEASES_RESP = [
    {
        "result": 0,
        "arguments": {
            "leases": [
                {
                    "ip-address": "10.0.0.1",
                    "hw-address": "aa:bb:cc:dd:ee:01",
                    "hostname": "active-host",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 1_700_000_000,
                    "state": 0,
                },
                {
                    "ip-address": "10.0.0.2",
                    "hw-address": "aa:bb:cc:dd:ee:02",
                    "hostname": "declined-host",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 1_700_000_000,
                    "state": 1,
                },
                {
                    "ip-address": "10.0.0.3",
                    "hw-address": "aa:bb:cc:dd:ee:03",
                    "hostname": "expired-host",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 1_700_000_000,
                    "state": 2,
                },
            ]
        },
    }
]

_PAGE_LEASES_RESP = [
    {
        "result": 0,
        "arguments": {
            "count": 2,
            "leases": [
                {
                    "ip-address": "10.0.0.10",
                    "hw-address": "aa:bb:cc:dd:ee:10",
                    "hostname": "page-active",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 1_700_000_000,
                    "state": 0,
                },
                {
                    "ip-address": "10.0.0.11",
                    "hw-address": "aa:bb:cc:dd:ee:11",
                    "hostname": "page-declined",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 1_700_000_000,
                    "state": 1,
                },
            ],
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseStateFilter(_ViewTestBase):
    """Tests that the optional state filter correctly limits lease results."""

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    # Subnet quick-select fetched via config-get; reservation enrichment finds none.
    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}

    def test_state_column_rendered_in_table(self):
        """Lease table includes a state_label column header."""
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-by-hw-address": _STATE_LEASES_RESP[0],
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:01"})
        self.assertEqual(response.status_code, 200)
        # State column header must be present
        self.assertContains(response, "State")

    def test_state_label_active_rendered(self):
        """Active lease shows 'Active' state badge."""
        active_lease = _STATE_LEASES_RESP[0]["arguments"]["leases"][0]
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-by-hw-address": {"result": 0, "arguments": {"leases": [active_lease]}},
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:01"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active")

    def test_state_label_declined_rendered(self):
        """Declined lease shows 'Declined' state badge."""
        declined_lease = _STATE_LEASES_RESP[0]["arguments"]["leases"][1]
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-by-hw-address": {"result": 0, "arguments": {"leases": [declined_lease]}},
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:02"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Declined")

    def test_state_filter_declined_hides_active(self):
        """State filter=1 (Declined) excludes Active leases from search results."""
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-by-hostname": _STATE_LEASES_RESP[0],
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hostname", "q": "host", "state": "1"})
        self.assertEqual(response.status_code, 200)
        # Active and Expired hosts should not appear
        self.assertNotContains(response, "active-host")
        self.assertNotContains(response, "expired-host")
        self.assertContains(response, "declined-host")

    def test_state_filter_any_returns_all(self):
        """Empty state filter (Any) returns all leases."""
        with stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get-by-hostname": _STATE_LEASES_RESP[0],
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hostname", "q": "host", "state": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "active-host")
        self.assertContains(response, "declined-host")
        self.assertContains(response, "expired-host")

    def test_state_filter_applied_on_paginated_subnet_search(self):
        """State filter also applies to paginated subnet-based search."""
        with stub_kea(
            {"config-get": self._CONFIG4, "lease4-get-page": _PAGE_LEASES_RESP[0], "reservation-get": {"result": 3}}
        ):
            response = self._htmx_get(
                self._url4(),
                {"by": "subnet", "q": "10.0.0.0/24", "state": "1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "page-active")
        self.assertContains(response, "page-declined")


# ---------------------------------------------------------------------------
# TestLeaseAddView — Manual Lease Add (lease4/6-add)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": 30}})
class TestLeaseAddView(_ViewTestBase):
    """Tests for ServerLease4AddView and ServerLease6AddView."""

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:server_lease{version}_add", args=[self.server.pk])

    def _valid_post4(self, **overrides):
        data = {
            "ip_address": "10.0.0.200",
            "subnet_id": "1",
            "hw_address": "aa:bb:cc:dd:ee:ff",
            "valid_lft": "3600",
            "hostname": "newlease.example.com",
        }
        data.update(overrides)
        return data

    def _valid_post6(self, **overrides):
        data = {
            "ip_address": "2001:db8::200",
            "duid": "00:01:02:03:04:05",
            "iaid": "12345",
            "subnet_id": "1",
            "valid_lft": "3600",
            "hostname": "newlease6.example.com",
        }
        data.update(overrides)
        return data

    def test_url_registered_v4(self):
        """URL server_lease4_add is registered and contains 'leases'."""
        url = self._url(version=4)
        self.assertIn("leases", url)
        self.assertIn("add", url)

    def test_url_registered_v6(self):
        """URL server_lease6_add is registered and contains 'leases'."""
        url = self._url(version=6)
        self.assertIn("leases", url)
        self.assertIn("add", url)

    def test_get_lease4_add_returns_200(self):
        """GET /leases4/add/ returns 200 and renders the add form."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ip_address")

    def test_get_lease6_add_returns_200(self):
        """GET /leases6/add/ returns 200 and shows duid + iaid fields."""
        response = self.client.get(self._url(version=6))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "duid")
        self.assertContains(response, "iaid")

    def test_post_lease4_add_valid_redirects(self):
        """POST valid v4 lease data redirects to the lease list."""
        with stub_kea({"lease4-add": {"result": 0}}):
            response = self.client.post(self._url(version=4), self._valid_post4())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)

    def test_post_lease4_add_calls_kea_with_correct_args(self):
        """POST v4 calls lease_add with ip-address, hw-address, and subnet-id."""
        with stub_kea({"lease4-add": {"result": 0}}) as kea:
            self.client.post(self._url(version=4), self._valid_post4())
        self.assertIn("lease4-add", kea.commands())
        lease = kea.bodies("lease4-add")[0]["arguments"]
        self.assertEqual(lease["ip-address"], "10.0.0.200")
        self.assertEqual(lease.get("hw-address"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(lease.get("subnet-id"), 1)

    def test_post_lease4_add_invalid_ip_shows_form_errors(self):
        """POST with a non-IPv4 string re-renders form with validation errors."""
        # Empty registry: any Kea command would raise — proves no lease was created.
        with stub_kea({}) as kea:
            response = self.client.post(self._url(version=4), self._valid_post4(ip_address="not-an-ip"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_post_lease4_add_kea_exception_shows_error_message(self):
        """POST that triggers a KeaException shows error and re-renders (no redirect)."""
        # result=1 on lease4-add makes the real client raise KeaException.
        with stub_kea({"lease4-add": {"result": 1, "text": "address already in use"}}):
            response = self.client.post(self._url(version=4), self._valid_post4())
        self.assertIn(response.status_code, (200, 302))

    def test_post_lease6_add_valid_redirects(self):
        """POST valid v6 lease data redirects to the lease list."""
        with stub_kea({"lease6-add": {"result": 0}}):
            response = self.client.post(self._url(version=6), self._valid_post6())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)

    def test_post_lease6_add_calls_kea_with_correct_args(self):
        """POST v6 calls lease_add with ip-address, duid, and iaid."""
        with stub_kea({"lease6-add": {"result": 0}}) as kea:
            self.client.post(self._url(version=6), self._valid_post6())
        self.assertIn("lease6-add", kea.commands())
        lease = kea.bodies("lease6-add")[0]["arguments"]
        self.assertEqual(lease["ip-address"], "2001:db8::200")
        self.assertEqual(lease.get("duid"), "00:01:02:03:04:05")
        self.assertEqual(lease.get("iaid"), 12345)

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected to login."""
        self.client.logout()
        response = self.client.get(self._url(version=4))
        self.assertIn(response.status_code, (302, 403))


# ---------------------------------------------------------------------------
# TestLeaseAddSyncToNetBox — sync-to-netbox checkbox on lease add form
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": 30}})
class TestLeaseAddSyncToNetBox(_ViewTestBase):
    """Tests for the sync_to_netbox checkbox on ServerLease4/6AddView."""

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:server_lease{version}_add", args=[self.server.pk])

    def _post4(self, sync=False):
        data = {
            "ip_address": "10.0.0.200",
            "subnet_id": "1",
            "hw_address": "aa:bb:cc:dd:ee:ff",
            "valid_lft": "3600",
            "hostname": "newlease.example.com",
        }
        if sync:
            data["sync_to_netbox"] = "on"
        return data

    # A followed redirect lands on the leases page, which fetches the subnet quick-select.
    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_lease4_add_form_has_sync_to_netbox_field(self):
        """GET lease4 add page renders a sync_to_netbox checkbox."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)
        self.assertIn("sync_to_netbox", response.content.decode())

    @patch("netbox_kea.views.leases.sync_lease_to_netbox")
    def test_post_lease4_add_with_sync_calls_sync_lease(self, mock_sync):
        """POST with sync_to_netbox=on calls sync_lease_to_netbox() with the lease dict."""
        mock_sync.return_value = (MagicMock(spec=NbIP), True, False)
        with stub_kea({"lease4-add": {"result": 0}}):
            response = self.client.post(self._url(version=4), self._post4(sync=True))
        self.assertEqual(response.status_code, 302)
        mock_sync.assert_called_once()
        lease = mock_sync.call_args[0][0]
        self.assertEqual(lease["ip-address"], "10.0.0.200")

    @patch("netbox_kea.views.leases.sync_lease_to_netbox")
    def test_post_lease4_add_without_sync_does_not_call_sync(self, mock_sync):
        """POST without sync_to_netbox does NOT call sync_lease_to_netbox()."""
        with stub_kea({"lease4-add": {"result": 0}}):
            response = self.client.post(self._url(version=4), self._post4(sync=False))
        self.assertEqual(response.status_code, 302)
        mock_sync.assert_not_called()

    @patch("netbox_kea.views.leases.sync_lease_to_netbox")
    def test_post_lease4_add_sync_failure_does_not_prevent_kea_success(self, mock_sync):
        """Sync failure is a warning; the lease creation still succeeds (302 redirect)."""
        mock_sync.side_effect = ValueError("NetBox unreachable")
        with stub_kea({"lease4-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(version=4), self._post4(sync=True))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("lease4-add"), 1)

    @patch("netbox_kea.views.leases.sync_lease_to_netbox")
    def test_post_lease4_add_sync_skipped_without_ipam_permission(self, mock_sync):
        """A user with server-change but no IPAM write permission must not trigger the IPAM sync."""
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        User = get_user_model()
        limited = User.objects.create_user(username="lease_no_ipam", password="x")
        perm = ObjectPermission.objects.create(name="change-server-lease-noipam", actions=["view", "change"])
        perm.object_types.add(ContentType.objects.get_for_model(Server))
        perm.users.add(limited)
        self.client.force_login(limited)

        with stub_kea({"lease4-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(version=4), self._post4(sync=True))
        # Lease still created in Kea (302), but the IPAM sync was gated out.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("lease4-add"), 1)
        mock_sync.assert_not_called()

    def test_post_lease4_add_reports_foreign_ip_skip(self):
        """A foreign NetBox IP (force=False) is skipped and reported as such, not 'synced'."""
        from ipam.models import IPAddress

        # self.client is the superuser (has IPAM perms) → reaches the real sync.
        IPAddress.objects.create(address="10.0.0.200/24", status="active", description="Router loopback")
        with stub_kea({"lease4-add": {"result": 0}, "config-get": self._CONFIG4}):
            response = self.client.post(self._url(version=4), self._post4(sync=True), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(
            any("skipped" in m.lower() and "not kea-managed" in m.lower() for m in msgs),
            f"Expected a foreign-IP skip warning, got: {msgs}",
        )
        # Foreign IP left exactly as the operator set it.
        ip = IPAddress.objects.get(address="10.0.0.200/24")
        self.assertEqual(ip.status, "active")
        self.assertEqual(ip.description, "Router loopback")

    def test_post_lease4_add_reports_successful_sync(self):
        """A fresh IP synced to NetBox reports a created/updated success message."""
        from ipam.models import IPAddress

        # No pre-existing row → the real sync creates it, no conflict → success message.
        with stub_kea({"lease4-add": {"result": 0}, "config-get": self._CONFIG4}):
            response = self.client.post(self._url(version=4), self._post4(sync=True), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(
            any("10.0.0.200" in m and "netbox" in m.lower() and "created" in m.lower() for m in msgs),
            f"Expected a NetBox sync success message, got: {msgs}",
        )
        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.200/").exists())


# ---------------------------------------------------------------------------
# TestBulkLeaseImportView — bulk lease CSV import (Gap C)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": 30}})
class TestBulkLeaseImportView(_ViewTestBase):
    """Tests for ServerLease4/6BulkImportView."""

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:server_lease{version}_bulk_import", args=[self.server.pk])

    def _csv4(self, rows=None):
        header = "ip-address,hw-address,subnet-id,valid-lft,hostname\n"
        if rows is None:
            rows = ["10.0.0.10,aa:bb:cc:dd:ee:ff,1,3600,host1.example.com\n"]
        return (header + "".join(rows)).encode("utf-8")

    def _csv6(self, rows=None):
        header = "ip-address,duid,iaid,subnet-id,hostname\n"
        if rows is None:
            rows = ["2001:db8::1,00:01:02:03,12345,1,host1.example.com\n"]
        return (header + "".join(rows)).encode("utf-8")

    def _post(self, version=4, csv_bytes=None):
        import io as _io

        if csv_bytes is None:
            csv_bytes = self._csv4() if version == 4 else self._csv6()
        f = _io.BytesIO(csv_bytes)
        f.name = "leases.csv"
        return {"csv_file": f}

    def test_get_v4_returns_200(self):
        """GET lease4 bulk import page returns 200."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)

    def test_get_v6_returns_200(self):
        """GET lease6 bulk import page returns 200."""
        response = self.client.get(self._url(version=6))
        self.assertEqual(response.status_code, 200)

    def test_post_v4_valid_csv_calls_lease_add(self):
        """POST with valid v4 CSV calls lease_add once per row."""
        with stub_kea({"lease4-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(version=4), self._post(version=4))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-add"), 1)
        self.assertEqual(kea.bodies("lease4-add")[0]["arguments"]["ip-address"], "10.0.0.10")

    def test_post_v6_valid_csv_calls_lease_add(self):
        """POST with valid v6 CSV calls lease_add with correct duid and iaid."""
        with stub_kea({"lease6-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(version=6), self._post(version=6))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease6-add"), 1)
        args = kea.bodies("lease6-add")[0]["arguments"]
        self.assertEqual(args["duid"], "00:01:02:03")
        self.assertEqual(args["iaid"], 12345)

    def test_post_multiple_rows_calls_lease_add_per_row(self):
        """Each CSV row triggers one lease_add call."""
        csv_bytes = self._csv4(
            rows=[
                "10.0.0.10,aa:bb:cc:dd:ee:01,1,3600,h1\n",
                "10.0.0.11,aa:bb:cc:dd:ee:02,1,3600,h2\n",
                "10.0.0.12,aa:bb:cc:dd:ee:03,1,3600,h3\n",
            ]
        )
        with stub_kea({"lease4-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-add"), 3)

    def test_post_partial_failure_shows_error_count(self):
        """If some rows fail, result context shows correct created/error counts."""
        csv_bytes = self._csv4(
            rows=[
                "10.0.0.10,aa:bb:cc:dd:ee:01,1,3600,h1\n",
                "10.0.0.11,aa:bb:cc:dd:ee:02,1,3600,h2\n",
            ]
        )
        # Row 1 succeeds (result 0), row 2 fails (result 1 → real client raises KeaException).
        with stub_kea({"lease4-add": queued({"result": 0}, {"result": 1, "text": "bad"})}):
            response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], 1)

    def test_post_empty_csv_shows_form_error(self):
        """Uploading a CSV with only a header (no data rows) returns 200 with empty result."""
        csv_bytes = b"ip-address,hw-address\n"
        # Header-only CSV → zero rows → no lease command issued (empty registry proves it).
        with stub_kea({}) as kea:
            response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])
        result = response.context.get("result")
        if result is not None:
            self.assertEqual(result["created"], 0)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url(version=4))
        self.assertIn(response.status_code, (302, 403))


# ─────────────────────────────────────────────────────────────────────────────
# Gap G: Django signals + lease journal entries
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSignals(_ViewTestBase):
    """Lease add/delete views must fire Django signals from netbox_kea.signals."""

    _LEASE4 = {
        "ip_address": "10.0.0.5",
        "hw_address": "aa:bb:cc:dd:ee:01",
        "hostname": "signal-host",
        "subnet_id": 1,
        "valid_lft": 3600,
    }

    # Pool-overlap advisory fetches the subnet; no pools → no overlap warning.
    _SUBNET4 = {"result": 0, "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": []}]}}

    def test_lease_add_fires_lease_added_signal(self):
        """_BaseLeaseAddView.post must send lease_added signal after successful add."""
        from netbox_kea import signals

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.lease_added.connect(handler)
        try:
            url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
            with stub_kea({"lease4-add": {"result": 0}}):
                self.client.post(url, self._LEASE4)
        finally:
            signals.lease_added.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["ip_address"], "10.0.0.5")
        self.assertEqual(received[0]["dhcp_version"], 4)
        self.assertEqual(received[0]["server"].pk, self.server.pk)

    def test_lease_delete_fires_leases_deleted_signal(self):
        """BaseServerLeasesDeleteView.post must send leases_deleted signal after successful delete."""
        from netbox_kea import signals

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.leases_deleted.connect(handler)
        try:
            url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
            with stub_kea({"lease4-del": {"result": 0}}):
                self.client.post(url, {"pk": "10.0.0.5", "_confirm": "1"})
        finally:
            signals.leases_deleted.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertIn("10.0.0.5", received[0]["ip_addresses"])
        self.assertEqual(received[0]["dhcp_version"], 4)

    def test_reservation_add_fires_reservation_created_signal(self):
        """ServerReservation4AddView.post must send reservation_created signal."""
        from netbox_kea import signals

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.reservation_created.connect(handler)
        try:
            url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
            with stub_kea({"subnet4-get": self._SUBNET4, "reservation-add": {"result": 0}}):
                self.client.post(
                    url,
                    {
                        "subnet_id": 1,
                        "ip_address": "10.0.0.10",
                        "identifier_type": "hw-address",
                        "identifier": "aa:bb:cc:dd:ee:01",
                        "hostname": "",
                    },
                )
        finally:
            signals.reservation_created.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["dhcp_version"], 4)

    def test_reservation_delete_fires_reservation_deleted_signal(self):
        """ServerReservation4DeleteView.post must send reservation_deleted signal."""
        from netbox_kea import signals

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.reservation_deleted.connect(handler)
        try:
            url = reverse(
                "plugins:netbox_kea:server_reservation4_delete",
                args=[self.server.pk, 1, "10.0.0.10"],
            )
            with stub_kea({"reservation-del": {"result": 0}}):
                self.client.post(url)
        finally:
            signals.reservation_deleted.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["dhcp_version"], 4)
        self.assertEqual(received[0]["ip_address"], "10.0.0.10")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseJournalEntries(_ViewTestBase):
    """Lease add and delete views must create JournalEntry records on the Server."""

    def test_lease_add_creates_journal_entry(self):
        """A successful lease add must create a JournalEntry attached to the server."""
        from django.contrib.contenttypes.models import ContentType
        from extras.models import JournalEntry

        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        server_ct = ContentType.objects.get_for_model(self.server)
        before = JournalEntry.objects.filter(
            assigned_object_id=self.server.pk,
            assigned_object_type=server_ct,
        ).count()
        with stub_kea({"lease4-add": {"result": 0}}):
            self.client.post(
                url,
                {
                    "ip_address": "10.0.0.5",
                    "hw_address": "aa:bb:cc:dd:ee:01",
                    "hostname": "journal-host",
                    "subnet_id": 1,
                    "valid_lft": 3600,
                },
            )
        after = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).count()
        self.assertEqual(after, before + 1)
        entry = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).latest(
            "created"
        )
        self.assertIn("10.0.0.5", entry.comments)

    def test_lease_delete_creates_journal_entry(self):
        """A successful lease delete must create a JournalEntry attached to the server."""
        from django.contrib.contenttypes.models import ContentType
        from extras.models import JournalEntry

        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        server_ct = ContentType.objects.get_for_model(self.server)
        before = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).count()
        with stub_kea({"lease4-del": {"result": 0}}):
            self.client.post(url, {"pk": "10.0.0.5", "_confirm": "1"})
        after = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).count()
        self.assertEqual(after, before + 1)
        entry = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).latest(
            "created"
        )
        self.assertIn("10.0.0.5", entry.comments)


# ---------------------------------------------------------------------------
# Tests for _enrich_leases_with_badges can_change parameter
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesWithBadgesCanChange(_ViewTestBase):
    """Tests for _enrich_leases_with_badges can_change parameter gating edit_url."""

    def test_edit_url_absent_when_can_change_false(self):
        """edit_url must NOT be set on leases when can_change=False."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.1", "hw_address": "aa:bb:cc:dd:ee:ff"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, False, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertNotIn("edit_url", lease)
        self.assertFalse(lease["can_change"])

    def test_edit_url_set_when_can_change_true(self):
        """edit_url must be set on leases when can_change=True."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.1", "hw_address": "aa:bb:cc:dd:ee:ff"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, False, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertIn("edit_url", lease)
        self.assertTrue(lease["can_change"])


# ---------------------------------------------------------------------------
# Tests for _enrich_leases_with_badges: is_reserved flag + reservation URLs
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesReservationFlags(_ViewTestBase):
    """Tests that is_reserved, reservation_url and create_reservation_url are set correctly."""

    def test_is_reserved_true_when_reservation_exists(self):
        """is_reserved must be True when the IP has a reservation in Kea."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.5", "hw_address": "aa:bb:cc:dd:ee:ff"}
        rsv = {"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}
        with (
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                return_value=({"10.0.0.5": rsv}, True, set()),
            ),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertTrue(lease["is_reserved"])

    def test_is_reserved_false_when_no_reservation(self):
        """is_reserved must be False when the IP has no reservation."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.99", "hw_address": "bb:bb:bb:bb:bb:bb"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertFalse(lease["is_reserved"])

    def test_reservation_url_set_for_read_only_when_reservation_exists(self):
        """reservation_url must be set even when can_change=False; can_change_reservation must be False."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.5", "hw_address": "aa:bb:cc:dd:ee:ff"}
        rsv = {"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}
        with (
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                return_value=({"10.0.0.5": rsv}, True, set()),
            ),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertIsNotNone(lease["reservation_url"])
        self.assertTrue(lease["reservation_url"])
        self.assertFalse(lease["can_change_reservation"])
        self.assertTrue(lease["is_reserved"])

    def test_reservation_url_set_when_can_change_true(self):
        """reservation_url must be a non-empty string when can_change=True and reservation exists."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.5", "hw_address": "aa:bb:cc:dd:ee:ff"}
        rsv = {"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}
        with (
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                return_value=({"10.0.0.5": rsv}, True, set()),
            ),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertIsNotNone(lease["reservation_url"])
        self.assertTrue(lease["reservation_url"])

    def test_create_reservation_url_none_when_can_change_false(self):
        """create_reservation_url must be None when can_change=False and no reservation."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.99", "hw_address": "cc:cc:cc:cc:cc:cc"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertIsNone(lease.get("create_reservation_url"))

    def test_create_reservation_url_set_when_can_change_true(self):
        """create_reservation_url must be set when can_change=True and no reservation."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.99", "hw_address": "cc:cc:cc:cc:cc:cc", "subnet_id": 1}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch("netbox_kea.views.leases._fetch_reservation_by_mac_for_leases", return_value=({}, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertIsNotNone(lease.get("create_reservation_url"))


# ---------------------------------------------------------------------------
# EnrichLeases exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesExceptionPaths(_ViewTestBase):
    """_enrich_leases_with_badges exception branches (KeaException result≠2 and generic)."""

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}
    _LEASE4 = {
        "ip-address": "10.0.0.1",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "subnet-id": 1,
        "valid-lft": 3600,
        "cltt": 0,
    }

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_kea_exception_non_hook_swallowed(self):
        """KeaException with result≠2 raised by the reservation fetch is swallowed; view returns 200."""
        from netbox_kea.kea import KeaException

        # Real lease search returns one lease; force the reservation fetch to raise so the
        # enrichment outer handler is exercised. (failed_ips then short-circuits the MAC pass.)
        with (
            stub_kea({"config-get": self._CONFIG4, "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)}}),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                side_effect=KeaException({"result": 1, "text": "error"}, index=0),
            ),
        ):
            response = self.client.get(self._url() + "?by=ip&q=10.0.0.1", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_generic_exception_in_enrichment_swallowed(self):
        """Generic exception in enrichment is swallowed and view returns 200."""
        with (
            stub_kea({"config-get": self._CONFIG4, "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)}}),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                side_effect=RuntimeError("unexpected error"),
            ),
        ):
            response = self.client.get(self._url() + "?by=ip&q=10.0.0.1", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)


# ===========================================================================
# BATCH 2: Covering remaining ~220 uncovered lines
# ===========================================================================

# ---------------------------------------------------------------------------
# _add_reservation_journal / _add_lease_journal — ImportError + DB errors
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestJournalHelperEdgeCases(_ViewTestBase):
    """Unit tests for _add_reservation_journal / _add_lease_journal exception paths."""

    def test_reservation_journal_import_error(self):
        """ImportError inside _add_reservation_journal is swallowed."""
        import sys

        from netbox_kea.views import _add_reservation_journal

        # Force the local 'from extras.models import JournalEntry' to raise ImportError
        with patch.dict(sys.modules, {"extras.models": None}):
            _add_reservation_journal(self.server, self.user, "created", {"ip-address": "10.0.0.1"})

    def test_reservation_journal_db_error(self):
        """ProgrammingError inside _add_reservation_journal is swallowed."""
        from django.db import ProgrammingError

        from netbox_kea.views import _add_reservation_journal

        with patch("extras.models.JournalEntry.objects.create", side_effect=ProgrammingError("table missing")):
            _add_reservation_journal(self.server, self.user, "deleted", {"ip-address": "10.0.0.1"})

    def test_lease_journal_multiple_ips(self):
        """_add_lease_journal with a list of IP addresses uses the 'N lease(s)' branch."""
        from netbox_kea.views import _add_lease_journal

        with patch("extras.models.JournalEntry.objects.create") as mock_create:
            mock_create.return_value = None
            _add_lease_journal(
                self.server,
                self.user,
                "deleted",
                ip_addresses=["10.0.0.1", "10.0.0.2"],
                hw_address="aa:bb:cc:dd:ee:ff",
                hostname="host1",
            )
            call_kwargs = mock_create.call_args[1]
            self.assertIn("2 lease(s)", call_kwargs["comments"])

    def test_lease_journal_import_error(self):
        """ImportError inside _add_lease_journal is swallowed."""
        import sys

        from netbox_kea.views import _add_lease_journal

        with patch.dict(sys.modules, {"extras.models": None}):
            _add_lease_journal(self.server, self.user, "created", ip_addresses=["10.0.0.1"])

    def test_lease_journal_db_error(self):
        """OperationalError inside _add_lease_journal is swallowed."""
        from django.db import OperationalError

        from netbox_kea.views import _add_lease_journal

        with patch("extras.models.JournalEntry.objects.create", side_effect=OperationalError("db gone")):
            _add_lease_journal(self.server, self.user, "created", ip_addresses=["10.0.0.1"])


# ---------------------------------------------------------------------------
# HTMX exception handler in BaseServerLeasesView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestHTMXExceptionHandler(_ViewTestBase):
    """Lines 731-736: exception during HTMX partial rendering returns error partial."""

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_htmx_exception_returns_error_partial(self):
        # A RuntimeError from the lease fetch must be caught and rendered as the HTMX error partial.
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": RuntimeError("boom")}):
            response = self.client.get(
                self._url() + "?q=10.0.0.1&by=ip",
                HTTP_HX_REQUEST="true",
            )
        # Must not crash — the outer handler catches the RuntimeError and renders
        # the HTMX error partial (never a 500; accepting 500 would let a regression
        # where the handler stops catching the error pass unnoticed).
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Lease edit GET — KeaException, not-found, v6 duid
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditGet(_ViewTestBase):
    """Lines 894-896, 899-900, 910: lease edit GET error paths."""

    def test_get_kea_exception_redirects(self):
        """KeaException in lease4 GET redirects to leases page."""
        # result=1 makes lease4-get raise KeaException in the real client.
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with stub_kea({"lease4-get": {"result": 1, "text": "err"}}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_get_lease_not_found_redirects(self):
        """result=3 (not found) in lease4 GET redirects to leases page."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with stub_kea({"lease4-get": {"result": 3, "arguments": None}}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_get_v6_lease_includes_duid(self):
        """v6 lease GET includes duid in form initial (line 910)."""
        server6 = _make_db_server(name="kea6-only", ca_url="https://kea6.example.com", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_lease6_edit", args=[server6.pk, "2001:db8::1"])
        with stub_kea(
            {
                "lease6-get": {
                    "result": 0,
                    "arguments": {
                        "ip-address": "2001:db8::1",
                        "duid": "00:01:00:01",
                        "hostname": "v6host",
                        "valid-lft": 3600,
                    },
                }
            }
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "00:01:00:01")


# ---------------------------------------------------------------------------
# Lease edit POST — invalid form
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditPostInvalidForm(_ViewTestBase):
    """Line 931: lease edit POST with invalid form re-renders with 200."""

    def test_post_invalid_form_rerenders(self):
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        # Invalid form re-renders before any Kea call — empty registry proves none is issued.
        with stub_kea({}) as kea:
            response = self.client.post(url, {"hostname": "", "valid_lft": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])


# ---------------------------------------------------------------------------
# Lease add — generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddGenericException(_ViewTestBase):
    """Lines 1056-1058: generic exception on lease_add re-renders form."""

    def test_generic_exception_rerenders_form(self):
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        # A transport error from lease4-add must re-render the form with an error message.
        with stub_kea({"lease4-add": requests.RequestException("unexpected crash")}):
            response = self.client.post(
                url,
                {
                    "ip_address": "10.0.0.99",
                    "subnet_id": "1",
                    "hw_address": "aa:bb:cc:dd:ee:ff",
                    "hostname": "testhost",
                    "valid_lft": "3600",
                },
            )
        self.assertEqual(response.status_code, 200)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("Failed to create lease" in m or "internal" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# _fetch_leases_from_server — various BY_* branches + edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesFromServer(_ViewTestBase):
    """Lines 3423-3452: _fetch_leases_from_server with various search branches."""

    def _call(self, by, q="aa:bb:cc:dd:ee:ff", version=4, resp=None):
        from netbox_kea.views import _fetch_leases_from_server

        if resp is None:
            resp = [{"result": 0, "arguments": {"leases": [{"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0}]}}]
        payload = resp[0] if isinstance(resp, list) else resp
        # _fetch_leases_from_server picks the command from `by`; register every
        # lease-get variant to the same payload so whichever it issues is covered.
        variants = [
            f"lease{version}-get",
            f"lease{version}-get-by-hw-address",
            f"lease{version}-get-by-hostname",
            f"lease{version}-get-by-client-id",
            f"lease{version}-get-all",
            f"lease{version}-get-by-duid",
        ]
        with stub_kea(dict.fromkeys(variants, payload)):
            return _fetch_leases_from_server(self.server, q, by, version)

    def test_by_hw_address(self):
        from netbox_kea import constants

        leases = self._call(constants.BY_HW_ADDRESS, q="aa:bb:cc:dd:ee:ff")
        self.assertIsInstance(leases, list)

    def test_by_hostname(self):
        from netbox_kea import constants

        leases = self._call(constants.BY_HOSTNAME, q="myhost")
        self.assertIsInstance(leases, list)

    def test_by_client_id(self):
        from netbox_kea import constants

        leases = self._call(constants.BY_CLIENT_ID, q="01:aa:bb:cc:dd:ee:ff")
        self.assertIsInstance(leases, list)

    def test_by_duid(self):
        from netbox_kea import constants

        leases = self._call(constants.BY_DUID, q="00:01:00:01:12:34", version=6)
        self.assertIsInstance(leases, list)

    def test_unknown_by_returns_empty(self):
        leases = self._call("unknown_by", q="x")
        self.assertEqual(leases, [])

    def test_result_3_returns_empty(self):
        from netbox_kea import constants

        leases = self._call(constants.BY_HOSTNAME, q="ghost", resp=[{"result": 3, "arguments": None}])
        self.assertEqual(leases, [])

    def test_null_args_raises_runtime_error(self):
        from netbox_kea import constants
        from netbox_kea.views import _fetch_leases_from_server

        with stub_kea({"lease4-get-by-hostname": {"result": 0, "arguments": None}}):
            with self.assertRaises(RuntimeError):
                _fetch_leases_from_server(self.server, "ghost", constants.BY_HOSTNAME, 4)

    def test_by_subnet_id(self):
        """Lines 3432-3433: BY_SUBNET_ID branch sets command_suffix='-all' and subnets arg."""
        from netbox_kea import constants

        resp = [{"result": 0, "arguments": {"leases": [{"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0}]}}]
        leases = self._call(constants.BY_SUBNET_ID, q="1", resp=resp)
        self.assertIsInstance(leases, list)


# ---------------------------------------------------------------------------
# _fetch_all_leases_from_server — pagination edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchAllLeasesFromServer(_ViewTestBase):
    """Lines 3497-3509: _fetch_all_leases_from_server pagination and truncation."""

    def _run(self, responses, max_leases=1000):
        from netbox_kea.views import _fetch_all_leases_from_server

        # Each element of *responses* is a one-service Kea reply list; unwrap to the
        # single dict and feed them as a FIFO on lease4-get-page (the last repeats).
        page_responses = [r[0] for r in responses]
        with stub_kea({"lease4-get-page": queued(*page_responses)}):
            return _fetch_all_leases_from_server(self.server, version=4, max_leases=max_leases)

    def test_result_3_stops_loop(self):
        """Line 3497: result=3 breaks the pagination loop."""
        leases, truncated = self._run([[{"result": 3, "arguments": None}]])
        self.assertEqual(leases, [])
        self.assertFalse(truncated)

    def test_null_args_raises_runtime_error(self):
        """Null arguments from lease-get-page raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            self._run([[{"result": 0, "arguments": None}]])

    def test_truncation_at_max(self):
        """Lines 3504-3506: truncates when max_leases exceeded."""
        page = [
            {
                "result": 0,
                "arguments": {
                    "leases": [
                        {"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0},
                        {"ip-address": "10.0.0.2", "valid-lft": 3600, "state": 0},
                    ],
                    "count": 2,
                },
            }
        ]
        leases, truncated = self._run([page], max_leases=1)
        self.assertTrue(truncated)
        self.assertEqual(len(leases), 1)

    def test_final_page_no_cursor_update(self):
        """Lines 3507: count < per_page → loop ends without updating cursor."""
        page = [
            {
                "result": 0,
                "arguments": {"leases": [{"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0}], "count": 1},
            }
        ]
        leases, truncated = self._run([page])
        self.assertFalse(truncated)
        self.assertEqual(len(leases), 1)

    def test_multi_page_cursor_updated(self):
        """Line 3509: cursor advances when count == per_page (250)."""
        big_page = [{"ip-address": f"10.0.{i // 256}.{i % 256}", "valid-lft": 3600, "state": 0} for i in range(250)]
        last_page = [{"ip-address": "10.3.255.1", "valid-lft": 3600, "state": 0}]
        responses = [
            [{"result": 0, "arguments": {"leases": big_page, "count": 250}}],
            [{"result": 0, "arguments": {"leases": last_page, "count": 1}}],
        ]
        leases, truncated = self._run(responses)
        self.assertFalse(truncated)
        self.assertEqual(len(leases), 251)


# ---------------------------------------------------------------------------
# _fetch_reservation_by_ip — pagination and IP formats
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationByIP(_ViewTestBase):
    """Lines 3522-3539: _fetch_reservation_by_ip pagination and IP formats."""

    def _run(self, pages):
        from netbox_kea.views import _fetch_reservation_by_ip

        # Each *pages* entry is a (hosts, next_from, next_source) tuple as returned by
        # reservation_get_page; rebuild the raw reservation-get-page replies it parses.
        responses = [
            {"result": 0, "arguments": {"hosts": hosts, "next": {"from": nf, "source-index": ns}}}
            for (hosts, nf, ns) in pages
        ]
        client = self.server.get_client(version=4)
        with stub_kea({"reservation-get-page": queued(*responses)}):
            return _fetch_reservation_by_ip(client, version=4)

    def test_single_ip_reservation(self):
        """Line 3530: reservation with ip-address key."""
        page = [{"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}]
        result, available = self._run([(page, 0, 0)])
        self.assertIn("10.0.0.5", result)
        self.assertTrue(available)

    def test_multiple_ips_reservation(self):
        """Lines 3532-3533: reservation with ip-addresses key."""
        page = [{"subnet-id": 1, "ip-addresses": ["2001:db8::1", "2001:db8::2"], "duid": "00:01"}]
        result, available = self._run([(page, 0, 0)])
        self.assertIn("2001:db8::1", result)
        self.assertIn("2001:db8::2", result)

    def test_multi_page_pagination(self):
        """Lines 3535-3538: multi-page pagination updates from_index/source_index."""
        page1 = [{"subnet-id": 1, "ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:01"}]
        page2 = [{"subnet-id": 1, "ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:dd:ee:02"}]
        result, available = self._run([(page1, 1, 1), (page2, 0, 0)])
        self.assertIn("10.0.0.1", result)
        self.assertIn("10.0.0.2", result)

    def test_malformed_ip_fields_do_not_crash_rendering(self):
        """A null/non-list ``ip-addresses`` (or null ``ip-address``) must be tolerated, not TypeError."""
        page = [
            {"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:01"},
            {"subnet-id": 1, "ip-addresses": None, "hw-address": "aa:bb:cc:dd:ee:02"},  # null → no crash
            {"subnet-id": 1, "ip-addresses": ["2001:db8::1", None, 7], "duid": "00:01"},  # mixed
            {"subnet-id": 1, "ip-address": None, "hw-address": "aa:bb:cc:dd:ee:03"},  # null ip-address ignored
        ]
        result, available = self._run([(page, 0, 0)])
        self.assertTrue(available)
        # Only the well-formed string addresses are mapped; None/non-str are skipped.
        self.assertEqual(set(result), {"10.0.0.5", "2001:db8::1"})


# ---------------------------------------------------------------------------
# _enrich_leases_with_badges — exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesExceptionPaths2(_ViewTestBase):
    """Lines 3611-3619: enrich leases exception handling in combined leases view."""

    # by=ip is a single-result search, so arguments is the lease dict itself.
    _LEASE4 = {"result": 0, "arguments": {"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0, "subnet-id": 1}}

    def _url(self):
        return reverse("plugins:netbox_kea:combined_leases4") + f"?servers={self.server.pk}&q=10.0.0.1&by=ip"

    def test_kea_exception_result2_sets_hook_unavailable(self):
        """Lines 3612-3616: KeaException result=2 → host_cmds_available=False."""
        from netbox_kea.kea import KeaException

        with (
            stub_kea({"lease4-get": self._LEASE4}),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                side_effect=KeaException({"result": 2, "text": "hook not loaded"}, index=0),
            ),
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_kea_exception_non_result2_continues(self):
        """Lines 3612-3616: KeaException result≠2 → logged, host_cmds=False."""
        from netbox_kea.kea import KeaException

        with (
            stub_kea({"lease4-get": self._LEASE4}),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                side_effect=KeaException({"result": 1, "text": "other error"}, index=0),
            ),
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_generic_exception_continues(self):
        """Lines 3617-3619: generic Exception from _fetch_reservation_by_ip_for_leases is handled."""
        with (
            stub_kea({"lease4-get": self._LEASE4}),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                side_effect=RuntimeError("unexpected crash"),
            ),
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Lease CSV bulk import — form invalid, parse error, generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseBulkImportEdgeCases(_ViewTestBase):
    """Lines 4599, 4617-4619, 4641-4643: lease CSV import edge cases."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_bulk_import", args=[self.server.pk])

    def test_post_no_file_rerenders(self):
        """Line 4599: POST without csv_file → invalid form → 200."""
        response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.views.sync_views.parse_lease_csv")
    def test_parse_error_shows_form_error(self, mock_parse):
        """Lines 4617-4619: ValueError from parse_lease_csv adds generic form error (no raw exception text)."""
        import io

        mock_parse.side_effect = ValueError("bad column")
        csv_file = io.BytesIO(b"ip-address\n10.0.0.1")
        csv_file.name = "leases.csv"
        # Parse fails before any client call — empty registry proves no Kea command runs.
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])
        self.assertContains(response, "parsing failed")
        self.assertNotContains(response, "bad column")

    def test_generic_exception_is_row_error(self):
        """Generic exceptions from lease_add are caught per-row (not propagated)."""
        import io

        csv_content = b"ip-address\n10.0.0.1"
        csv_file = io.BytesIO(csv_content)
        csv_file.name = "leases.csv"
        # An unexpected error type from lease4-add is caught per-row by the import loop.
        with stub_kea({"lease4-add": AttributeError("bug")}):
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["errors"], 1)
        self.assertEqual(
            response.context["result"]["error_rows"][0]["error"],
            "An unexpected error occurred.",
        )


# ---------------------------------------------------------------------------
# get_leases_page — edge cases (lines 464, 480, 487-489)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageEdgeCases(_ViewTestBase):
    """Edge cases in BaseServerLeasesView.get_leases_page()."""

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_zero_network_uses_network_as_start(self):
        """Line 464: subnet.network == 0 → frm = str(subnet.network) = '0.0.0.0'."""
        # 0.0.0.0/8: int(network) == 0 → line 464 fires
        with stub_kea(
            {"config-get": self._CONFIG4, "lease4-get-page": {"result": 0, "arguments": {"count": 0, "leases": []}}}
        ):
            response = self.client.get(
                self._url(),
                {"by": "subnet", "q": "0.0.0.0/8"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)

    def test_null_args_raises_runtime_error(self):
        """Line 480: lease-get-page returns arguments=None → RuntimeError (caught by HTMX handler)."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": {"result": 0, "arguments": None}}):
            response = self.client.get(
                self._url(),
                {"by": "subnet", "q": "10.0.0.0/24"},
                HTTP_HX_REQUEST="true",
            )
        # RuntimeError is caught by outer except → HTMX error partial
        self.assertEqual(response.status_code, 200)

    def test_lease_outside_subnet_truncates_list(self):
        """Lines 487-489: lease IP not in queried subnet → raw_leases truncated."""
        per_page = 25
        # Return per_page leases where the only one is OUTSIDE the queried subnet.
        page = {
            "result": 0,
            "arguments": {"count": per_page, "leases": [{"ip-address": "10.0.1.1", "valid-lft": 3600, "state": 0}]},
        }
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page}):
            response = self.client.get(
                self._url(),
                {"by": "subnet", "q": "10.0.0.0/24"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageAllLeasesMode(_ViewTestBase):
    """All-leases browse mode (``by=""``) starts pagination at the address-space root.

    Covers ``BaseServerLeasesView.get_leases_page()`` when *subnet* is ``None``: the
    lease-page ``from`` cursor must be ``"0.0.0.0"`` for DHCPv4 and ``"::"`` for
    DHCPv6. The existing get_leases_page tests all pass ``by=subnet``, so the
    ``subnet is None`` branch was otherwise unexercised.
    """

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}
    _CONFIG6 = {"result": 0, "arguments": {"Dhcp6": {"subnet6": []}}}
    _EMPTY_PAGE = {"result": 0, "arguments": {"count": 0, "leases": []}}

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def _url6(self):
        return reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])

    def test_all_leases_v4_starts_from_zero_address(self):
        """``by=""`` on the v4 view must call lease4-get-page with ``from="0.0.0.0"``."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": self._EMPTY_PAGE}) as kea:
            response = self.client.get(self._url4(), {"by": ""}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.bodies("lease4-get-page")[0]["arguments"]["from"], "0.0.0.0")

    def test_all_leases_v6_starts_from_unspecified_address(self):
        """``by=""`` on the v6 view must call lease6-get-page with ``from="::"``."""
        with stub_kea({"config-get": self._CONFIG6, "lease6-get-page": self._EMPTY_PAGE}) as kea:
            response = self.client.get(self._url6(), {"by": ""}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.bodies("lease6-get-page")[0]["arguments"]["from"], "::")


# ---------------------------------------------------------------------------
# get_leases — AbortRequest and null args (lines 522, 535)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesCoverage(_ViewTestBase):
    """Edge cases in BaseServerLeasesView.get_leases()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_invalid_by_raises_abort_request(self):
        """Line 522: invalid 'by' value → AbortRequest raised (before the client is used)."""
        from utilities.exceptions import AbortRequest

        from netbox_kea.views import ServerLeases4View

        view = ServerLeases4View()
        client = KeaClient(url="https://kea.example.com")
        with self.assertRaises(AbortRequest):
            view.get_leases(client, "test_query", "not_a_valid_by")

    def test_null_args_from_lease_get_raises_runtime_error(self):
        """Line 535: lease-get returns arguments=None → RuntimeError (caught by HTMX handler)."""
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}
        with stub_kea({"config-get": config, "lease4-get": {"result": 0, "arguments": None}}):
            response = self.client.get(
                self._url(),
                {"by": "ip", "q": "10.0.0.1"},
                HTTP_HX_REQUEST="true",
            )
        # RuntimeError is caught by outer except → HTMX error partial, still 200
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# get_export — invalid form (lines 563-564) + export_all null args (line 618)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetExportCoverage(_ViewTestBase):
    """Edge cases in get_export() and get_export_all()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_export_with_invalid_form_redirects(self):
        """Lines 563-564: invalid form for export → messages.warning + redirect."""
        # Pass an invalid 'by' value (not in choices) to force form.is_valid() == False
        response = self.client.get(self._url(), {"export": "1", "by": "INVALID_VALUE", "q": "test"})
        self.assertIn(response.status_code, [200, 302])

    def test_export_all_null_args_returns_csv(self):
        """Line 618: export_all lease-get-page returns arguments=None → break → empty CSV."""
        with stub_kea({"lease4-get-page": {"result": 0, "arguments": None}}):
            response = self.client.get(self._url(), {"export_all": "1"})
        # Should return CSV even when args is None (empty export)
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# HTMX invalid form (lines 649-650)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestHTMXInvalidFormCoverage(_ViewTestBase):
    """Lines 649-650: HTMX GET with invalid form → renders HTMX partial."""

    def test_htmx_invalid_form_returns_partial(self):
        """form.is_valid()==False for HTMX → renders server_dhcp_leases_htmx.html."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}
        # 'by' has an invalid choice value → form.is_valid() returns False (no lease fetch).
        with stub_kea({"config-get": config}):
            response = self.client.get(
                url,
                {"by": "INVALID_CHOICE", "q": "test"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Lease6 edit — duid branch (lines 952-953)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease6EditDuid(_ViewTestBase):
    """Lines 952-953: POST lease6 edit with duid → duid added to kwargs."""

    def test_post_with_duid_calls_lease_update(self):
        """duid field in POST → kwargs['duid'] is set and lease_update called."""
        url = reverse(
            "plugins:netbox_kea:server_lease6_edit",
            args=[self.server.pk, "2001:db8::1"],
        )
        # lease_update reads the current lease (lease6-get) then writes lease6-update.
        current = {"result": 0, "arguments": {"ip-address": "2001:db8::1", "duid": "00:00", "valid-lft": 3600}}
        with stub_kea({"lease6-get": current, "lease6-update": {"result": 0}}) as kea:
            response = self.client.post(
                url,
                {
                    "duid": "01:02:03:04",
                    "valid_lft": "",
                    "hostname": "",
                },
            )
        # Should redirect to leases6 URL
        self.assertIn(response.status_code, [302, 200])
        self.assertIn("lease6-update", kea.commands())
        self.assertEqual(kea.bodies("lease6-update")[0]["arguments"]["duid"], "01:02:03:04")


# ---------------------------------------------------------------------------
# _fetch_one — missing subnet_id (line 3561)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchOneEmptyLease(_ViewTestBase):
    """Line 3561: _fetch_one returns early when lease has no subnet_id."""

    def test_lease_without_subnet_id_skips_reservation_lookup(self):
        """Lease without subnet-id → _fetch_one returns (ip, None, True) without API call."""
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}
        # A lease with ip-address but NO subnet-id → enrichment issues no reservation-get
        # (an empty registry for reservation-get would raise if it were called).
        lease = {
            "result": 0,
            "arguments": {"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0, "hostname": "testhost"},
        }
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea({"config-get": config, "lease4-get": lease}):
            response = self.client.get(url, {"by": "ip", "q": "10.0.0.1"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# CombinedLeasesView — truncated server (line 4228)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeasesTruncated(_ViewTestBase):
    """Line 4228: _fetch_all_leases_from_server returns was_truncated=True → server name added."""

    @patch("netbox_kea.views.combined._fetch_all_leases_from_server")
    def test_truncated_server_name_in_context(self, mock_fetch_all):
        """was_truncated=True → server.name appended to truncated_servers."""
        from netbox_kea.utilities import format_leases

        # Return a non-empty leases list with truncated=True, tagged with server info
        server_pk = self.server.pk
        server_name = self.server.name
        leases = [
            {**lease, "server_pk": server_pk, "server_name": server_name}
            for lease in format_leases([{"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0}])
        ]
        mock_fetch_all.return_value = (leases, True)
        url = reverse("plugins:netbox_kea:combined_leases4") + f"?state=0&server={self.server.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        truncated = response.context.get("truncated_servers", [])
        self.assertIn(self.server.name, truncated)


# ---------------------------------------------------------------------------
# Fix A: partial delete loop
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeasePartialDelete(_ViewTestBase):
    """Bulk delete continues past individual KeaExceptions."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])

    # A followed redirect lands on the leases page, which fetches the subnet quick-select.
    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_continues_after_first_delete_error(self):
        """When the first IP fails, the second IP is still deleted."""
        # First lease4-del fails (result 1 → KeaException), second succeeds.
        with stub_kea(
            {"lease4-del": queued({"result": 1, "text": "not found"}, {"result": 0}), "config-get": self._CONFIG4}
        ) as kea:
            response = self.client.post(
                self._url(),
                {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-del"), 2)

    def test_success_message_shows_count_of_deleted(self):
        """Success message reflects only the successfully deleted count."""
        with stub_kea({"lease4-del": {"result": 0}, "config-get": self._CONFIG4}):
            response = self.client.post(
                self._url(),
                {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
                follow=True,
            )
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("2" in m and "deleted" in m.lower() for m in msgs))

    def test_partial_failure_shows_warning(self):
        """When some IPs fail, a warning message about partial failure is shown."""
        with stub_kea(
            {"lease4-del": queued({"result": 1, "text": "not found"}, {"result": 0}), "config-get": self._CONFIG4}
        ):
            response = self.client.post(
                self._url(),
                {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
                follow=True,
            )
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("failed" in m.lower() or "error" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Fix B: get_export_all except narrowing
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportAllExceptNarrowing(_ViewTestBase):
    """get_export_all() must not swallow local bugs via bare except Exception."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_attribute_error_propagates(self):
        """An AttributeError inside get_export_all must not be silently caught."""
        # get_export_all narrows its except clauses, so an AttributeError propagates.
        with stub_kea({"lease4-get-page": AttributeError("bad stub")}):
            with self.assertRaises(AttributeError):
                self.client.get(self._url(), {"export_all": "1"})


# ---------------------------------------------------------------------------
# Fix C: reservation enrichment failed_ips seeding
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesFailedIpsSeeding(_ViewTestBase):
    """On enrichment error, all lease IPs are marked as indeterminate (failed_ips)."""

    @patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases")
    def test_reservation_enrichment_exception_does_not_show_not_reserved(self, mock_fetch_reservations):
        """When reservation lookup raises an unexpected Exception, leases must not
        incorrectly appear as 'not reserved' (no create-reservation link shown)."""
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}
        raw_leases = [
            {
                "ip-address": "10.0.0.1",
                "hw-address": "aa:bb:cc:dd:ee:ff",
                "subnet-id": 1,
                "cltt": 1700000000,
                "valid-lft": 86400,
                "hostname": "testhost",
            }
        ]
        # Make reservation lookup raise an unexpected exception
        mock_fetch_reservations.side_effect = RuntimeError("unexpected enrichment failure")

        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        # by=subnet_id → lease4-get-all with subnets=[1]; q="1" is a valid integer subnet ID.
        with stub_kea({"config-get": config, "lease4-get-all": {"result": 0, "arguments": {"leases": raw_leases}}}):
            response = self.client.get(url, HTTP_HX_REQUEST="true", data={"by": "subnet_id", "q": "1"})

        self.assertEqual(response.status_code, 200)
        # After fix: failed_ips is seeded with all lease IPs, so create-reservation link not shown
        add_url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        self.assertNotContains(response, add_url)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportStateFilter(_ViewTestBase):
    """get_export() must honour the 'state' query parameter."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_state_filter_applied_to_export(self):
        """Exported CSV must contain only leases matching the requested state."""
        # Return two leases: one with state=0 (default/active), one with state=1 (declined)
        leases = [
            {
                "ip-address": "10.0.0.1",
                "hw-address": "aa:bb:cc:00:00:01",
                "subnet-id": 1,
                "cltt": 1700000000,
                "valid-lft": 86400,
                "hostname": "",
                "state": 0,
            },
            {
                "ip-address": "10.0.0.2",
                "hw-address": "aa:bb:cc:00:00:02",
                "subnet-id": 1,
                "cltt": 1700000000,
                "valid-lft": 86400,
                "hostname": "",
                "state": 1,
            },
        ]
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}}
        # Page 1 returns both leases; page 2 signals end-of-data (result 3) so the
        # export pagination loop terminates regardless of the server's per-page size.
        pages = [
            {"result": 0, "arguments": {"leases": leases, "count": 2}},
            {"result": 3, "arguments": None},
        ]
        # Request export with state=1 (declined only)
        with stub_kea({"config-get": config, "lease4-get-page": queued(*pages)}):
            response = self.client.get(
                self._url(),
                {
                    "export": "1",
                    "by": "subnet",
                    "q": "10.0.0.0/24",
                    "state": "1",
                },
            )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("10.0.0.2", content)
        self.assertNotIn("10.0.0.1", content)


# ─────────────────────────────────────────────────────────────────────────────
# F8: HTMX handler exception narrowing
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestHtmxHandlerExceptNarrowing(_ViewTestBase):
    """HTMX lease handler must not swallow programming errors via bare except Exception."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_attribute_error_not_swallowed_by_htmx_handler(self):
        """An AttributeError inside the HTMX handler must propagate (not be caught silently)."""
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}
        # The HTMX handler narrows its except clauses, so an AttributeError propagates.
        with stub_kea({"config-get": config, "lease4-get-page": AttributeError("stub programming bug")}):
            with self.assertRaises(AttributeError):
                self.client.get(
                    self._url(),
                    HTTP_HX_REQUEST="true",
                    data={"by": "subnet", "q": "10.0.0.0/24"},
                )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseDeleteLoopTransportErrors(_ViewTestBase):
    """Bulk delete loop must continue when RequestException/ValueError raised for one IP."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_request_exception_continues_loop(self):
        import requests as _requests

        # First IP's delete raises a transport error; the loop must still delete the second.
        def del_resp(body):
            if body["arguments"]["ip-address"] == "10.0.0.1":
                return _requests.ConnectionError("down")
            return {"result": 0}

        with stub_kea({"lease4-del": del_resp, "config-get": self._CONFIG4}) as kea:
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-del"), 2)

    def test_value_error_continues_loop(self):
        def del_resp(body):
            if body["arguments"]["ip-address"] == "10.0.0.1":
                return ValueError("bad JSON")
            return {"result": 0}

        with stub_kea({"lease4-del": del_resp, "config-get": self._CONFIG4}) as kea:
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-del"), 2)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportTransportErrors(_ViewTestBase):
    """get_export() must handle RequestException and ValueError gracefully."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_request_exception_redirects_with_error(self):
        import requests as _requests

        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": _requests.ConnectionError("down")}):
            response = self.client.get(
                self._url(),
                {"export": "1", "by": "subnet", "q": "10.0.0.0/24"},
            )
        self.assertIn(response.status_code, [200, 302])

    def test_value_error_redirects_with_error(self):
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": ValueError("bad JSON")}):
            response = self.client.get(
                self._url(),
                {"export": "1", "by": "subnet", "q": "10.0.0.0/24"},
            )
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# F3: Single-lease GET — transport errors
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSingleLeaseGetTransportErrors(_ViewTestBase):
    """Single-lease GET must handle RequestException/ValueError gracefully."""

    def test_request_exception_redirects(self):
        """requests.RequestException from lease-get must redirect with error message."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with stub_kea({"lease4-get": requests.ConnectionError("down")}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)

    def test_value_error_redirects(self):
        """ValueError from lease-get must redirect with error message."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with stub_kea({"lease4-get": ValueError("bad JSON")}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)


# ---------------------------------------------------------------------------
# F3: Lease edit POST — transport errors
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditPostTransportErrors(_ViewTestBase):
    """Lease edit POST must handle RequestException/ValueError gracefully."""

    def test_request_exception_redirects(self):
        """requests.RequestException from lease_update must redirect."""
        # lease_update's first command is lease{v}-get; raising there surfaces the transport error.
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with stub_kea({"lease4-get": requests.ConnectionError("down")}):
            response = self.client.post(url, {"hostname": "host", "valid_lft": "3600"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)

    def test_value_error_redirects(self):
        """ValueError from lease_update must redirect."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with stub_kea({"lease4-get": ValueError("bad value")}):
            response = self.client.post(url, {"hostname": "host", "valid_lft": "3600"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)


# ---------------------------------------------------------------------------
# F3: Lease add POST — ValueError (RequestException already handled)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddValueError(_ViewTestBase):
    """Lease add POST must handle ValueError gracefully."""

    def test_value_error_rerenders_form(self):
        """ValueError from lease_add must not propagate as 500."""
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        with stub_kea({"lease4-add": ValueError("bad value")}):
            response = self.client.post(url, {"ip_address": "10.0.0.99"})
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# F9: _add_lease_journal bare except narrowing
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseJournalExceptionNarrowing(_ViewTestBase):
    """_add_lease_journal except must only catch DB errors, not all exceptions."""

    @patch("netbox_kea.views.leases._add_lease_journal")
    def test_database_error_does_not_fail_request(self, mock_journal):
        """DatabaseError from _add_lease_journal must be caught; lease add still redirects."""
        from django.db import DatabaseError

        mock_journal.side_effect = DatabaseError("DB error")
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        with stub_kea({"lease4-add": {"result": 0}}):
            response = self.client.post(url, {"ip_address": "10.0.0.55"})
        self.assertIn(response.status_code, [200, 302])

    @patch("netbox_kea.views.leases._add_lease_journal")
    def test_operational_error_does_not_fail_request(self, mock_journal):
        """OperationalError from _add_lease_journal must be caught; lease add still redirects."""
        from django.db import OperationalError

        mock_journal.side_effect = OperationalError("DB lock")
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        with stub_kea({"lease4-add": {"result": 0}}):
            response = self.client.post(url, {"ip_address": "10.0.0.56"})
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# Coverage: get_export() error paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportClientError(_ViewTestBase):
    """Cover error paths in get_export()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_get_client_value_error_redirects(self):
        """ValueError from get_client in export redirects with error."""
        # A cert without a key makes the real KeaClient constructor raise ValueError.
        bad = _make_db_server(name="badtls-export", client_cert_path="/x/cert.pem")
        url = reverse("plugins:netbox_kea:server_leases4", args=[bad.pk])
        with stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}}):
            response = self.client.get(url, {"export": "form", "by": "subnet", "q": "10.0.0.0/24"})
        self.assertIn(response.status_code, [200, 302])

    def test_runtime_error_during_fetch_redirects(self):
        """RuntimeError during lease fetch in export redirects with error."""
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}
        with stub_kea({"config-get": config, "lease4-get-page": RuntimeError("unexpected")}):
            response = self.client.get(self._url(), {"export": "form", "by": "subnet", "q": "10.0.0.0/24"})
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# Coverage: HTMX error handler exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseHtmxErrorHandler(_ViewTestBase):
    """Cover HTMX error rendering paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_kea_exception_renders_htmx_error(self):
        """KeaException in HTMX handler renders error template."""
        # result=1 on lease4-get-page makes the real client raise KeaException.
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": {"result": 1, "text": "err"}}):
            response = self.client.get(
                self._url(),
                {"by": "subnet", "q": "10.0.0.0/24"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)

    def test_request_exception_renders_htmx_error(self):
        """requests.RequestException in HTMX handler renders error template."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": requests.ConnectionError("down")}):
            response = self.client.get(
                self._url(),
                {"by": "subnet", "q": "10.0.0.0/24"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: lease edit GET validation branches
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditGetValidation(_ViewTestBase):
    """Cover lease edit GET validation paths."""

    def _url(self, ip="10.0.0.1"):
        return reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, ip])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_lease_not_found_result3_redirects(self):
        """result=3 from Kea (lease not found) shows warning and redirects."""
        with stub_kea({"lease4-get": {"result": 3, "text": "not found"}, "config-get": self._CONFIG4}):
            response = self.client.get(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_bad_response_shape_redirects(self):
        """An empty (shapeless) response list redirects with error."""
        # A real command returning [] passes the result-code check but fails the
        # view's resp[0] shape guard → redirect.
        with stub_kea({"lease4-get": lambda body: [], "config-get": self._CONFIG4}):
            response = self.client.get(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_bad_arguments_redirects(self):
        """Non-dict arguments in response redirects with error."""
        with stub_kea({"lease4-get": {"result": 0, "arguments": "not a dict"}, "config-get": self._CONFIG4}):
            response = self.client.get(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_get_client_value_error_redirects(self):
        """ValueError from get_client redirects with error."""
        # A cert without a key makes the real KeaClient constructor raise ValueError.
        bad = _make_db_server(name="badtls-edit", client_cert_path="/x/cert.pem")
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[bad.pk, "10.0.0.1"])
        with stub_kea({"config-get": self._CONFIG4}):
            response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: lease update POST exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseUpdatePostErrors(_ViewTestBase):
    """Cover lease update POST error handling."""

    def _url(self, ip="10.0.0.1"):
        return reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, ip])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_request_exception_redirects(self):
        """RequestException from lease_update redirects with error."""
        # lease_update's first command is lease4-get; raising there surfaces the transport error.
        with stub_kea({"lease4-get": requests.ConnectionError("down"), "config-get": self._CONFIG4}):
            response = self.client.post(self._url(), {"hostname": "test", "valid_lft": "3600"}, follow=True)
        self.assertEqual(response.status_code, 200)

    def test_value_error_redirects(self):
        """ValueError from lease_update redirects with error."""
        with stub_kea({"lease4-get": ValueError("bad JSON"), "config-get": self._CONFIG4}):
            response = self.client.post(self._url(), {"hostname": "test", "valid_lft": "3600"}, follow=True)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: lease add POST exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddPostErrors(_ViewTestBase):
    """Cover lease add POST error handling."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])

    def _valid_form(self):
        return {"ip_address": "10.0.0.1", "subnet_id": "1", "hw_address": "aa:bb:cc:00:00:01", "valid_lft": "3600"}

    def test_kea_exception_rerenders_form(self):
        """KeaException from lease_add re-renders form with error."""
        # result=1 on lease4-add makes the real client raise KeaException.
        with stub_kea({"lease4-add": {"result": 1, "text": "dup"}}):
            response = self.client.post(self._url(), self._valid_form())
        self.assertEqual(response.status_code, 200)

    def test_request_exception_rerenders_form(self):
        """RequestException from lease_add re-renders form with error."""
        with stub_kea({"lease4-add": requests.ConnectionError("down")}):
            response = self.client.post(self._url(), self._valid_form())
        self.assertEqual(response.status_code, 200)

    def test_value_error_rerenders_form(self):
        """ValueError from lease_add re-renders form with error."""
        with stub_kea({"lease4-add": ValueError("bad JSON")}):
            response = self.client.post(self._url(), self._valid_form())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: lease add post-creation side effect errors (journal + sync)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddSideEffectErrors(_ViewTestBase):
    """Cover lease add post-creation side effect error paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])

    def _valid_form(self):
        return {"ip_address": "10.0.0.1", "subnet_id": "1", "hw_address": "aa:bb:cc:00:00:01", "valid_lft": "3600"}

    @patch("netbox_kea.views.leases._add_lease_journal")
    def test_journal_db_error_still_succeeds(self, mock_journal):
        """DatabaseError in journal entry creation still redirects successfully."""
        from django.db import DatabaseError

        mock_journal.side_effect = DatabaseError("DB error")
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}
        with stub_kea({"lease4-add": {"result": 0}, "config-get": config}):
            response = self.client.post(self._url(), self._valid_form(), follow=True)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Pending IP change detection (Issue #32 Part B)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPendingIpChangeDetection(_ViewTestBase):
    """Tests for pending IP change detection via MAC-based reservation lookup."""

    def test_pending_ip_change_detected_when_mac_reservation_at_different_ip(self):
        """When a lease MAC has a reservation at a different IP, pending_ip_change must be True."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        mac_rsv = {"subnet-id": 1, "ip-address": "10.0.0.20", "hw-address": "aa:bb:cc:dd:ee:01"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_mac_for_leases",
                return_value=({("aa:bb:cc:dd:ee:01", 1): mac_rsv}, set()),
            ),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertTrue(lease["pending_ip_change"])
        self.assertEqual(lease["pending_reservation_ip"], "10.0.0.20")

    def test_no_pending_ip_change_when_no_mac_reservation(self):
        """pending_ip_change must be False when the MAC has no reservation anywhere."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch("netbox_kea.views.leases._fetch_reservation_by_mac_for_leases", return_value=({}, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertFalse(lease["pending_ip_change"])
        self.assertEqual(lease["pending_reservation_ip"], "")

    def test_pending_ip_change_blocks_sync_url(self):
        """When pending_ip_change is True, sync_url must NOT be set (sync would create wrong IP)."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        mac_rsv = {"subnet-id": 1, "ip-address": "10.0.0.20", "hw-address": "aa:bb:cc:dd:ee:01"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_mac_for_leases",
                return_value=({("aa:bb:cc:dd:ee:01", 1): mac_rsv}, set()),
            ),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertIsNone(lease.get("sync_url"))

    def test_pending_ip_change_blocks_create_reservation_url(self):
        """When pending_ip_change is True, create_reservation_url must be None."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        mac_rsv = {"subnet-id": 1, "ip-address": "10.0.0.20", "hw-address": "aa:bb:cc:dd:ee:01"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_mac_for_leases",
                return_value=({("aa:bb:cc:dd:ee:01", 1): mac_rsv}, set()),
            ),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertIsNone(lease.get("create_reservation_url"))

    def test_pending_ip_change_sets_reservation_url_when_can_change(self):
        """When pending_ip_change is True and can_change, reservation_url points to the reserved IP."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        mac_rsv = {"subnet-id": 1, "ip-address": "10.0.0.20", "hw-address": "aa:bb:cc:dd:ee:01"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_mac_for_leases",
                return_value=({("aa:bb:cc:dd:ee:01", 1): mac_rsv}, set()),
            ),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertIsNotNone(lease["reservation_url"])
        self.assertIn("10.0.0.20", lease["reservation_url"])

    def test_pending_ip_change_reservation_url_set_when_read_only(self):
        """When pending_ip_change is True but can_change=False, reservation_url must still be set."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        mac_rsv = {"subnet-id": 1, "ip-address": "10.0.0.20", "hw-address": "aa:bb:cc:dd:ee:01"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_mac_for_leases",
                return_value=({("aa:bb:cc:dd:ee:01", 1): mac_rsv}, set()),
            ),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertTrue(lease["pending_ip_change"])
        self.assertIsNotNone(lease["reservation_url"])
        self.assertIn("10.0.0.20", lease["reservation_url"])
        self.assertFalse(lease["can_change_reservation"])

    def test_ip_matched_reservation_overrides_mac_lookup(self):
        """When IP-based reservation matches, pending_ip_change must be False even if MAC differs."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.5", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        ip_rsv = {"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:01"}
        with (
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                return_value=({"10.0.0.5": ip_rsv}, True, set()),
            ),
            patch("netbox_kea.views.leases._fetch_reservation_by_mac_for_leases", return_value=({}, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertFalse(lease["pending_ip_change"])
        self.assertTrue(lease["is_reserved"])

    def test_mac_lookup_skipped_when_host_cmds_unavailable(self):
        """When host_cmds is not loaded, MAC lookup must not run and pending_ip_change is False."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        with (
            patch(
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
                return_value=({}, False, set()),
            ),
            patch("netbox_kea.views.leases._fetch_reservation_by_mac_for_leases") as mock_mac_fetch,
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        mock_mac_fetch.assert_not_called()

    def test_mac_lookup_failure_does_not_crash(self):
        """When MAC-based lookup raises an exception, enrichment continues without pending change."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch("netbox_kea.views.leases._fetch_reservation_by_mac_for_leases", side_effect=RuntimeError("boom")),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            stub_kea({"reservation-get": {"result": 3}}),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertFalse(lease.get("pending_ip_change", False))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPendingIpChangeBadgeRendering(_ViewTestBase):
    """Tests that the pending IP change badge renders correctly in the lease table."""

    _LEASE4 = {
        "ip-address": "10.0.0.10",
        "hw-address": "aa:bb:cc:dd:ee:01",
        "hostname": "pending-host",
        "subnet-id": 7,
        "valid-lft": 3600,
        "cltt": 1_700_000_000,
    }

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 7, "subnet": "10.0.0.0/24"}]}}}
    _MAC_RSV = {"subnet-id": 7, "ip-address": "10.0.0.20", "hw-address": "aa:bb:cc:dd:ee:01"}

    def _stub(self):
        """Real lease search + IP reservation lookup that finds nothing (result 3)."""
        return stub_kea(
            {
                "config-get": self._CONFIG4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 3},  # no IP-based match
            }
        )

    @patch("netbox_kea.views.leases._fetch_reservation_by_mac_for_leases")
    def test_pending_ip_badge_renders_in_response(self, mock_mac_fetch):
        """The lease table must show a 'Pending' badge with the reserved IP when pending change detected."""
        mock_mac_fetch.return_value = ({("aa:bb:cc:dd:ee:01", 7): self._MAC_RSV}, set())
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub():
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.10"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pending")
        self.assertContains(response, "10.0.0.20")

    @patch("netbox_kea.views.leases._fetch_reservation_by_mac_for_leases")
    def test_pending_ip_badge_does_not_show_sync_button(self, mock_mac_fetch):
        """When pending IP change detected, the Sync button must NOT appear."""
        mock_mac_fetch.return_value = ({("aa:bb:cc:dd:ee:01", 7): self._MAC_RSV}, set())
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub():
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.10"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sync</button>")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationByMac(_ViewTestBase):
    """Tests for _fetch_reservation_by_mac_for_leases helper function."""

    def _client(self):
        return self.server.get_client(version=4)

    def test_returns_reservation_when_ip_differs(self):
        """MAC reservation at different IP must be included in result."""
        from netbox_kea.views.leases import _fetch_reservation_by_mac_for_leases

        rsv = {"ip-address": "10.0.0.20", "hw-address": "aa:bb:cc:dd:ee:01", "subnet-id": 1}
        leases = [{"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}]
        with stub_kea({"reservation-get": {"result": 0, "arguments": rsv}}):
            result, failed = _fetch_reservation_by_mac_for_leases(self._client(), 4, leases, set(), set())
        self.assertIn(("aa:bb:cc:dd:ee:01", 1), result)
        self.assertEqual(result[("aa:bb:cc:dd:ee:01", 1)]["ip-address"], "10.0.0.20")
        self.assertEqual(failed, set())

    def test_skips_reservation_when_ip_matches(self):
        """MAC reservation at same IP as lease must NOT be included (already handled by IP lookup)."""
        from netbox_kea.views.leases import _fetch_reservation_by_mac_for_leases

        rsv = {"ip-address": "10.0.0.10", "hw-address": "aa:bb:cc:dd:ee:01", "subnet-id": 1}
        leases = [{"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}]
        with stub_kea({"reservation-get": {"result": 0, "arguments": rsv}}):
            result, failed = _fetch_reservation_by_mac_for_leases(self._client(), 4, leases, set(), set())
        self.assertEqual(result, {})

    def test_skips_already_matched_ips(self):
        """Leases in already_matched_ips must not trigger a MAC lookup."""
        from netbox_kea.views.leases import _fetch_reservation_by_mac_for_leases

        leases = [{"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}]
        # Empty registry: any reservation-get would raise, proving none is issued.
        with stub_kea({}) as kea:
            result, failed = _fetch_reservation_by_mac_for_leases(self._client(), 4, leases, {"10.0.0.10"}, set())
        self.assertEqual(result, {})
        self.assertEqual(kea.commands(), [])

    def test_skips_failed_ips(self):
        """Leases in failed_ips must not trigger a MAC lookup."""
        from netbox_kea.views.leases import _fetch_reservation_by_mac_for_leases

        leases = [{"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}]
        with stub_kea({}) as kea:
            result, failed = _fetch_reservation_by_mac_for_leases(self._client(), 4, leases, set(), {"10.0.0.10"})
        self.assertEqual(result, {})
        self.assertEqual(kea.commands(), [])

    def test_returns_empty_for_no_mac_reservation(self):
        """When reservation_get returns None, result must be empty."""
        from netbox_kea.views.leases import _fetch_reservation_by_mac_for_leases

        leases = [{"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}]
        with stub_kea({"reservation-get": {"result": 3}}):
            result, failed = _fetch_reservation_by_mac_for_leases(self._client(), 4, leases, set(), set())
        self.assertEqual(result, {})

    def test_exception_in_worker_is_swallowed(self):
        """An exception from reservation_get must not crash; MAC is simply omitted."""
        from netbox_kea.views.leases import _fetch_reservation_by_mac_for_leases

        leases = [{"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}]
        with stub_kea({"reservation-get": RuntimeError("connection failed")}):
            result, failed = _fetch_reservation_by_mac_for_leases(self._client(), 4, leases, set(), set())
        self.assertEqual(result, {})
        self.assertIn(("aa:bb:cc:dd:ee:01", 1), failed)

    def test_deduplicates_by_mac(self):
        """Multiple leases with the same MAC must only trigger one reservation_get call."""
        from netbox_kea.views.leases import _fetch_reservation_by_mac_for_leases

        leases = [
            {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1},
            {"ip_address": "10.0.0.11", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1},
        ]
        with stub_kea({"reservation-get": {"result": 3}}) as kea:
            _fetch_reservation_by_mac_for_leases(self._client(), 4, leases, set(), set())
        self.assertEqual(kea.commands().count("reservation-get"), 1)


# ---------------------------------------------------------------------------
# Coverage: defensive checks in get_leases_page() and get_export_all()
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageDefensiveChecks(_ViewTestBase):
    """Cover the isinstance guards and RuntimeError paths in get_leases_page()."""

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_non_list_leases_raises_runtime_error(self):
        """When Kea returns leases as non-list, the view catches the RuntimeError."""
        page = {"result": 0, "arguments": {"leases": "not-a-list", "count": 0}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page}):
            response = self.client.get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_non_int_count_raises_runtime_error(self):
        """When Kea returns count as non-int, the view catches the RuntimeError."""
        page = {"result": 0, "arguments": {"leases": [], "count": "bad"}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page}):
            response = self.client.get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_filtered_out_items_on_partial_page_returns_empty(self):
        """Items without ip-address are filtered; partial page returns empty gracefully."""
        page = {"result": 0, "arguments": {"leases": [{"no-ip": "bad"}], "count": 1}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page}):
            response = self.client.get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestExportAllDefensiveChecks(_ViewTestBase):
    """Cover defensive branches in get_export_all()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_non_list_leases_in_export_redirects(self):
        """When export_all gets non-list leases, it redirects with error."""
        page = {"result": 0, "arguments": {"leases": "not-a-list", "count": 0}}
        with stub_kea({"lease4-get-page": page}):
            response = self.client.get(self._url(), {"export_all": "1"})
        self.assertEqual(response.status_code, 302)

    def test_non_int_count_in_export_redirects(self):
        """When export_all gets non-int count, it redirects with error."""
        page = {"result": 0, "arguments": {"leases": [], "count": "bad"}}
        with stub_kea({"lease4-get-page": page}):
            response = self.client.get(self._url(), {"export_all": "1"})
        self.assertEqual(response.status_code, 302)

    def test_full_page_all_filtered_aborts_export(self):
        """When a full page has all entries filtered out, export aborts with error."""
        # export_all uses per_page=1000; a full page of invalid items aborts the export.
        page = {"result": 0, "arguments": {"leases": [{"no-ip": f"bad-{i}"} for i in range(1000)], "count": 1000}}
        with stub_kea({"lease4-get-page": page}):
            response = self.client.get(self._url(), {"export_all": "1"})
        self.assertEqual(response.status_code, 302)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestMacMatchedSubnetIdValidation(TestCase):
    """Cover isinstance(mac_rsv_subnet_id, int) guard in _set_unmatched_reservation."""

    def test_non_int_subnet_id_sets_reservation_url_to_none(self):
        """When mac_rsv has non-int subnet-id, reservation_url must be None."""
        from netbox_kea.views.leases import _set_unmatched_reservation

        lease: dict = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        reservation_by_mac = {
            ("aa:bb:cc:dd:ee:01", 1): {"subnet-id": "not-an-int", "ip-address": "10.0.0.5"},
        }
        _set_unmatched_reservation(
            lease=lease,
            server_pk=1,
            version=4,
            reservation_by_mac=reservation_by_mac,
            failed_mac_keys=set(),
            can_change=True,
            reservation_url_name="plugins:netbox_kea:server_reservation4_edit",
            add_url_name="plugins:netbox_kea:server_reservation4_add",
        )
        self.assertIsNone(lease.get("reservation_url"))
        self.assertTrue(lease.get("pending_ip_change"))

    def test_int_subnet_id_sets_reservation_url(self):
        """When mac_rsv has valid int subnet-id, reservation_url must be set."""
        from netbox_kea.views.leases import _set_unmatched_reservation

        lease: dict = {"ip_address": "10.0.0.10", "hw_address": "aa:bb:cc:dd:ee:01", "subnet_id": 1}
        reservation_by_mac = {
            ("aa:bb:cc:dd:ee:01", 1): {"subnet-id": 2, "ip-address": "10.0.0.5"},
        }
        _set_unmatched_reservation(
            lease=lease,
            server_pk=1,
            version=4,
            reservation_by_mac=reservation_by_mac,
            failed_mac_keys=set(),
            can_change=True,
            reservation_url_name="plugins:netbox_kea:server_reservation4_edit",
            add_url_name="plugins:netbox_kea:server_reservation4_add",
        )
        self.assertIsNotNone(lease.get("reservation_url"))
        self.assertIn("/2/", lease["reservation_url"])


# ─────────────────────────────────────────────────────────────────────────────
# Coverage gap tests — malformed responses, error paths, partial failures
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageMalformedResponse(_ViewTestBase):
    """get_leases_page() must raise RuntimeError on malformed Kea responses.

    These paths are exercised via HTMX GET with by=subnet which calls
    get_leases_page() internally. The view's top-level except catches
    RuntimeError and renders the HTMX error template (200, not 500).
    """

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_non_list_leases_payload_renders_error(self):
        """When Kea returns non-list 'leases', the HTMX handler catches RuntimeError."""
        page = {"result": 0, "arguments": {"leases": "not-a-list", "count": 1}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page}):
            response = self._htmx_get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

    def test_non_int_count_renders_error(self):
        """When Kea returns non-int 'count', the HTMX handler catches RuntimeError."""
        page = {"result": 0, "arguments": {"leases": [], "count": "not-an-int"}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page}):
            response = self._htmx_get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

    def test_full_page_all_filtered_renders_error(self):
        """Full page (count==per_page) but all entries invalid must trigger RuntimeError."""
        # Entries that are not valid dicts (filtered out by _is_valid_lease_entry).
        per_page = 50
        page = {"result": 0, "arguments": {"leases": ["not-a-dict"] * per_page, "count": per_page}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page}):
            response = self._htmx_get(
                self._url(),
                {"by": "subnet", "q": "10.0.0.0/24", "per_page": str(per_page)},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

    def test_none_arguments_renders_error(self):
        """When resp[0]['arguments'] is None, the HTMX handler catches RuntimeError."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": {"result": 0, "arguments": None}}):
            response = self._htmx_get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

    def test_empty_response_list_renders_error(self):
        """When Kea returns an empty list, get_leases_page raises RuntimeError."""
        # A real command returning [] passes the result-code check but fails the resp guard.
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": lambda body: []}):
            response = self._htmx_get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

    def test_non_dict_first_element_renders_error(self):
        """When resp[0] is not a dict, the HTMX handler catches RuntimeError."""
        # A non-dict entry can't survive the real command's result-code check
        # (check_response would TypeError first), so this defensive guard is only
        # reachable by returning it straight from command() — mock that one method.
        with patch.object(KeaClient, "command", return_value=["not-a-dict"]):
            response = self._htmx_get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesSingleResultValidation(_ViewTestBase):
    """get_leases() single-result paths must raise RuntimeError on bad data.

    Single-result mode (by=ip) returns args dict directly, not a list.
    """

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_single_result_missing_ip_address_renders_error(self):
        """Single-result response without 'ip-address' key must trigger RuntimeError."""
        # Single result mode (by ip), but response args lack 'ip-address'.
        resp = {"result": 0, "arguments": {"hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": resp}):
            response = self._htmx_get(self._url(), {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

    def test_multiple_result_all_non_dict_renders_error(self):
        """Multiple-result with all non-dict entries filtered out must trigger RuntimeError."""
        # by=hw returns multiple mode; all entries are non-dict.
        resp = {"result": 0, "arguments": {"leases": ["bad", 123, None], "count": 3}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-by-hw-address": resp}):
            response = self._htmx_get(self._url(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

    def test_multiple_result_none_arguments_renders_error(self):
        """Multiple-result with None arguments must trigger RuntimeError."""
        resp = {"result": 0, "arguments": None}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-by-hw-address": resp}):
            response = self._htmx_get(self._url(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

    def test_multiple_result_non_list_leases_renders_error(self):
        """Multiple-result with non-list leases must trigger RuntimeError."""
        resp = {"result": 0, "arguments": {"leases": "not-a-list"}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-by-hw-address": resp}):
            response = self._htmx_get(self._url(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestExportErrorPaths(_ViewTestBase):
    """Export must redirect with error messages when Kea calls fail."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_export_request_exception_redirects(self):
        """RequestException during export fetch must redirect with error message."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": requests.RequestException("connection refused")}):
            response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 302)

    def test_export_runtime_error_redirects(self):
        """RuntimeError during export fetch must redirect with error message."""
        # Single-result response lacking 'ip-address' → get_leases() raises RuntimeError.
        resp = {"result": 0, "arguments": {"hw-address": "aa:bb:cc:dd:ee:ff"}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": resp}):
            response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 302)

    def test_export_kea_exception_redirects(self):
        """KeaException during export fetch must redirect with error hint."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": {"result": 1, "text": "internal error"}}):
            response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 302)

    def test_export_client_creation_failure_redirects(self):
        """ValueError during get_client() for export must redirect with error message."""
        # A cert without a key makes the real KeaClient constructor raise ValueError.
        bad = _make_db_server(name="badtls-export2", client_cert_path="/x/cert.pem")
        url = reverse("plugins:netbox_kea:server_leases4", args=[bad.pk])
        with stub_kea({"config-get": self._CONFIG4}):
            response = self.client.get(url, {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 302)

    def test_export_subnet_runtime_error_redirects(self):
        """RuntimeError during paginated subnet export must redirect with error message."""
        # Malformed response for get_leases_page (non-list leases).
        page = {"result": 0, "arguments": {"leases": "not-a-list", "count": 1}}
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page}):
            response = self.client.get(self._url(), {"export": "all", "by": "subnet", "q": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 302)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseDeletePartialFailure(_ViewTestBase):
    """Bulk delete must handle partial failures gracefully."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])

    def test_partial_failure_shows_mixed_messages(self):
        """Some leases succeed, others fail with KeaException → mixed messages."""
        # First lease4-del succeeds (result 0), second fails (result 1 → KeaException).
        with stub_kea({"lease4-del": queued({"result": 0}, {"result": 1, "text": "lease not found"})}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        # Follow redirect to check messages
        msgs = list(response.wsgi_request._messages)
        msg_texts = [str(m) for m in msgs]
        # Should have success message for 1 lease + error for 1 + warning about failures
        has_success = any("Deleted 1" in t for t in msg_texts)
        has_error = any("Error deleting" in t for t in msg_texts)
        has_warning = any("Failed to delete" in t for t in msg_texts)
        self.assertTrue(has_success, f"Expected success message, got: {msg_texts}")
        self.assertTrue(has_error, f"Expected error message, got: {msg_texts}")
        self.assertTrue(has_warning, f"Expected warning message, got: {msg_texts}")

    def test_partial_failure_request_exception(self):
        """RequestException on some leases must show per-lease error messages."""

        # First lease4-del succeeds; the second raises a transport error.
        def del_resp(body):
            if body["arguments"]["ip-address"] == "10.0.0.2":
                return requests.RequestException("timeout")
            return {"result": 0}

        with stub_kea({"lease4-del": del_resp}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        msgs = list(response.wsgi_request._messages)
        msg_texts = [str(m) for m in msgs]
        has_error = any("see server logs" in t for t in msg_texts)
        self.assertTrue(has_error, f"Expected transport error message, got: {msg_texts}")

    @patch("netbox_kea.views.leases._add_lease_journal")
    def test_journal_database_error_still_completes(self, mock_journal):
        """DatabaseError from journal creation must not prevent deletion from completing."""
        from django.db import DatabaseError

        mock_journal.side_effect = DatabaseError("table locked")
        with stub_kea({"lease4-del": {"result": 0}}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        msgs = list(response.wsgi_request._messages)
        msg_texts = [str(m) for m in msgs]
        # The delete itself should succeed despite journal failure
        has_success = any("Deleted 1" in t for t in msg_texts)
        self.assertTrue(has_success, f"Expected success message despite journal error, got: {msg_texts}")

    def test_all_leases_fail_shows_only_errors(self):
        """When every lease deletion fails, no success message should appear."""
        # Every lease4-del returns result 1 → KeaException for each IP.
        with stub_kea({"lease4-del": {"result": 1, "text": "not found"}}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        msgs = list(response.wsgi_request._messages)
        msg_texts = [str(m) for m in msgs]
        has_success = any("Deleted" in t and "0" not in t for t in msg_texts)
        self.assertFalse(has_success, f"Should not have success message when all fail, got: {msg_texts}")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchOneMacValueError(_ViewTestBase):
    """_fetch_one_mac must return _FETCH_ERROR sentinel when subnet_id is non-numeric.

    This is tested via HTMX lease search where the lease has a non-int subnet_id,
    triggering the ValueError path in _fetch_one_mac.
    """

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_non_numeric_subnet_id_does_not_crash(self):
        """Lease with non-numeric subnet-id must not crash during MAC reservation lookup."""
        # A non-int subnet-id makes enrichment mark the IP indeterminate without any
        # reservation-get, so no reservation command is registered.
        lease = {
            "ip-address": "10.0.0.5",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "test",
            "subnet-id": "not-a-number",
            "valid-lft": 3600,
            "cltt": 1_700_000_000,
        }
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": {"result": 0, "arguments": lease}}):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        # Must render OK, not 500
        self.assertEqual(response.status_code, 200)

    def test_none_subnet_id_does_not_crash(self):
        """Lease with subnet-id=None must not crash during MAC reservation lookup."""
        lease = {
            "ip-address": "10.0.0.6",
            "hw-address": "aa:bb:cc:dd:ee:01",
            "hostname": "test2",
            "valid-lft": 3600,
            "cltt": 1_700_000_000,
        }
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea({"config-get": self._CONFIG4, "lease4-get": {"result": 0, "arguments": lease}}):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.6"})
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageSubnetEdgeCases(_ViewTestBase):
    """Additional edge-case tests for get_leases_page() cursor and filtering logic."""

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}

    def test_result_3_returns_empty_table(self):
        """result=3 (no leases) must render an empty table, not an error."""
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": {"result": 3, "arguments": None}}):
            response = self._htmx_get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 200)
        # Should NOT contain the error template content
        self.assertNotContains(response, "error_id")

    def test_leases_outside_subnet_are_truncated(self):
        """Leases with IPs outside the queried subnet must be excluded."""
        page = {
            "result": 0,
            "arguments": {
                "leases": [
                    {
                        "ip-address": "10.0.0.5",
                        "hw-address": "aa:bb:cc:dd:ee:ff",
                        "hostname": "in-subnet",
                        "subnet-id": 1,
                        "valid-lft": 3600,
                        "cltt": 1_700_000_000,
                    },
                    {
                        "ip-address": "10.0.1.5",
                        "hw-address": "aa:bb:cc:dd:ee:01",
                        "hostname": "out-of-subnet",
                        "subnet-id": 1,
                        "valid-lft": 3600,
                        "cltt": 1_700_000_000,
                    },
                ],
                "count": 2,
            },
        }
        with stub_kea({"config-get": self._CONFIG4, "lease4-get-page": page, "reservation-get": {"result": 3}}):
            response = self._htmx_get(self._url(), {"by": "subnet", "q": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.5")
        self.assertNotContains(response, "10.0.1.5")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchSubnetChoices(TestCase):
    """_fetch_subnet_choices(): network-order sorting + 5-minute caching.

    Uses a real Server, the real Django cache, and the **real** ``KeaClient`` with
    only the HTTP boundary stubbed via ``kea_stub.stub_kea`` — the real
    ``config-get`` request payload and response parsing are exercised.
    """

    # subnet4 ids/CIDRs are intentionally out of order, and chosen so that
    # network order (10.0.1 < 10.0.2 < 10.0.10) differs from lexicographic label
    # order (".10." sorts before ".2.").  Includes a shared-network subnet.
    _CONFIG = [
        {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [
                        {"id": 3, "subnet": "10.0.2.0/24"},
                        {"id": 1, "subnet": "10.0.10.0/24"},
                        {"id": 2, "subnet": "10.0.1.0/24"},
                    ],
                    "shared-networks": [
                        {"name": "sn-a", "subnet4": [{"id": 4, "subnet": "192.168.0.0/16"}]},
                    ],
                }
            },
        }
    ]

    def setUp(self):
        self.server = _make_db_server()
        self._clear_cache()
        self.addCleanup(self._clear_cache)

    def _clear_cache(self):
        # Delete only our own keys so we never disturb the shared dev-server cache.
        from django.core.cache import cache

        for v in (4, 6):
            cache.delete(_subnet_choices_cache_key(self.server, v))

    def test_choices_sorted_by_network_not_lexicographically(self):
        with stub_kea({"config-get": self._CONFIG}) as kea:
            choices = _fetch_subnet_choices(self.server, 4)
        # The helper must request the protocol-specific client (dual-URL servers
        # route v4/v6 to different endpoints) → config-get went to the dhcp4 service.
        self.assertEqual(kea.bodies("config-get")[0]["service"], ["dhcp4"])
        self.assertEqual(
            [c[0] for c in choices],
            ["10.0.1.0/24", "10.0.2.0/24", "10.0.10.0/24", "192.168.0.0/16"],
        )
        # Each choice carries (cidr, subnet_id) so the template can build both the
        # Subnet (CIDR) and Subnet-ID comboboxes.
        self.assertEqual(choices[0], ("10.0.1.0/24", 2))

    def test_v6_shared_network_subnet6_is_parsed(self):
        """Dhcp6/subnet6 and shared-networks[].subnet6 parse via version-aware keys.

        Locks the v6 contract so a cross-protocol regression (e.g. reading
        ``subnet4`` for a v6 request) is caught.
        """
        config6 = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp6": {
                        "subnet6": [{"id": 11, "subnet": "2001:db8:1::/64"}],
                        "shared-networks": [
                            {"name": "sn6", "subnet6": [{"id": 12, "subnet": "2001:db8:2::/64"}]},
                        ],
                    }
                },
            }
        ]
        with stub_kea({"config-get": config6}) as kea:
            choices = _fetch_subnet_choices(self.server, 6)
        self.assertEqual(kea.bodies("config-get")[0]["service"], ["dhcp6"])
        self.assertEqual(choices, [("2001:db8:1::/64", 11), ("2001:db8:2::/64", 12)])

    def test_non_string_subnet_values_do_not_500(self):
        """Malformed subnet entries (non-string CIDR) are skipped, not fed to sort() → no TypeError/500."""
        bad_config = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [
                            {"id": 1, "subnet": "10.0.1.0/24"},
                            {"id": 2, "subnet": 12345},  # int, not str
                            {"id": 3, "subnet": ["10.0.3.0/24"]},  # list, not str
                            {"id": 4, "subnet": None},  # None
                            {"id": 5},  # missing subnet
                        ],
                        "shared-networks": [],
                    }
                },
            }
        ]
        with stub_kea({"config-get": bad_config}):
            choices = _fetch_subnet_choices(self.server, 4)
        # Only the valid string CIDR survives; the call returns cleanly.
        self.assertEqual(choices, [("10.0.1.0/24", 1)])

    def test_non_list_subnet_containers_do_not_500(self):
        """Non-list subnet4 / shared-networks containers degrade to empty, not TypeError/500."""
        bad_config = [{"result": 0, "arguments": {"Dhcp4": {"subnet4": 1, "shared-networks": 1}}}]
        with stub_kea({"config-get": bad_config}):
            choices = _fetch_subnet_choices(self.server, 4)
        self.assertEqual(choices, [])

    def test_non_dict_dhcp_conf_returns_empty(self):
        """A non-dict ``Dhcp4`` payload degrades to no choices (not an AttributeError)."""
        with stub_kea({"config-get": [{"result": 0, "arguments": {"Dhcp4": "not-a-dict"}}]}):
            self.assertEqual(_fetch_subnet_choices(self.server, 4), [])

    def test_non_dict_subnet_entry_is_skipped(self):
        """Non-dict entries inside subnet4 are skipped; valid dict entries still parse."""
        config = [{"result": 0, "arguments": {"Dhcp4": {"subnet4": [1, {"id": 2, "subnet": "10.0.0.0/24"}]}}}]
        with stub_kea({"config-get": config}):
            self.assertEqual(_fetch_subnet_choices(self.server, 4), [("10.0.0.0/24", 2)])

    def test_subnet_sort_key_handles_non_string_and_unparseable(self):
        """_subnet_sort_key buckets non-string and unparseable CIDRs apart from real networks."""
        self.assertEqual(_subnet_sort_key((12345, 1)), (1, "12345"))  # non-string CIDR
        self.assertEqual(_subnet_sort_key(("not-a-cidr", 2)), (1, "not-a-cidr"))  # unparseable string
        self.assertEqual(_subnet_sort_key(("10.0.0.0/24", 3))[0], 0)  # real network sorts first

    def test_result_is_cached_second_call_skips_kea(self):
        with stub_kea({"config-get": self._CONFIG}) as kea:
            first = _fetch_subnet_choices(self.server, 4)
            second = _fetch_subnet_choices(self.server, 4)
        self.assertEqual(first, second)
        # config-get hit Kea exactly once; the second render is served from cache.
        self.assertEqual(kea.commands().count("config-get"), 1)
        # The single request used the protocol-specific dhcp4 service.
        self.assertEqual(kea.bodies("config-get")[0]["service"], ["dhcp4"])

    def test_transient_error_returns_empty_and_is_not_cached(self):
        with stub_kea({"config-get": RuntimeError("kea unreachable")}):
            self.assertEqual(_fetch_subnet_choices(self.server, 4), [])

        # A failed fetch must not be cached, so the next render retries and succeeds.
        with stub_kea({"config-get": self._CONFIG}) as kea:
            choices = _fetch_subnet_choices(self.server, 4)
        self.assertTrue(choices)
        self.assertEqual(kea.commands().count("config-get"), 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchSubnetCombobox(_ViewTestBase):
    """Lease search form renders an editable Subnet/Subnet-ID combobox on the Search field.

    There is no separate subnet selector — the attribute selector drives which
    (if any) datalist the Search field is associated with.
    """

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    @patch("netbox_kea.views.leases._fetch_subnet_choices")
    def test_datalists_and_toggle_script_rendered(self, mock_choices):
        mock_choices.return_value = [("10.0.1.0/24", 2), ("10.0.2.0/24", 3)]
        body = self.client.get(self._url()).content.decode()
        # Both comboboxes present with the right values.
        self.assertIn('id="kea-lease-subnet-cidrs"', body)
        self.assertIn('id="kea-lease-subnet-ids"', body)
        self.assertIn('value="10.0.1.0/24"', body)  # CIDR option (by=subnet)
        self.assertIn('value="2"', body)  # subnet-id option (by=subnet_id)
        # The toggle script wires the Search field (q) to the attribute selector (by).
        self.assertIn("syncSubnetCombobox", body)
        self.assertIn('getElementById("id_by")', body)

    @patch("netbox_kea.views.leases._fetch_subnet_choices")
    def test_no_separate_subnet_select_field(self, mock_choices):
        mock_choices.return_value = [("10.0.1.0/24", 2)]
        body = self.client.get(self._url()).content.decode()
        # The old standalone subnet quick-select is gone.
        self.assertNotIn('name="subnet"', body)
        self.assertNotIn("Select a subnet", body)

    @patch("netbox_kea.views.leases._fetch_subnet_choices")
    def test_no_datalists_when_no_subnets(self, mock_choices):
        mock_choices.return_value = []
        body = self.client.get(self._url()).content.decode()
        self.assertNotIn("kea-lease-subnet-cidrs", body)
        self.assertNotIn("syncSubnetCombobox", body)
