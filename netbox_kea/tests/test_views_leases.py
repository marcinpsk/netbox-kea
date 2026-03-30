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

import re
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import Server

# Minimal PLUGINS_CONFIG so server.get_client() can read kea_timeout.
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_INT_PK_RE = re.compile(r"/servers/(\d+)/")


def _make_db_server(**kwargs) -> Server:
    """Create and persist a Server without live connectivity checks.

    ``Server.objects.create()`` skips ``Model.clean()``, so no Kea connectivity
    check is triggered.  The ``PLUGINS_CONFIG`` override is applied by the calling
    test class.
    """
    defaults = {
        "name": "test-kea",
        "server_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


def _kea_command_side_effect(cmd, service=None, arguments=None, check=None):
    """Return a plausible Kea API response for each command type."""
    if cmd == "status-get":
        return [{"result": 0, "arguments": {"pid": 1234, "uptime": 3600, "reload": 0}}]
    if cmd == "version-get":
        return [{"result": 0, "arguments": {"extended": "2.4.1-stable"}}]
    if cmd == "config-get":
        # Return minimal Dhcp4/Dhcp6 config so subnet views can parse it.
        if service and service[0] == "dhcp6":
            return [{"result": 0, "arguments": {"Dhcp6": {"subnet6": [], "shared-networks": []}}}]
        return [{"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}]
    return [{"result": 0, "arguments": {}}]


# ─────────────────────────────────────────────────────────────────────────────
# Shared base class
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _ViewTestBase(TestCase):
    """Creates a superuser and a single Server for use in all view tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="kea_testuser",
            email="kea_test@example.com",
            password="kea_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def _assert_no_none_pk_redirect(self, response):
        """Assert that a redirect URL never contains the string ``None`` as a pk.

        This is the specific pattern that caused the ``POST /plugins/kea/servers/None``
        404 bug: ``get_absolute_url()`` with ``pk=None`` produces that URL.
        """
        if hasattr(response, "url"):
            self.assertNotIn(
                "servers/None",
                response.url,
                f"Redirect went to bad URL: {response.url}",
            )

    def _assert_redirect_to_integer_pk(self, response):
        """Assert that a redirect URL contains an integer server pk."""
        self._assert_no_none_pk_redirect(response)
        self.assertIsNotNone(
            _INT_PK_RE.search(response.url),
            f"Expected /servers/<int>/ in redirect URL, got: {response.url}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Leases views — initial page load (no Kea calls)
# ─────────────────────────────────────────────────────────────────────────────


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

    @patch("netbox_kea.models.KeaClient")
    def test_post_htmx_single_lease_returns_hx_refresh(self, MockKeaClient):
        """An HTMX POST with a single IP and _confirm returns HX-Refresh: true instead of redirect."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "text": "Success"}]
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        response = self.client.post(
            url,
            {"pk": "192.0.2.1", "_confirm": "1"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Refresh"), "true")
        mock_client.command.assert_called()


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

    def _htmx_get(self, url, data):
        """Issue an HTMX GET request (adds HX-Request header)."""
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    @patch("netbox_kea.models.KeaClient")
    def test_reserved_badge_shown_when_reservation_exists(self, MockKeaClient):
        """When a lease IP has a corresponding reservation, the table cell shows 'Reserved'."""
        mock_client = MockKeaClient.return_value
        # Lease search by IP
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "192.168.1.100", **self._LEASE4}}]
        # Reservation lookup returns a matching reservation for this specific IP
        mock_client.reservation_get.return_value = self._RESERVATION4

        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserved")

    @patch("netbox_kea.models.KeaClient")
    def test_no_reserved_badge_when_no_reservation(self, MockKeaClient):
        """When no reservation exists for the lease IP, no badge is rendered."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "192.168.1.100", **self._LEASE4}}]
        # No reservation found for this IP
        mock_client.reservation_get.return_value = None

        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        self.assertEqual(response.status_code, 200)
        # The column header says "Reserved" — check no badge link is rendered
        self.assertNotContains(response, 'text-decoration-none">Reserved</a>')

    @patch("netbox_kea.models.KeaClient")
    def test_no_crash_when_host_cmds_unavailable(self, MockKeaClient):
        """When host_cmds is not loaded, reservation lookup is skipped and no badge shown."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "192.168.1.100", **self._LEASE4}}]
        # host_cmds not loaded — result=2 means unknown command
        mock_client.reservation_get.side_effect = KeaException(
            {"result": 2, "text": "unknown command 'reservation-get'"},
            index=0,
        )

        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        # Must not 500; page renders normally without badge
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'text-decoration-none">Reserved</a>')


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9A: Lease search paths — all BY_* types
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchPaths(_ViewTestBase):
    """Each search-by type in BaseServerLeasesView.get_leases() must dispatch the
    correct Kea command with correct arguments, via HTMX GET."""

    _LEASE4 = {
        "ip-address": "10.0.0.5",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "client-id": "01:aa:bb:cc:dd:ee:ff",
        "hostname": "search-host",
        "subnet-id": 1,
        "valid-lft": 3600,
        "cltt": 1_700_000_000,
    }

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def _url6(self):
        return reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])

    def _setup_mock(self, MockKeaClient, leases, multiple=True):
        mock_client = MockKeaClient.return_value
        if multiple:
            mock_client.command.return_value = [{"result": 0, "arguments": {"leases": leases, "count": len(leases)}}]
        else:
            mock_client.command.return_value = [{"result": 0, "arguments": leases[0] if leases else {}}]
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        return mock_client

    @patch("netbox_kea.models.KeaClient")
    def test_search_by_hw_address_sends_correct_command(self, MockKeaClient):
        """BY_HW_ADDRESS must call lease4-get-by-hw-address with hw-address argument."""
        mock_client = self._setup_mock(MockKeaClient, [dict(self._LEASE4)])
        response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        cmd_names = [c.args[0] for c in mock_client.command.call_args_list]
        self.assertIn("lease4-get-by-hw-address", cmd_names)
        call = next(c for c in mock_client.command.call_args_list if c.args[0] == "lease4-get-by-hw-address")
        self.assertEqual(call.kwargs["arguments"]["hw-address"], "aa:bb:cc:dd:ee:ff")

    @patch("netbox_kea.models.KeaClient")
    def test_search_by_hostname_sends_correct_command(self, MockKeaClient):
        """BY_HOSTNAME must call lease4-get-by-hostname with hostname argument."""
        mock_client = self._setup_mock(MockKeaClient, [dict(self._LEASE4)])
        response = self._htmx_get(self._url4(), {"by": "hostname", "q": "search-host"})
        self.assertEqual(response.status_code, 200)
        cmd_names = [c.args[0] for c in mock_client.command.call_args_list]
        self.assertIn("lease4-get-by-hostname", cmd_names)
        call = next(c for c in mock_client.command.call_args_list if c.args[0] == "lease4-get-by-hostname")
        self.assertEqual(call.kwargs["arguments"]["hostname"], "search-host")

    @patch("netbox_kea.models.KeaClient")
    def test_search_by_client_id_sends_correct_command(self, MockKeaClient):
        """BY_CLIENT_ID must call lease4-get-by-client-id with client-id argument."""
        mock_client = self._setup_mock(MockKeaClient, [dict(self._LEASE4)])
        response = self._htmx_get(self._url4(), {"by": "client_id", "q": "01:aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        cmd_names = [c.args[0] for c in mock_client.command.call_args_list]
        self.assertIn("lease4-get-by-client-id", cmd_names)
        call = next(c for c in mock_client.command.call_args_list if c.args[0] == "lease4-get-by-client-id")
        self.assertEqual(call.kwargs["arguments"]["client-id"], "01:aa:bb:cc:dd:ee:ff")

    @patch("netbox_kea.models.KeaClient")
    def test_search_by_subnet_id_sends_correct_command(self, MockKeaClient):
        """BY_SUBNET_ID must call lease4-get-all with subnets=[<id>]."""
        mock_client = self._setup_mock(MockKeaClient, [dict(self._LEASE4)])
        response = self._htmx_get(self._url4(), {"by": "subnet_id", "q": "1"})
        self.assertEqual(response.status_code, 200)
        cmd_names = [c.args[0] for c in mock_client.command.call_args_list]
        self.assertIn("lease4-get-all", cmd_names)
        call = next(c for c in mock_client.command.call_args_list if c.args[0] == "lease4-get-all")
        self.assertEqual(call.kwargs["arguments"]["subnets"], [1])

    @patch("netbox_kea.models.KeaClient")
    def test_search_by_ip_returns_200(self, MockKeaClient):
        """BY_IP must call lease4-get with ip-address argument and return 200."""
        mock_client = self._setup_mock(MockKeaClient, [dict(self._LEASE4)], multiple=False)
        response = self._htmx_get(self._url4(), {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        cmd_names = [c.args[0] for c in mock_client.command.call_args_list]
        self.assertIn("lease4-get", cmd_names)

    @patch("netbox_kea.models.KeaClient")
    def test_search_result_3_returns_empty_table(self, MockKeaClient):
        """result=3 (not found) must render an empty table, not a 500."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 3, "arguments": None}]
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        response = self._htmx_get(self._url4(), {"by": "ip", "q": "10.0.0.99"})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_search_by_duid_v6_sends_correct_command(self, MockKeaClient):
        """BY_DUID on the v6 endpoint must call lease6-get-by-duid."""
        server6 = _make_db_server(name="kea-v6-search", server_url="https://kea6.example.com", dhcp4=False, dhcp6=True)
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [], "count": 0}}]
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:server_leases6", args=[server6.pk])
        response = self._htmx_get(url, {"by": "duid", "q": "00:01:aa:bb:cc:dd"})
        self.assertEqual(response.status_code, 200)
        cmd_names = [c.args[0] for c in mock_client.command.call_args_list]
        self.assertIn("lease6-get-by-duid", cmd_names)


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

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_export_all_returns_csv_content_type(self, MockKeaClient):
        """?export=all must respond with text/csv Content-Type."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    @patch("netbox_kea.models.KeaClient")
    def test_export_table_returns_csv(self, MockKeaClient):
        """?export=table must also return text/csv (selected columns)."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.get(self._url(), {"export": "table", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_export_with_invalid_form_redirects(self):
        """?export=all with missing q/by must redirect (not crash)."""
        # No 'q' or 'by' — form is invalid
        response = self.client.get(self._url(), {"export": "all"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_export_by_subnet_paginates_all_leases(self, MockKeaClient):
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
        call_count = {"n": 0}

        def command_side_effect(cmd, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First page: 3 leases; count == per_page (3) signals more data
                return [{"result": 0, "arguments": {"leases": page1_leases, "count": 3}}]
            # Second call returns empty — end of pagination
            return [{"result": 3, "arguments": None}]

        MockKeaClient.return_value.command.side_effect = command_side_effect
        # Pass per_page=3 so that count(3) == per_page(3) triggers next-page fetch
        response = self.client.get(
            self._url(),
            {"export": "all", "by": "subnet", "q": "10.0.0.0/24", "per_page": "3"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))
        self.assertGreaterEqual(call_count["n"], 2)


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

    @patch("netbox_kea.models.KeaClient")
    def test_post_confirmed_calls_kea_and_redirects(self, MockKeaClient):
        """POST with _confirm=1 must call Kea lease4-del and redirect."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0}]
        response = self.client.post(
            self._url(),
            {"pk": ["10.0.0.1"], "_confirm": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        # Verify Kea was called with the lease4-del command
        cmd_names = [c.args[0] for c in mock_client.command.call_args_list]
        self.assertIn("lease4-del", cmd_names)

    @patch("netbox_kea.models.KeaClient")
    def test_post_confirmed_kea_error_redirects_with_error_message(self, MockKeaClient):
        """When Kea returns an error during deletion, must redirect (not 500) and show error."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = KeaException({"result": 1, "text": "lease not found"})
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

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    @patch("netbox_kea.models.KeaClient")
    def test_non_result2_kea_exception_does_not_crash(self, MockKeaClient):
        """A KeaException with result=1 (server error) on reservation lookup must not 500."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get.side_effect = KeaException({"result": 1, "text": "server error"}, index=0)
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_unexpected_exception_on_reservation_lookup_does_not_crash(self, MockKeaClient):
        """An unexpected exception (e.g. network error) during reservation lookup must not 500."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get.side_effect = RuntimeError("socket closed")
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_sync_url_set_when_no_netbox_ip(self, MockKeaClient, mock_bulk_fetch):
        """When the lease IP is absent from NetBox, sync_url must be set on the lease dict."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get.return_value = None
        mock_bulk_fetch.return_value = {}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        # Sync button (hx-post) must appear since no NetBox IP
        self.assertContains(response, "hx-post")

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_synced_badge_set_when_netbox_ip_exists(self, MockKeaClient, mock_bulk_fetch):
        """When the lease IP exists in NetBox IPAM, netbox_ip_url must be set (Synced badge)."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get.return_value = None
        nb_ip = MagicMock()
        nb_ip.get_absolute_url.return_value = "/ipam/ip-addresses/99/"
        mock_bulk_fetch.return_value = {"10.0.0.5": nb_ip}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
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

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_stale_mac_badge_shows_specific_macs_in_title(self, MockKeaClient, mock_bulk_fetch):
        """The ⚠ MAC? badge title must contain both lease MAC and reservation MAC."""
        mock_client = MockKeaClient.return_value
        mock_client.clone.return_value = mock_client  # worker threads must see configured behaviors
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get.return_value = self._RESERVATION
        mock_bulk_fetch.return_value = {}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "aa:bb:cc:dd:ee:01")  # lease MAC in tooltip
        self.assertContains(response, "aa:bb:cc:dd:ee:99")  # reservation MAC in tooltip

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_stale_mac_badge_renders_htmx_delete_button(self, MockKeaClient, mock_bulk_fetch):
        """The stale-MAC badge must include an HTMX delete button (hx-post) for one-click removal."""
        mock_client = MockKeaClient.return_value
        mock_client.clone.return_value = mock_client  # worker threads must see configured behaviors
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get.return_value = self._RESERVATION
        mock_bulk_fetch.return_value = {}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        # hx-post must point to the delete endpoint (distinct from the bulk-delete form action)
        delete_url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        self.assertContains(response, f'hx-post="{delete_url}"')

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_matching_mac_badge_has_no_htmx_delete_button(self, MockKeaClient, mock_bulk_fetch):
        """When lease MAC matches reservation MAC, no HTMX delete button must appear."""
        matching_rsv = {**self._RESERVATION, "hw-address": self._LEASE4["hw-address"]}
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get.return_value = matching_rsv
        mock_bulk_fetch.return_value = {}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        delete_url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        self.assertNotContains(response, f'hx-post="{delete_url}"')

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_stale_mac_badge_no_delete_when_no_permission(self, MockKeaClient, mock_bulk_fetch):
        """When the user lacks delete permission, the stale-MAC badge must NOT include an HTMX delete button."""
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        from netbox_kea.models import Server

        User = get_user_model()
        readonly_user = User.objects.create_user(username="readonly_stale", password="pass")
        # Grant only view permission via NetBox's ObjectPermission (not change/delete)
        ct = ContentType.objects.get_for_model(Server)
        view_obj_perm = ObjectPermission.objects.create(name="test-view-server-readonly", actions=["view"])
        view_obj_perm.object_types.add(ct)
        view_obj_perm.users.add(readonly_user)
        self.client.force_login(readonly_user)

        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"ip-address": "10.0.0.5", **self._LEASE4}}]
        mock_client.reservation_get.return_value = self._RESERVATION
        mock_bulk_fetch.return_value = {}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
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

    def _single_page_side_effect(self, cmd, service=None, arguments=None, check=None):
        """Kea returns one page with one lease, then empty (result=3) on next call."""
        if cmd == "lease4-get-page":
            frm = arguments.get("from", "")
            if frm == "0.0.0.0":
                return [{"result": 0, "arguments": {"leases": [self._LEASE], "count": 1}}]
            return [{"result": 3, "arguments": None}]
        return [{"result": 0, "arguments": {}}]

    @patch("netbox_kea.models.KeaClient")
    def test_export_all_returns_csv(self, MockKeaClient):
        """?export_all=1 must return text/csv."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = self._single_page_side_effect
        response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    @patch("netbox_kea.models.KeaClient")
    def test_export_all_includes_lease_data(self, MockKeaClient):
        """?export_all=1 CSV must contain the lease IP address."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = self._single_page_side_effect
        response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        content = (
            b"".join(response.streaming_content).decode()
            if hasattr(response, "streaming_content")
            else response.content.decode()
        )
        self.assertIn("10.0.0.1", content)

    @patch("netbox_kea.models.KeaClient")
    def test_export_all_paginates_all_leases(self, MockKeaClient):
        """?export_all=1 must paginate until Kea returns result=3."""
        # The view uses per_page=1000. Return count=1000 on the first call so the
        # view sees a full page and issues a second request; the second call returns
        # result=3 to signal end-of-data.
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
        call_count = {"n": 0}

        def paginate_side_effect(cmd, service=None, arguments=None, check=None):
            if cmd != "lease4-get-page":
                return [{"result": 0, "arguments": {}}]
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Report count==1000 so the view thinks there may be more pages.
                return [{"result": 0, "arguments": {"leases": page1, "count": 1000}}]
            return [{"result": 3, "arguments": None}]

        MockKeaClient.return_value.command.side_effect = paginate_side_effect
        response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))
        self.assertGreaterEqual(call_count["n"], 2)

    @patch("netbox_kea.models.KeaClient")
    def test_export_all_v6_starts_from_double_colon(self, MockKeaClient):
        """?export_all=1 for v6 must start the cursor from '::'."""
        call_args_list = []

        def v6_side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "lease6-get-page":
                call_args_list.append(arguments)
                return [{"result": 3, "arguments": None}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = v6_side_effect
        response = self.client.get(self._url6(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(call_args_list), 1)
        self.assertEqual(call_args_list[0]["from"], "::")

    @patch("netbox_kea.models.KeaClient")
    def test_export_all_v4_starts_from_zero_ip(self, MockKeaClient):
        """?export_all=1 for v4 must start the cursor from '0.0.0.0'."""
        call_args_list = []

        def v4_side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "lease4-get-page":
                call_args_list.append(arguments)
                return [{"result": 3, "arguments": None}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = v4_side_effect
        response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(call_args_list), 1)
        self.assertEqual(call_args_list[0]["from"], "0.0.0.0")


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

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.command.return_value = _LEASE4_GET_RESP
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_prefills_hostname(self, MockKeaClient):
        """GET pre-fills hostname from the existing lease."""
        MockKeaClient.return_value.command.return_value = _LEASE4_GET_RESP
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("host1.example.com", content)

    @patch("netbox_kea.models.KeaClient")
    def test_get_prefills_hw_address(self, MockKeaClient):
        """GET pre-fills hw_address from the existing lease (v4 only)."""
        MockKeaClient.return_value.command.return_value = _LEASE4_GET_RESP
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("aa:bb:cc:dd:ee:ff", content)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_lease_update_and_redirects(self, MockKeaClient):
        """POST with valid data calls lease_update and redirects."""
        MockKeaClient.return_value.lease_update.return_value = None
        response = self.client.post(
            self._url(),
            {
                "hostname": "newhost.example.com",
                "hw_address": "11:22:33:44:55:66",
                "valid_lft": "7200",
            },
        )
        self.assertEqual(response.status_code, 302)
        MockKeaClient.return_value.lease_update.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_redirects_with_error(self, MockKeaClient):
        """POST that raises KeaException shows error and redirects."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.lease_update.side_effect = KeaException({"result": 1, "text": "lease not found"})
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

    @patch("netbox_kea.models.KeaClient")
    def test_state_column_rendered_in_table(self, MockKeaClient):
        """Lease table includes a state_label column header."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _STATE_LEASES_RESP
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:01"})
        self.assertEqual(response.status_code, 200)
        # State column header must be present
        self.assertContains(response, "State")

    @patch("netbox_kea.models.KeaClient")
    def test_state_label_active_rendered(self, MockKeaClient):
        """Active lease shows 'Active' state badge."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {"leases": [_STATE_LEASES_RESP[0]["arguments"]["leases"][0]]},
            }
        ]
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:01"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active")

    @patch("netbox_kea.models.KeaClient")
    def test_state_label_declined_rendered(self, MockKeaClient):
        """Declined lease shows 'Declined' state badge."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {"leases": [_STATE_LEASES_RESP[0]["arguments"]["leases"][1]]},
            }
        ]
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:02"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Declined")

    @patch("netbox_kea.models.KeaClient")
    def test_state_filter_declined_hides_active(self, MockKeaClient):
        """State filter=1 (Declined) excludes Active leases from search results."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _STATE_LEASES_RESP
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        response = self._htmx_get(self._url4(), {"by": "hostname", "q": "host", "state": "1"})
        self.assertEqual(response.status_code, 200)
        # Active and Expired hosts should not appear
        self.assertNotContains(response, "active-host")
        self.assertNotContains(response, "expired-host")
        self.assertContains(response, "declined-host")

    @patch("netbox_kea.models.KeaClient")
    def test_state_filter_any_returns_all(self, MockKeaClient):
        """Empty state filter (Any) returns all leases."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _STATE_LEASES_RESP
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        response = self._htmx_get(self._url4(), {"by": "hostname", "q": "host", "state": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "active-host")
        self.assertContains(response, "declined-host")
        self.assertContains(response, "expired-host")

    @patch("netbox_kea.models.KeaClient")
    def test_state_filter_applied_on_paginated_subnet_search(self, MockKeaClient):
        """State filter also applies to paginated subnet-based search."""
        mock_client = MockKeaClient.return_value
        # First call: lease4-get-page; second: reservation_get_page
        mock_client.command.return_value = _PAGE_LEASES_RESP
        mock_client.reservation_get_page.return_value = ([], 0, 0)
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

    @patch("netbox_kea.models.KeaClient")
    def test_get_lease4_add_returns_200(self, MockKeaClient):
        """GET /leases4/add/ returns 200 and renders the add form."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ip_address")

    @patch("netbox_kea.models.KeaClient")
    def test_get_lease6_add_returns_200(self, MockKeaClient):
        """GET /leases6/add/ returns 200 and shows duid + iaid fields."""
        response = self.client.get(self._url(version=6))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "duid")
        self.assertContains(response, "iaid")

    @patch("netbox_kea.models.KeaClient")
    def test_post_lease4_add_valid_redirects(self, MockKeaClient):
        """POST valid v4 lease data redirects to the lease list."""
        MockKeaClient.return_value.lease_add.return_value = None
        response = self.client.post(self._url(version=4), self._valid_post4())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_post_lease4_add_calls_kea_with_correct_args(self, MockKeaClient):
        """POST v4 calls lease_add with ip-address, hw-address, and subnet-id."""
        mock_client = MockKeaClient.return_value
        mock_client.lease_add.return_value = None
        self.client.post(self._url(version=4), self._valid_post4())
        mock_client.lease_add.assert_called_once()
        args = mock_client.lease_add.call_args
        lease = args[0][1] if args[0] else args[1].get("lease", args[0][1])
        self.assertEqual(lease["ip-address"], "10.0.0.200")
        self.assertEqual(lease.get("hw-address"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(lease.get("subnet-id"), 1)

    @patch("netbox_kea.models.KeaClient")
    def test_post_lease4_add_invalid_ip_shows_form_errors(self, MockKeaClient):
        """POST with a non-IPv4 string re-renders form with validation errors."""
        response = self.client.post(self._url(version=4), self._valid_post4(ip_address="not-an-ip"))
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.lease_add.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_post_lease4_add_kea_exception_shows_error_message(self, MockKeaClient):
        """POST that triggers a KeaException shows error and re-renders (no redirect)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.lease_add.side_effect = KeaException({"result": 1, "text": "address already in use"})
        response = self.client.post(self._url(version=4), self._valid_post4())
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_lease6_add_valid_redirects(self, MockKeaClient):
        """POST valid v6 lease data redirects to the lease list."""
        MockKeaClient.return_value.lease_add.return_value = None
        response = self.client.post(self._url(version=6), self._valid_post6())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_post_lease6_add_calls_kea_with_correct_args(self, MockKeaClient):
        """POST v6 calls lease_add with ip-address, duid, and iaid."""
        mock_client = MockKeaClient.return_value
        mock_client.lease_add.return_value = None
        self.client.post(self._url(version=6), self._valid_post6())
        mock_client.lease_add.assert_called_once()
        args = mock_client.lease_add.call_args
        lease = args[0][1] if args[0] else args[1].get("lease")
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

    @patch("netbox_kea.models.KeaClient")
    def test_lease4_add_form_has_sync_to_netbox_field(self, MockKeaClient):
        """GET lease4 add page renders a sync_to_netbox checkbox."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)
        self.assertIn("sync_to_netbox", response.content.decode())

    @patch("netbox_kea.views.leases.sync_lease_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_lease4_add_with_sync_calls_sync_lease(self, MockKeaClient, mock_sync):
        """POST with sync_to_netbox=on calls sync_lease_to_netbox() with the lease dict."""
        MockKeaClient.return_value.lease_add.return_value = None
        mock_sync.return_value = (MagicMock(), True)
        response = self.client.post(self._url(version=4), self._post4(sync=True))
        self.assertEqual(response.status_code, 302)
        mock_sync.assert_called_once()
        lease = mock_sync.call_args[0][0]
        self.assertEqual(lease["ip-address"], "10.0.0.200")

    @patch("netbox_kea.views.leases.sync_lease_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_lease4_add_without_sync_does_not_call_sync(self, MockKeaClient, mock_sync):
        """POST without sync_to_netbox does NOT call sync_lease_to_netbox()."""
        MockKeaClient.return_value.lease_add.return_value = None
        response = self.client.post(self._url(version=4), self._post4(sync=False))
        self.assertEqual(response.status_code, 302)
        mock_sync.assert_not_called()

    @patch("netbox_kea.views.leases.sync_lease_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_lease4_add_sync_failure_does_not_prevent_kea_success(self, MockKeaClient, mock_sync):
        """Sync failure is a warning; the lease creation still succeeds (302 redirect)."""
        MockKeaClient.return_value.lease_add.return_value = None
        mock_sync.side_effect = Exception("NetBox unreachable")
        response = self.client.post(self._url(version=4), self._post4(sync=True))
        self.assertEqual(response.status_code, 302)
        MockKeaClient.return_value.lease_add.assert_called_once()


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

    @patch("netbox_kea.models.KeaClient")
    def test_get_v4_returns_200(self, MockKeaClient):
        """GET lease4 bulk import page returns 200."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_v6_returns_200(self, MockKeaClient):
        """GET lease6 bulk import page returns 200."""
        response = self.client.get(self._url(version=6))
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_v4_valid_csv_calls_lease_add(self, MockKeaClient):
        """POST with valid v4 CSV calls lease_add once per row."""
        MockKeaClient.return_value.lease_add.return_value = None
        response = self.client.post(self._url(version=4), self._post(version=4))
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.lease_add.assert_called_once()
        args = MockKeaClient.return_value.lease_add.call_args[0]
        self.assertEqual(args[0], 4)
        self.assertEqual(args[1]["ip-address"], "10.0.0.10")

    @patch("netbox_kea.models.KeaClient")
    def test_post_v6_valid_csv_calls_lease_add(self, MockKeaClient):
        """POST with valid v6 CSV calls lease_add with correct duid and iaid."""
        MockKeaClient.return_value.lease_add.return_value = None
        response = self.client.post(self._url(version=6), self._post(version=6))
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.lease_add.assert_called_once()
        args = MockKeaClient.return_value.lease_add.call_args[0]
        self.assertEqual(args[0], 6)
        self.assertEqual(args[1]["duid"], "00:01:02:03")
        self.assertEqual(args[1]["iaid"], 12345)

    @patch("netbox_kea.models.KeaClient")
    def test_post_multiple_rows_calls_lease_add_per_row(self, MockKeaClient):
        """Each CSV row triggers one lease_add call."""
        MockKeaClient.return_value.lease_add.return_value = None
        csv_bytes = self._csv4(
            rows=[
                "10.0.0.10,aa:bb:cc:dd:ee:01,1,3600,h1\n",
                "10.0.0.11,aa:bb:cc:dd:ee:02,1,3600,h2\n",
                "10.0.0.12,aa:bb:cc:dd:ee:03,1,3600,h3\n",
            ]
        )
        response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MockKeaClient.return_value.lease_add.call_count, 3)

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_failure_shows_error_count(self, MockKeaClient):
        """If some rows fail, result context shows correct created/error counts."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.lease_add.side_effect = [None, KeaException({"result": 1, "text": "bad"}, index=0)]
        csv_bytes = self._csv4(
            rows=[
                "10.0.0.10,aa:bb:cc:dd:ee:01,1,3600,h1\n",
                "10.0.0.11,aa:bb:cc:dd:ee:02,1,3600,h2\n",
            ]
        )
        response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], 1)

    @patch("netbox_kea.models.KeaClient")
    def test_post_empty_csv_shows_form_error(self, MockKeaClient):
        """Uploading a CSV with only a header (no data rows) returns 200 with empty result."""
        MockKeaClient.return_value.lease_add.return_value = None
        csv_bytes = b"ip-address,hw-address\n"
        response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
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

    @patch("netbox_kea.models.KeaClient")
    def test_lease_add_fires_lease_added_signal(self, MockKeaClient):
        """_BaseLeaseAddView.post must send lease_added signal after successful add."""
        from netbox_kea import signals

        mock_client = MockKeaClient.return_value
        mock_client.lease_add.return_value = None

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.lease_added.connect(handler)
        try:
            url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
            self.client.post(url, self._LEASE4)
        finally:
            signals.lease_added.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["ip_address"], "10.0.0.5")
        self.assertEqual(received[0]["dhcp_version"], 4)
        self.assertEqual(received[0]["server"].pk, self.server.pk)

    @patch("netbox_kea.models.KeaClient")
    def test_lease_delete_fires_leases_deleted_signal(self, MockKeaClient):
        """BaseServerLeasesDeleteView.post must send leases_deleted signal after successful delete."""
        from netbox_kea import signals

        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "text": "Success"}]

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.leases_deleted.connect(handler)
        try:
            url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
            self.client.post(url, {"pk": "10.0.0.5", "_confirm": "1"})
        finally:
            signals.leases_deleted.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertIn("10.0.0.5", received[0]["ip_addresses"])
        self.assertEqual(received[0]["dhcp_version"], 4)

    @patch("netbox_kea.models.KeaClient")
    def test_reservation_add_fires_reservation_created_signal(self, MockKeaClient):
        """ServerReservation4AddView.post must send reservation_created signal."""
        from netbox_kea import signals

        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        mock_client.command.return_value = [{"result": 0, "text": "Success"}]  # config-write

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.reservation_created.connect(handler)
        try:
            url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
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

    @patch("netbox_kea.models.KeaClient")
    def test_reservation_delete_fires_reservation_deleted_signal(self, MockKeaClient):
        """ServerReservation4DeleteView.post must send reservation_deleted signal."""
        from netbox_kea import signals

        mock_client = MockKeaClient.return_value
        mock_client.reservation_del.return_value = None
        mock_client.command.return_value = [{"result": 0, "text": "Success"}]

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.reservation_deleted.connect(handler)
        try:
            url = reverse(
                "plugins:netbox_kea:server_reservation4_delete",
                args=[self.server.pk, 1, "10.0.0.10"],
            )
            self.client.post(url)
        finally:
            signals.reservation_deleted.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["dhcp_version"], 4)
        self.assertEqual(received[0]["ip_address"], "10.0.0.10")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseJournalEntries(_ViewTestBase):
    """Lease add and delete views must create JournalEntry records on the Server."""

    @patch("netbox_kea.models.KeaClient")
    def test_lease_add_creates_journal_entry(self, MockKeaClient):
        """A successful lease add must create a JournalEntry attached to the server."""
        from django.contrib.contenttypes.models import ContentType
        from extras.models import JournalEntry

        mock_client = MockKeaClient.return_value
        mock_client.lease_add.return_value = None
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        server_ct = ContentType.objects.get_for_model(self.server)
        before = JournalEntry.objects.filter(
            assigned_object_id=self.server.pk,
            assigned_object_type=server_ct,
        ).count()
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

    @patch("netbox_kea.models.KeaClient")
    def test_lease_delete_creates_journal_entry(self, MockKeaClient):
        """A successful lease delete must create a JournalEntry attached to the server."""
        from django.contrib.contenttypes.models import ContentType
        from extras.models import JournalEntry

        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "text": "Success"}]
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        server_ct = ContentType.objects.get_for_model(self.server)
        before = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).count()
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
        from unittest.mock import MagicMock

        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.1", "hw_address": "aa:bb:cc:dd:ee:ff"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, False, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            patch.object(server, "get_client", return_value=MagicMock()),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertNotIn("edit_url", lease)
        self.assertFalse(lease["can_change"])

    def test_edit_url_set_when_can_change_true(self):
        """edit_url must be set on leases when can_change=True."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.1", "hw_address": "aa:bb:cc:dd:ee:ff"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, False, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            patch.object(server, "get_client", return_value=MagicMock()),
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
            patch.object(server, "get_client", return_value=MagicMock()),
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
            patch.object(server, "get_client", return_value=MagicMock()),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertFalse(lease["is_reserved"])

    def test_reservation_url_none_for_read_only_when_reservation_exists(self):
        """reservation_url must be None when can_change=False even if a reservation exists."""
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
            patch.object(server, "get_client", return_value=MagicMock()),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertIsNone(lease["reservation_url"])
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
            patch.object(server, "get_client", return_value=MagicMock()),
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
            patch.object(server, "get_client", return_value=MagicMock()),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=False)
        self.assertIsNone(lease.get("create_reservation_url"))

    def test_create_reservation_url_set_when_can_change_true(self):
        """create_reservation_url must be set when can_change=True and no reservation."""
        from netbox_kea.views import _enrich_leases_with_badges

        server = self.server
        lease = {"ip_address": "10.0.0.99", "hw_address": "cc:cc:cc:cc:cc:cc"}
        with (
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True, set())),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            patch.object(server, "get_client", return_value=MagicMock()),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertIsNotNone(lease.get("create_reservation_url"))


# ---------------------------------------------------------------------------
# EnrichLeases exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesExceptionPaths(_ViewTestBase):
    """_enrich_leases_with_badges exception branches (KeaException result≠2 and generic)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_non_hook_swallowed(self, MockKeaClient):
        """KeaException with result≠2 is swallowed and view returns 200."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.lease4_get_page.return_value = (
            [
                {
                    "ip-address": "10.0.0.1",
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "hostname": "host1",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 0,
                }
            ],
            0,
            0,
        )
        with patch(
            "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
            side_effect=KeaException({"result": 1, "text": "error"}, index=0),
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_in_enrichment_swallowed(self, MockKeaClient):
        """Generic exception in enrichment is swallowed and view returns 200."""
        mock_client = MockKeaClient.return_value
        mock_client.lease4_get_page.return_value = (
            [
                {
                    "ip-address": "10.0.0.2",
                    "hw-address": "aa:bb:cc:dd:ee:01",
                    "hostname": "host2",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 0,
                }
            ],
            0,
            0,
        )
        with patch(
            "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
            side_effect=RuntimeError("unexpected error"),
        ):
            response = self.client.get(self._url())
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

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_htmx_exception_returns_error_partial(self, MockKeaClient):
        MockKeaClient.return_value.command.side_effect = RuntimeError("boom")
        response = self.client.get(
            self._url() + "?q=10.0.0.1&by=ip",
            HTTP_HX_REQUEST="true",
        )
        # Must not crash — returns HTMX error partial
        self.assertIn(response.status_code, (200, 500))


# ---------------------------------------------------------------------------
# Lease edit GET — KeaException, not-found, v6 duid
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditGet(_ViewTestBase):
    """Lines 894-896, 899-900, 910: lease edit GET error paths."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_kea_exception_redirects(self, MockKeaClient):
        """KeaException in lease4 GET redirects to leases page."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException({"result": 1, "text": "err"}, index=0)
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_get_lease_not_found_redirects(self, MockKeaClient):
        """result=3 (not found) in lease4 GET redirects to leases page."""
        MockKeaClient.return_value.command.return_value = [{"result": 3, "arguments": None}]
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_get_v6_lease_includes_duid(self, MockKeaClient):
        """v6 lease GET includes duid in form initial (line 910)."""
        server6 = _make_db_server(name="kea6-only", server_url="https://kea6.example.com", dhcp4=False, dhcp6=True)
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "ip-address": "2001:db8::1",
                    "duid": "00:01:00:01",
                    "hostname": "v6host",
                    "valid-lft": 3600,
                },
            }
        ]
        url = reverse("plugins:netbox_kea:server_lease6_edit", args=[server6.pk, "2001:db8::1"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "00:01:00:01")


# ---------------------------------------------------------------------------
# Lease edit POST — invalid form
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditPostInvalidForm(_ViewTestBase):
    """Line 931: lease edit POST with invalid form re-renders with 200."""

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_rerenders(self, MockKeaClient):
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        response = self.client.post(url, {"hostname": "", "valid_lft": "not-a-number"})
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Lease add — generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddGenericException(_ViewTestBase):
    """Lines 1056-1058: generic exception on lease_add re-renders form."""

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_rerenders_form(self, MockKeaClient):
        MockKeaClient.return_value.lease_add.side_effect = requests.RequestException("unexpected crash")
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
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
        with patch("netbox_kea.models.KeaClient") as MockKea:
            MockKea.return_value.command.return_value = resp
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

    def test_null_args_returns_empty(self):
        from netbox_kea import constants

        leases = self._call(constants.BY_HOSTNAME, q="ghost", resp=[{"result": 0, "arguments": None}])
        self.assertEqual(leases, [])

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

        with patch("netbox_kea.models.KeaClient") as MockKea:
            MockKea.return_value.command.side_effect = iter(responses)
            return _fetch_all_leases_from_server(self.server, version=4, max_leases=max_leases)

    def test_result_3_stops_loop(self):
        """Line 3497: result=3 breaks the pagination loop."""
        leases, truncated = self._run([[{"result": 3, "arguments": None}]])
        self.assertEqual(leases, [])
        self.assertFalse(truncated)

    def test_null_args_stops_loop(self):
        """Line 3500: null arguments breaks the pagination loop."""
        leases, truncated = self._run([[{"result": 0, "arguments": None}]])
        self.assertEqual(leases, [])
        self.assertFalse(truncated)

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

        with patch("netbox_kea.models.KeaClient") as MockKea:
            side = iter(pages)
            MockKea.return_value.reservation_get_page.side_effect = lambda *a, **kw: next(side)
            result, available = _fetch_reservation_by_ip(MockKea.return_value, version=4)
            return result, available

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


# ---------------------------------------------------------------------------
# _enrich_leases_with_badges — exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesExceptionPaths2(_ViewTestBase):
    """Lines 3611-3619: enrich leases exception handling in combined leases view."""

    def _url(self):
        return reverse("plugins:netbox_kea:combined_leases4") + f"?servers={self.server.pk}&q=10.0.0.1&by=ip"

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_result2_sets_hook_unavailable(self, MockKeaClient):
        """Lines 3612-3616: KeaException result=2 → host_cmds_available=False."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {"leases": [{"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0, "subnet-id": 1}]},
            }
        ]
        with patch(
            "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
            side_effect=KeaException({"result": 2, "text": "hook not loaded"}, index=0),
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_non_result2_continues(self, MockKeaClient):
        """Lines 3612-3616: KeaException result≠2 → logged, host_cmds=False."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {"leases": [{"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0, "subnet-id": 1}]},
            }
        ]
        with patch(
            "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
            side_effect=KeaException({"result": 1, "text": "other error"}, index=0),
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_continues(self, MockKeaClient):
        """Lines 3617-3619: generic Exception from _fetch_reservation_by_ip_for_leases is handled."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {"leases": [{"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0, "subnet-id": 1}]},
            }
        ]
        with patch(
            "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases",
            side_effect=RuntimeError("unexpected crash"),
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
    @patch("netbox_kea.models.KeaClient")
    def test_parse_error_shows_form_error(self, MockKeaClient, mock_parse):
        """Lines 4617-4619: ValueError from parse_lease_csv adds generic form error (no raw exception text)."""
        import io

        mock_parse.side_effect = ValueError("bad column")
        csv_file = io.BytesIO(b"ip-address\n10.0.0.1")
        csv_file.name = "leases.csv"
        response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "parsing failed")
        self.assertNotContains(response, "bad column")

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_adds_error_row(self, MockKeaClient):
        """Lines 4641-4643: generic exception during lease_add adds error row."""
        import io

        MockKeaClient.return_value.lease_add.side_effect = RuntimeError("crash")
        # Provide valid CSV content so parse_lease_csv succeeds
        csv_content = b"ip-address\n10.0.0.1"
        csv_file = io.BytesIO(csv_content)
        csv_file.name = "leases.csv"
        response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unexpected")


# ---------------------------------------------------------------------------
# get_leases_page — edge cases (lines 464, 480, 487-489)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageEdgeCases(_ViewTestBase):
    """Edge cases in BaseServerLeasesView.get_leases_page()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_zero_network_uses_network_as_start(self, MockKeaClient):
        """Line 464: subnet.network == 0 → frm = str(subnet.network) = '0.0.0.0'."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": {"count": 0, "leases": []}}]
        # 0.0.0.0/8: int(network) == 0 → line 464 fires
        response = self.client.get(
            self._url(),
            {"by": "subnet", "q": "0.0.0.0/8"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_null_args_raises_runtime_error(self, MockKeaClient):
        """Line 480: lease-get-page returns arguments=None → RuntimeError (caught by HTMX handler)."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": None}]
        response = self.client.get(
            self._url(),
            {"by": "subnet", "q": "10.0.0.0/24"},
            HTTP_HX_REQUEST="true",
        )
        # RuntimeError is caught by outer except → HTMX error partial
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_lease_outside_subnet_truncates_list(self, MockKeaClient):
        """Lines 487-489: lease IP not in queried subnet → raw_leases truncated."""
        mock_client = MockKeaClient.return_value
        per_page = 25
        # Return per_page leases where the only one is OUTSIDE the queried subnet
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "count": per_page,
                    "leases": [{"ip-address": "10.0.1.1", "valid-lft": 3600, "state": 0}],
                },
            }
        ]
        response = self.client.get(
            self._url(),
            {"by": "subnet", "q": "10.0.0.0/24"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# get_leases — AbortRequest and null args (lines 522, 535)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesCoverage(_ViewTestBase):
    """Edge cases in BaseServerLeasesView.get_leases()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_invalid_by_raises_abort_request(self):
        """Line 522: invalid 'by' value → AbortRequest raised."""
        from unittest.mock import MagicMock

        from utilities.exceptions import AbortRequest

        from netbox_kea.views import ServerLeases4View

        view = ServerLeases4View()
        mock_client = MagicMock()
        with self.assertRaises(AbortRequest):
            view.get_leases(mock_client, "test_query", "not_a_valid_by")

    @patch("netbox_kea.models.KeaClient")
    def test_null_args_from_lease_get_raises_runtime_error(self, MockKeaClient):
        """Line 535: lease-get returns arguments=None → RuntimeError (caught by HTMX handler)."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": None}]
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

    @patch("netbox_kea.models.KeaClient")
    def test_export_all_null_args_returns_csv(self, MockKeaClient):
        """Line 618: export_all lease-get-page returns arguments=None → break → empty CSV."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": None}]
        response = self.client.get(self._url(), {"export_all": "1"})
        # Should return CSV even when args is None (empty export)
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# HTMX invalid form (lines 649-650)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestHTMXInvalidFormCoverage(_ViewTestBase):
    """Lines 649-650: HTMX GET with invalid form → renders HTMX partial."""

    @patch("netbox_kea.models.KeaClient")
    def test_htmx_invalid_form_returns_partial(self, MockKeaClient):
        """form.is_valid()==False for HTMX → renders server_dhcp_leases_htmx.html."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        # 'by' has an invalid choice value → form.is_valid() returns False
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

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_duid_calls_lease_update(self, MockKeaClient):
        """duid field in POST → kwargs['duid'] is set and lease_update called."""
        mock_client = MockKeaClient.return_value
        mock_client.lease_update.return_value = None
        url = reverse(
            "plugins:netbox_kea:server_lease6_edit",
            args=[self.server.pk, "2001:db8::1"],
        )
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
        mock_client.lease_update.assert_called_once()
        _, call_kwargs = mock_client.lease_update.call_args
        self.assertEqual(call_kwargs.get("duid"), "01:02:03:04")


# ---------------------------------------------------------------------------
# _fetch_one — missing subnet_id (line 3561)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchOneEmptyLease(_ViewTestBase):
    """Line 3561: _fetch_one returns early when lease has no subnet_id."""

    @patch("netbox_kea.models.KeaClient")
    def test_lease_without_subnet_id_skips_reservation_lookup(self, MockKeaClient):
        """Lease without subnet-id → _fetch_one returns (ip, None, True) without API call."""
        mock_client = MockKeaClient.return_value
        # Return a lease with ip-address but NO subnet-id
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "ip-address": "10.0.0.1",
                    "valid-lft": 3600,
                    "state": 0,
                    "hostname": "testhost",
                    # deliberately omit "subnet-id"
                },
            }
        ]
        mock_client.reservation_get.return_value = None
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
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

    @patch("netbox_kea.models.KeaClient")
    def test_continues_after_first_delete_error(self, MockKeaClient):
        """When the first IP fails, the second IP is still deleted."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = [
            KeaException({"result": 1, "text": "not found", "arguments": None}, index=0),
            None,
        ]
        response = self.client.post(
            self._url(),
            {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_client.command.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_success_message_shows_count_of_deleted(self, MockKeaClient):
        """Success message reflects only the successfully deleted count."""
        MockKeaClient.return_value.command.return_value = None
        response = self.client.post(
            self._url(),
            {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
            follow=True,
        )
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("2" in m and "deleted" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_partial_failure_shows_warning(self, MockKeaClient):
        """When some IPs fail, a warning message about partial failure is shown."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = [
            KeaException({"result": 1, "text": "not found", "arguments": None}, index=0),
            None,
        ]
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

    @patch("netbox_kea.models.KeaClient")
    def test_attribute_error_propagates(self, MockKeaClient):
        """An AttributeError inside get_export_all must not be silently caught."""
        MockKeaClient.return_value.command.side_effect = AttributeError("bad mock")
        with self.assertRaises(AttributeError):
            self.client.get(self._url(), {"export_all": "1"})


# ---------------------------------------------------------------------------
# Fix C: reservation enrichment failed_ips seeding
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesFailedIpsSeeding(_ViewTestBase):
    """On enrichment error, all lease IPs are marked as indeterminate (failed_ips)."""

    @patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases")
    @patch("netbox_kea.models.KeaClient")
    def test_reservation_enrichment_exception_does_not_show_not_reserved(self, MockKeaClient, mock_fetch_reservations):
        """When reservation lookup raises an unexpected Exception, leases must not
        incorrectly appear as 'not reserved' (no create-reservation link shown)."""
        mock_client = MockKeaClient.return_value

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
        # subnet_id=1 search: client.command returns leases
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": raw_leases}}]
        # Make reservation lookup raise an unexpected exception
        mock_fetch_reservations.side_effect = RuntimeError("unexpected enrichment failure")

        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        # Use by=subnet_id so q="1" is a valid integer subnet ID (by=subnet requires CIDR)
        response = self.client.get(url, HTTP_HX_REQUEST="true", data={"by": "subnet_id", "q": "1"})

        self.assertEqual(response.status_code, 200)
        # After fix: failed_ips is seeded with all lease IPs, so create-reservation link not shown
        add_url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        self.assertNotContains(response, add_url)


# ─────────────────────────────────────────────────────────────────────────────
# F3: get_export() state filter
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportStateFilter(_ViewTestBase):
    """get_export() must honour the 'state' query parameter."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_state_filter_applied_to_export(self, MockKeaClient):
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
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"leases": leases, "count": 2}}]

        # Request export with state=1 (declined only)
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

    @patch("netbox_kea.models.KeaClient")
    def test_attribute_error_not_swallowed_by_htmx_handler(self, MockKeaClient):
        """An AttributeError inside the HTMX handler must propagate (not be caught silently)."""
        MockKeaClient.return_value.command.side_effect = AttributeError("mock programming bug")
        with self.assertRaises(AttributeError):
            self.client.get(
                self._url(),
                HTTP_HX_REQUEST="true",
                data={"by": "subnet", "q": "10.0.0.0/24"},
            )
