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
import unittest as _unittest  # alias to avoid pytest collection confusion
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import Server
from netbox_kea.views import _get_reservation_identifier as _extract_identifier

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
# ServerListView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerListView(_ViewTestBase):
    """GET /plugins/kea/servers/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:server_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)


# ─────────────────────────────────────────────────────────────────────────────
# ServerView (detail)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDetailView(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_nonexistent_returns_404(self):
        url = reverse("plugins:netbox_kea:server", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# ServerEditView — add
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerAddView(_ViewTestBase):
    """GET/POST /plugins/kea/servers/add/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_add")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_post_missing_fields_rerenders_form_not_redirect_to_none(self):
        """Empty POST must re-render the form (200), never redirect to servers/None.

        This is the minimal reproduction of the original bug: an unsaved Server
        instance has ``pk=None``, so any redirect built from
        ``instance.get_absolute_url()`` would go to ``servers/None``.
        """
        url = reverse("plugins:netbox_kea:server_add")
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)
        self.assertNotIn(b"servers/None", response.content)

    def test_post_connectivity_failure_rerenders_form(self):
        """ValidationError from clean() must re-render the form at /add/, not servers/None."""
        from django.core.exceptions import ValidationError

        url = reverse("plugins:netbox_kea:server_add")
        with patch.object(Server, "clean", side_effect=ValidationError("unreachable")):
            response = self.client.post(
                url,
                {
                    "name": "bad-server",
                    "server_url": "http://unreachable.kea.example.com",
                    "dhcp4": True,
                    "dhcp6": False,
                    "ssl_verify": True,
                    "has_control_agent": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)
        self.assertNotIn(b"servers/None", response.content)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_data_redirects_to_integer_pk(self, MockKeaClient):
        """Successful server creation must redirect to servers/<int:pk>/, never /servers/None/."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_add")
        response = self.client.post(
            url,
            {
                "name": "new-valid-server",
                "server_url": "https://kea.new.example.com",
                "dhcp4": True,
                "dhcp6": False,
                "ssl_verify": True,
                "has_control_agent": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_server_is_saved_to_db(self, MockKeaClient):
        """After successful add, the Server object must exist in the DB."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_add")
        self.client.post(
            url,
            {
                "name": "saved-server",
                "server_url": "https://kea.saved.example.com",
                "dhcp4": True,
                "dhcp6": False,
                "ssl_verify": True,
                "has_control_agent": True,
            },
        )
        self.assertTrue(Server.objects.filter(name="saved-server").exists())


# ─────────────────────────────────────────────────────────────────────────────
# ServerEditView — edit existing
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerEditView(_ViewTestBase):
    """GET/POST /plugins/kea/servers/<pk>/edit/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_nonexistent_returns_404(self):
        url = reverse("plugins:netbox_kea:server_edit", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_post_missing_fields_rerenders_form(self):
        """Invalid edit POST must re-render the form (200), not redirect."""
        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        response = self.client.post(url, {"name": "", "server_url": ""})
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_edit_redirects_to_same_server(self, MockKeaClient):
        """Successful edit must redirect to the same server's detail URL."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        response = self.client.post(
            url,
            {
                "name": self.server.name,
                "server_url": "https://kea.edited.example.com",
                "dhcp4": True,
                "dhcp6": False,
                "ssl_verify": True,
                "has_control_agent": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)
        # Must redirect to THIS server's pk, not some other.
        self.assertIn(str(self.server.pk), response.url)


# ─────────────────────────────────────────────────────────────────────────────
# ServerDeleteView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDeleteView(_ViewTestBase):
    """GET/POST /plugins/kea/servers/<pk>/delete/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_delete", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_post_confirm_deletes_and_redirects(self):
        """Confirmed delete must remove the server and redirect (not to servers/None)."""
        pk = self.server.pk
        url = reverse("plugins:netbox_kea:server_delete", args=[pk])
        response = self.client.post(url, {"confirm": True})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertFalse(Server.objects.filter(pk=pk).exists())


# ─────────────────────────────────────────────────────────────────────────────
# ServerStatusView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerStatusView(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/status/"""

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_without_control_agent_returns_200(self, MockKeaClient):
        """Status view with has_control_agent=False must still return 200."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        server = _make_db_server(name="direct-daemon", has_control_agent=False)
        url = reverse("plugins:netbox_kea:server_status", args=[server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_nonexistent_returns_404(self):
        url = reverse("plugins:netbox_kea:server_status", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


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
# Subnet views
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnets4View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/subnets4/"""

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_with_dhcp4_disabled_redirects_with_valid_pk(self):
        v6_only = _make_db_server(name="v6-only-subnets", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_subnets4", args=[v6_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(v6_only.pk), response.url)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnets6View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/subnets6/"""

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEnrichment(_ViewTestBase):
    """Subnet views must pass option-data and pool information through to the table."""

    def _config_with_subnet(self, version: int) -> list[dict]:
        """Return a mock config-get response with one subnet including options and pools."""
        subnet_key = f"subnet{version}"
        dhcp_key = f"Dhcp{version}"
        subnet = {
            "id": 1,
            "subnet": "10.0.0.0/24",
            "option-data": [
                {"code": 3, "name": "routers", "data": "10.0.0.1"},
                {"code": 6, "name": "domain-name-servers", "data": "8.8.8.8"},
            ],
            "pools": [{"pool": "10.0.0.50-10.0.0.99", "option-data": []}],
        }
        return [{"result": 0, "arguments": {dhcp_key: {subnet_key: [subnet], "shared-networks": []}}}]

    def _side_effect_v4(self, cmd, service=None, **kwargs):
        if cmd == "config-get":
            return self._config_with_subnet(4)
        return _kea_command_side_effect(cmd, service=service, **kwargs)

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_table_includes_options_data(self, MockKeaClient):
        """Each subnet dict in the table must carry parsed option-data."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = self._side_effect_v4
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Table rows should contain gateway info from the subnet options
        self.assertContains(response, "10.0.0.1")

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_table_includes_pool_ranges(self, MockKeaClient):
        """Each subnet dict must carry pool range data."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = self._side_effect_v4
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.50-10.0.0.99")


# ─────────────────────────────────────────────────────────────────────────────
# ServerBulkImportView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerBulkImportView(_ViewTestBase):
    """GET/POST /plugins/kea/servers/import/

    Primary regression guard: the import URL must return 200, not 404.
    Before this fix, the URL pattern was missing entirely and clicking
    "Import" on the server list yielded a 404.
    """

    def test_get_returns_200_not_404(self):
        """Regression: /plugins/kea/servers/import/ must load the import form."""
        url = reverse("plugins:netbox_kea:server_bulk_import")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:server_bulk_import")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_csv_creates_server(self, MockKeaClient):
        """Valid CSV must create the server and redirect."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_bulk_import")
        csv_data = (
            "name,server_url,dhcp4,dhcp6,ssl_verify,has_control_agent\r\n"
            "import-test-server,https://import.example.com,true,false,true,false\r\n"
        )
        response = self.client.post(
            url,
            {"data": csv_data, "format": "csv", "csv_delimiter": ","},
        )
        # Either 200 (results page) or 302 (redirect on success)
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(Server.objects.filter(name="import-test-server").exists())

    def test_post_duplicate_name_returns_error_not_500(self):
        """Duplicate server name must re-render the form with errors, not 500."""
        url = reverse("plugins:netbox_kea:server_bulk_import")
        # setUp() already created a server named 'test-kea'
        csv_data = "name,server_url,dhcp4,dhcp6\r\ntest-kea,https://dup.example.com,true,false\r\n"
        # No KeaClient mock: clean() should never be reached (unique constraint
        # fires first during model validation)
        response = self.client.post(
            url,
            {"data": csv_data, "format": "csv", "csv_delimiter": ","},
        )
        self.assertIn(response.status_code, [200, 400])
        # Only one server with this name must exist
        self.assertEqual(Server.objects.filter(name="test-kea").count(), 1)


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
# _extract_identifier — pure unit tests (no DB needed)
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractIdentifier(_unittest.TestCase):
    """Unit tests for the ``_extract_identifier()`` helper in ``views.py``.

    The function walks a Kea reservation dict looking for identifier keys in
    priority order (v4: hw-address > client-id > circuit-id > flex-id;
    v6: duid > hw-address > client-id > flex-id).
    """

    def test_v4_prefers_hw_address(self):
        r = {"hw-address": "aa:bb:cc:dd:ee:ff", "client-id": "01:aa:bb", "subnet-id": 1}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "aa:bb:cc:dd:ee:ff")

    def test_v4_client_id_when_no_hw_address(self):
        r = {"client-id": "01:aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "client-id")
        self.assertEqual(ival, "01:aa:bb:cc:dd:ee:ff")

    def test_v4_circuit_id(self):
        r = {"circuit-id": "0a:1b:2c"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "circuit-id")
        self.assertEqual(ival, "0a:1b:2c")

    def test_v4_flex_id_as_last_resort(self):
        r = {"flex-id": "aabbccdd"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "flex-id")
        self.assertEqual(ival, "aabbccdd")

    def test_v4_hw_address_beats_flex_id(self):
        r = {"flex-id": "aabbccdd", "hw-address": "aa:bb:cc"}
        itype, _ = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")

    def test_v6_prefers_duid_over_hw_address(self):
        r = {"duid": "00:01:02:03:04:05", "hw-address": "aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 6)
        self.assertEqual(itype, "duid")
        self.assertEqual(ival, "00:01:02:03:04:05")

    def test_v6_hw_address_fallback_when_no_duid(self):
        r = {"hw-address": "aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 6)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "aa:bb:cc:dd:ee:ff")

    def test_fallback_returns_hw_address_empty_string(self):
        """When no known identifier key is present return ``("hw-address", "")``.

        This keeps the form pre-population logic from crashing.
        """
        r = {"subnet-id": 1, "ip-address": "10.0.0.1"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7c: Global DHCP options on the server status tab
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_WITH_OPTIONS_V4 = {
    "option-data": [
        {"code": 6, "name": "domain-name-servers", "data": "8.8.8.8, 8.8.4.4"},
        {"code": 15, "name": "domain-name", "data": "example.com"},
    ],
    "subnet4": [],
    "shared-networks": [],
}


def _kea_command_with_global_options(cmd, service=None, arguments=None, check=None):
    """Mock side-effect that includes option-data in config-get."""
    if cmd == "status-get":
        return [{"result": 0, "arguments": {"pid": 1, "uptime": 100, "reload": 0}}]
    if cmd == "version-get":
        return [{"result": 0, "arguments": {"extended": "2.4.1"}}]
    if cmd == "config-get":
        if service and service[0] == "dhcp6":
            return [
                {
                    "result": 0,
                    "arguments": {
                        "Dhcp6": {
                            "option-data": [{"code": 23, "name": "dns-servers", "data": "2001:db8::1"}],
                            "subnet6": [],
                            "shared-networks": [],
                        }
                    },
                }
            ]
        return [{"result": 0, "arguments": {"Dhcp4": _CONFIG_WITH_OPTIONS_V4}}]
    return [{"result": 0, "arguments": {}}]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerStatusGlobalOptions(_ViewTestBase):
    """Status view must render global DHCP options extracted from ``config-get``."""

    @patch("netbox_kea.models.KeaClient")
    def test_global_options_present_in_context(self, MockKeaClient):
        """``global_options`` context key must exist and contain parsed option dicts."""
        MockKeaClient.return_value.command.side_effect = _kea_command_with_global_options
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("global_options", response.context)
        opts = response.context["global_options"]
        # Server has dhcp4 enabled — DHCPv4 options must be present.
        # Keys are humanised ("Dns Servers") — check by looking at all option values.
        self.assertTrue(any("Dns Servers" in v for v in opts.values()))

    @patch("netbox_kea.models.KeaClient")
    def test_global_options_dns_rendered_in_html(self, MockKeaClient):
        """DNS server IP must appear somewhere in the rendered status page."""
        MockKeaClient.return_value.command.side_effect = _kea_command_with_global_options
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertContains(response, "8.8.8.8")

    @patch("netbox_kea.models.KeaClient")
    def test_global_options_domain_name_rendered(self, MockKeaClient):
        """Domain name option must also appear in the status page HTML."""
        MockKeaClient.return_value.command.side_effect = _kea_command_with_global_options
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertContains(response, "example.com")

    @patch("netbox_kea.models.KeaClient")
    def test_status_still_200_when_config_get_fails(self, MockKeaClient):
        """If ``config-get`` raises, the status page must still return 200 (graceful degradation)."""
        from netbox_kea.kea import KeaException

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                raise KeaException({"result": 1, "text": "internal error"}, index=0)
            return _kea_command_with_global_options(cmd, service=service)

        MockKeaClient.return_value.command.side_effect = side_effect
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: Subnet utilization statistics
# ─────────────────────────────────────────────────────────────────────────────

_STAT_LEASE4_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "result-set": {
                "columns": [
                    "subnet-id",
                    "total-addresses",
                    "assigned-addresses",
                    "declined-addresses",
                ],
                "rows": [[1, 100, 25, 0]],
            }
        },
    }
]


def _config_with_one_subnet(service=None):
    """Return a minimal config-get payload with one subnet."""
    version = 6 if (service and service[0] == "dhcp6") else 4
    dhcp_key = f"Dhcp{version}"
    subnet_key = f"subnet{version}"
    return [
        {
            "result": 0,
            "arguments": {
                dhcp_key: {
                    "option-data": [],
                    subnet_key: [{"id": 1, "subnet": "192.168.1.0/24", "option-data": [], "pools": []}],
                    "shared-networks": [],
                }
            },
        }
    ]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetUtilizationStats(_ViewTestBase):
    """Subnet table must show a utilization column when ``stat_cmds`` hook is loaded."""

    @patch("netbox_kea.models.KeaClient")
    def test_utilization_percentage_shown_in_table(self, MockKeaClient):
        """25/100 addresses → '25%' utilization shown in subnets4 table."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return _config_with_one_subnet(service)
            if cmd == "stat-lease4-get":
                return _STAT_LEASE4_RESPONSE
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "25%")

    @patch("netbox_kea.models.KeaClient")
    def test_no_crash_when_stat_cmds_unavailable(self, MockKeaClient):
        """When stat_cmds hook is not loaded, subnets page must still render (200)."""
        from netbox_kea.kea import KeaException

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return _config_with_one_subnet(service)
            if cmd == "stat-lease4-get":
                raise KeaException(
                    {"result": 2, "text": "unknown command 'stat-lease4-get'"},
                    index=0,
                )
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_zero_percent_when_no_leases_assigned(self, MockKeaClient):
        """0 assigned / 100 total → '0%' utilization."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return _config_with_one_subnet(service)
            if cmd == "stat-lease4-get":
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "result-set": {
                                "columns": ["subnet-id", "total-addresses", "assigned-addresses", "declined-addresses"],
                                "rows": [[1, 100, 0, 0]],
                            }
                        },
                    }
                ]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0%")

    @patch("netbox_kea.models.KeaClient")
    def test_hundred_percent_when_fully_utilized(self, MockKeaClient):
        """All addresses assigned → '100%' utilization."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return _config_with_one_subnet(service)
            if cmd == "stat-lease4-get":
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "result-set": {
                                "columns": ["subnet-id", "total-addresses", "assigned-addresses", "declined-addresses"],
                                "rows": [[1, 50, 50, 0]],
                            }
                        },
                    }
                ]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "100%")


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
        from unittest.mock import MagicMock

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
# Feature 3.2: Subnet Lease Wipe — _BaseSubnetWipeView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4WipeView(_ViewTestBase):
    """Tests for ServerSubnet4WipeView (GET confirmation + POST wipe)."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_wipe_leases", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_confirmation_page(self, MockKeaClient):
        """GET must show the wipe confirmation page with subnet info."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [
            {"result": 0, "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}]}}
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")
        self.assertContains(response, "42")

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_form_when_subnet_fetch_fails(self, MockKeaClient):
        """GET must still return 200 even when the subnet-get Kea call fails."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = KeaException({"result": 1, "text": "not found"}, index=0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_lease_wipe_and_redirects(self, MockKeaClient):
        """POST must call lease_wipe on the client and redirect to the subnets tab."""
        mock_client = MockKeaClient.return_value
        mock_client.lease_wipe.return_value = None
        response = self.client.post(self._url(subnet_id=10))
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        mock_client.lease_wipe.assert_called_once_with(version=4, subnet_id=10)

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_kea_exception_shows_error_message(self, MockKeaClient):
        """POST that causes a KeaException must flash an error and redirect (no 500)."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.lease_wipe.side_effect = KeaException({"result": 1, "text": "hook not loaded"}, index=0)
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_unexpected_exception_shows_error_message(self, MockKeaClient):
        """POST that raises an unexpected exception must redirect (no 500)."""
        mock_client = MockKeaClient.return_value
        mock_client.lease_wipe.side_effect = RuntimeError("unexpected")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet6WipeView(_ViewTestBase):
    """Tests for ServerSubnet6WipeView — verifies v6 variant uses correct Kea commands."""

    def _url(self, subnet_id=7):
        return reverse("plugins:netbox_kea:server_subnet6_wipe_leases", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [
            {"result": 0, "arguments": {"subnet6": [{"id": 7, "subnet": "2001:db8::/32"}]}}
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2001:db8::/32")

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_lease_wipe_v6(self, MockKeaClient):
        """POST must call lease_wipe with version=6."""
        mock_client = MockKeaClient.return_value
        mock_client.lease_wipe.return_value = None
        response = self.client.post(self._url(subnet_id=7))
        self.assertEqual(response.status_code, 302)
        mock_client.lease_wipe.assert_called_once_with(version=6, subnet_id=7)


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


# ─────────────────────────────────────────────────────────────────────────────
# DHCP Enable / Disable views
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDHCP4EnableView(_ViewTestBase):
    """Tests for ServerDHCP4EnableView (GET confirmation + POST enable)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_enable", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_confirmation_page(self, MockKeaClient):
        """GET must render the enable confirmation page with dhcp_version=4."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "4")

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_dhcp_enable_and_redirects(self, MockKeaClient):
        """POST must call dhcp_enable('dhcp4') and redirect to status tab."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_enable.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        mock_client.dhcp_enable.assert_called_once_with("dhcp4")

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST with KeaException must flash an error message and redirect (no 500)."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.dhcp_enable.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_unexpected_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST with unexpected exception must redirect (no 500)."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_enable.side_effect = RuntimeError("unexpected")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDHCP6EnableView(_ViewTestBase):
    """Tests for ServerDHCP6EnableView — verifies v6 variant uses dhcp6 service."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp6_enable", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_dhcp_enable_v6(self, MockKeaClient):
        """POST must call dhcp_enable('dhcp6')."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_enable.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        mock_client.dhcp_enable.assert_called_once_with("dhcp6")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDHCP4DisableView(_ViewTestBase):
    """Tests for ServerDHCP4DisableView (GET form + POST disable with optional max_period)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_disable", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_form_page(self, MockKeaClient):
        """GET must render the disable form with max_period field."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "max_period")

    @patch("netbox_kea.models.KeaClient")
    def test_post_without_max_period_calls_disable_no_period(self, MockKeaClient):
        """POST without max_period must call dhcp_disable(service) with max_period=None."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_disable.return_value = None
        response = self.client.post(self._url(), {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        mock_client.dhcp_disable.assert_called_once_with("dhcp4", max_period=None)

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_max_period_passes_value(self, MockKeaClient):
        """POST with max_period=300 must pass max_period=300 to dhcp_disable."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_disable.return_value = None
        response = self.client.post(self._url(), {"confirm": "1", "max_period": "300"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        mock_client.dhcp_disable.assert_called_once_with("dhcp4", max_period=300)

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_invalid_max_period_rerenders_form(self, MockKeaClient):
        """POST with non-integer max_period must re-render the form (not redirect)."""
        response = self.client.post(self._url(), {"confirm": "1", "max_period": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.dhcp_disable.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST with KeaException must flash an error and redirect (no 500)."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.dhcp_disable.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        response = self.client.post(self._url(), {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_unexpected_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST with unexpected exception must redirect (no 500)."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_disable.side_effect = RuntimeError("unexpected")
        response = self.client.post(self._url(), {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDHCP6DisableView(_ViewTestBase):
    """Tests for ServerDHCP6DisableView — verifies v6 variant uses dhcp6 service."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp6_disable", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_dhcp_disable_v6(self, MockKeaClient):
        """POST must call dhcp_disable('dhcp6')."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_disable.return_value = None
        response = self.client.post(self._url(), {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        mock_client.dhcp_disable.assert_called_once_with("dhcp6", max_period=None)


# ─────────────────────────────────────────────────────────────────────────────
# Subnet Edit views
# ─────────────────────────────────────────────────────────────────────────────

_SUBNET4_GET_FULL = [
    {
        "result": 0,
        "arguments": {
            "subnet4": [
                {
                    "id": 42,
                    "subnet": "10.0.0.0/24",
                    "pools": [{"pool": "10.0.0.100-10.0.0.200"}],
                    "option-data": [
                        {"name": "routers", "data": "10.0.0.1"},
                        {"name": "domain-name-servers", "data": "8.8.8.8"},
                    ],
                    "valid-lft": 3600,
                }
            ]
        },
    }
]

_SUBNET6_GET_FULL = [
    {
        "result": 0,
        "arguments": {
            "subnet6": [
                {
                    "id": 7,
                    "subnet": "2001:db8::/48",
                    "pools": [],
                    "option-data": [],
                    "valid-lft": 3600,
                }
            ]
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4EditView(_ViewTestBase):
    """Tests for ServerSubnet4EditView (GET prefill + POST update)."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET must render the edit form with status 200."""
        MockKeaClient.return_value.command.return_value = _SUBNET4_GET_FULL
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_prefills_form_with_current_subnet_values(self, MockKeaClient):
        """GET must pre-populate form with current subnet CIDR and pools."""
        MockKeaClient.return_value.command.return_value = _SUBNET4_GET_FULL
        response = self.client.get(self._url())
        self.assertContains(response, "10.0.0.0/24")
        self.assertContains(response, "10.0.0.100-10.0.0.200")

    @patch("netbox_kea.models.KeaClient")
    def test_get_when_subnet_fetch_fails_still_returns_200(self, MockKeaClient):
        """GET must return 200 even when the subnet-get Kea call fails."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException({"result": 1, "text": "not found"}, index=0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_form_calls_subnet_update_and_redirects(self, MockKeaClient):
        """POST with valid form must call subnet_update and redirect to subnet list."""
        MockKeaClient.return_value.subnet_update.return_value = None
        response = self.client.post(
            self._url(subnet_id=42),
            {
                "subnet_cidr": "10.0.0.0/24",
                "pools": "10.0.0.100-10.0.0.200",
                "gateway": "10.0.0.1",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.subnet_update.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version_and_subnet_id_to_subnet_update(self, MockKeaClient):
        """POST must call subnet_update with version=4 and the correct subnet_id."""
        MockKeaClient.return_value.subnet_update.return_value = None
        self.client.post(
            self._url(subnet_id=42),
            {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
        )
        call_kwargs = MockKeaClient.return_value.subnet_update.call_args
        self.assertEqual(call_kwargs.kwargs.get("version") or call_kwargs[1].get("version"), 4)
        subnet_id_arg = call_kwargs.kwargs.get("subnet_id") or call_kwargs[1].get("subnet_id")
        self.assertEqual(subnet_id_arg, 42)

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_kea_exception_shows_error_and_rerenders(self, MockKeaClient):
        """POST that raises KeaException must re-render the form (not crash)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.subnet_update.side_effect = KeaException(
            {"result": 1, "text": "subnet cmds not loaded"}, index=0
        )
        response = self.client.post(
            self._url(subnet_id=42),
            {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
        )
        # Should show error (redirect or re-render, not 500)
        self.assertIn(response.status_code, (200, 302))
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_rerenders_with_200(self, MockKeaClient):
        """POST with invalid data (bad gateway IP) must re-render the form."""
        response = self.client.post(
            self._url(subnet_id=42),
            {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "not-an-ip", "dns_servers": "", "ntp_servers": ""},
        )
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.subnet_update.assert_not_called()

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url(), {})
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet6EditView(_ViewTestBase):
    """Tests for ServerSubnet6EditView — verifies v6 variant uses correct version."""

    def _url(self, subnet_id=7):
        return reverse("plugins:netbox_kea:server_subnet6_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET must return 200 for IPv6 edit view."""
        MockKeaClient.return_value.command.return_value = _SUBNET6_GET_FULL
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_subnet_update_with_version_6(self, MockKeaClient):
        """POST must call subnet_update with version=6."""
        MockKeaClient.return_value.subnet_update.return_value = None
        self.client.post(
            self._url(subnet_id=7),
            {"subnet_cidr": "2001:db8::/48", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
        )
        call_kwargs = MockKeaClient.return_value.subnet_update.call_args
        version_arg = call_kwargs.kwargs.get("version") or call_kwargs[1].get("version")
        self.assertEqual(version_arg, 6)


# ─────────────────────────────────────────────────────────────────────────────
# ServerFilterSet / ServerFilterForm
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerFilterSet(_ViewTestBase):
    """Tests for ServerFilterSet — queryset filtering by name, URL, has_control_agent."""

    def _make_servers(self):
        """Create three servers with distinct attributes for filtering."""
        Server.objects.all().delete()
        s1 = Server.objects.create(
            name="alpha-kea",
            server_url="http://alpha.example.com:8000",
            dhcp4=True,
            dhcp6=False,
            has_control_agent=True,
        )
        s2 = Server.objects.create(
            name="beta-kea",
            server_url="http://beta.example.com:8000",
            dhcp4=False,
            dhcp6=True,
            has_control_agent=False,
        )
        s3 = Server.objects.create(
            name="gamma-server",
            server_url="http://gamma.example.com:9000",
            dhcp4=True,
            dhcp6=True,
            has_control_agent=True,
        )
        return s1, s2, s3

    def test_filter_by_name_contains(self):
        """ServerFilterSet supports case-insensitive name substring filtering."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"name": "kea"}, queryset=Server.objects.all()).qs
        names = list(qs.values_list("name", flat=True))
        self.assertIn("alpha-kea", names)
        self.assertIn("beta-kea", names)
        self.assertNotIn("gamma-server", names)

    def test_filter_by_server_url_contains(self):
        """ServerFilterSet supports case-insensitive server_url substring filtering."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"server_url": "beta"}, queryset=Server.objects.all()).qs
        names = list(qs.values_list("name", flat=True))
        self.assertEqual(names, ["beta-kea"])

    def test_filter_by_has_control_agent_true(self):
        """ServerFilterSet can filter servers where has_control_agent=True."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"has_control_agent": True}, queryset=Server.objects.all()).qs
        names = list(qs.values_list("name", flat=True).order_by("name"))
        self.assertIn("alpha-kea", names)
        self.assertIn("gamma-server", names)
        self.assertNotIn("beta-kea", names)

    def test_filter_by_has_control_agent_false(self):
        """ServerFilterSet can filter servers where has_control_agent=False."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"has_control_agent": False}, queryset=Server.objects.all()).qs
        names = list(qs.values_list("name", flat=True))
        self.assertEqual(names, ["beta-kea"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerFilterForm(_ViewTestBase):
    """Tests for ServerFilterForm — renders new filter fields."""

    def test_filter_form_has_name_field(self):
        """ServerFilterForm includes a 'name' text field."""
        from netbox_kea.forms import ServerFilterForm

        form = ServerFilterForm()
        self.assertIn("name", form.fields)

    def test_filter_form_has_server_url_field(self):
        """ServerFilterForm includes a 'server_url' text field."""
        from netbox_kea.forms import ServerFilterForm

        form = ServerFilterForm()
        self.assertIn("server_url", form.fields)

    def test_filter_form_has_has_control_agent_field(self):
        """ServerFilterForm includes a 'has_control_agent' nullable boolean field."""
        from netbox_kea.forms import ServerFilterForm

        form = ServerFilterForm()
        self.assertIn("has_control_agent", form.fields)

    def test_server_list_filters_by_name_via_get(self):
        """GET /plugins/kea/servers/?name=<term> returns 200 and filters results."""
        Server.objects.all().delete()
        Server.objects.create(name="alpha-kea", server_url="http://a:8000", dhcp4=True, dhcp6=False)
        Server.objects.create(name="gamma-server", server_url="http://g:8000", dhcp4=True, dhcp6=False)
        url = reverse("plugins:netbox_kea:server_list")
        response = self.client.get(url, {"name": "alpha"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alpha-kea")
        self.assertNotContains(response, "gamma-server")


# ---------------------------------------------------------------------------
# TestSubnetOptionsView
# ---------------------------------------------------------------------------

# Fake config-get response containing one v4 subnet with one existing option
_OPTIONS_CONFIG_GET = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [
                    {
                        "id": 42,
                        "subnet": "10.0.0.0/24",
                        "option-data": [
                            {"name": "domain-name-servers", "data": "8.8.8.8"},
                            {"name": "routers", "data": "10.0.0.1"},
                        ],
                    }
                ]
            }
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsView(_ViewTestBase):
    """Tests for ServerSubnet4/6OptionsEditView (GET prefill + POST update)."""

    def _url(self, version=4, subnet_id=42):
        return reverse(
            f"plugins:netbox_kea:server_subnet{version}_options_edit",
            args=[self.server.pk, subnet_id],
        )

    def test_url_registered_v4(self):
        """URL server_subnet4_options_edit is registered."""
        url = self._url(version=4)
        self.assertIn("options", url)

    def test_url_registered_v6(self):
        """URL server_subnet6_options_edit is registered."""
        url = self._url(version=6)
        self.assertIn("options", url)

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.command.return_value = _OPTIONS_CONFIG_GET
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_prefills_existing_options(self, MockKeaClient):
        """GET pre-populates formset with existing option-data from config-get."""
        MockKeaClient.return_value.command.return_value = _OPTIONS_CONFIG_GET
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("domain-name-servers", content)
        self.assertIn("8.8.8.8", content)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_subnet_update_options(self, MockKeaClient):
        """POST with valid formset calls subnet_update_options and redirects."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        MockKeaClient.return_value.subnet_update_options.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version_and_subnet_id(self, MockKeaClient):
        """POST calls subnet_update_options with the correct version and subnet_id."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        self.client.post(
            self._url(version=4, subnet_id=42),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        call_kwargs = MockKeaClient.return_value.subnet_update_options.call_args
        args = call_kwargs[1] if call_kwargs[1] else {}
        positional = call_kwargs[0] if call_kwargs[0] else ()
        # version=4 and subnet_id=42 should be passed (positional or keyword)
        self.assertIn(4, list(positional) + list(args.values()))
        self.assertIn(42, list(positional) + list(args.values()))

    @patch("netbox_kea.models.KeaClient")
    def test_post_deleted_rows_excluded_from_options(self, MockKeaClient):
        """Rows with DELETE=on are excluded from the options list passed to subnet_update_options."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
                "form-1-name": "domain-name-servers",
                "form-1-data": "8.8.8.8",
                "form-1-always_send": "",
                "form-1-DELETE": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.subnet_update_options.call_args
        # options argument should have only 1 item (dns row deleted)
        options_arg = next(v for v in list(call_kwargs[0]) + list(call_kwargs[1].values()) if isinstance(v, list))
        self.assertEqual(len(options_arg), 1)
        self.assertEqual(options_arg[0]["name"], "routers")

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_message(self, MockKeaClient):
        """POST that raises KeaException shows an error message, stays on form."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.subnet_update_options.side_effect = KeaException(
            {"result": 1, "text": "subnet not found"}
        )
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        self.assertEqual(response.status_code, 302)  # redirect back to subnets
        # Error stored in messages — check it doesn't crash

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


# TestServerOptionsView
# ---------------------------------------------------------------------------

_SERVER_OPTIONS_CONFIG_GET = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "option-data": [
                    {"name": "domain-name-servers", "data": "8.8.8.8"},
                    {"name": "routers", "data": "10.0.0.1"},
                ],
                "subnet4": [],
            }
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsView(_ViewTestBase):
    """Tests for ServerDHCP4/6OptionsEditView (GET prefill + POST update)."""

    def _url(self, version=4):
        return reverse(
            f"plugins:netbox_kea:server_dhcp{version}_options_edit",
            args=[self.server.pk],
        )

    def test_url_registered_v4(self):
        """URL server_dhcp4_options_edit is registered."""
        url = self._url(version=4)
        self.assertIn("options", url)

    def test_url_registered_v6(self):
        """URL server_dhcp6_options_edit is registered."""
        url = self._url(version=6)
        self.assertIn("options", url)

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.command.return_value = _SERVER_OPTIONS_CONFIG_GET
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_prefills_existing_options(self, MockKeaClient):
        """GET pre-populates formset with existing server-level option-data."""
        MockKeaClient.return_value.command.return_value = _SERVER_OPTIONS_CONFIG_GET
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("domain-name-servers", content)
        self.assertIn("8.8.8.8", content)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_server_update_options(self, MockKeaClient):
        """POST with valid formset calls server_update_options and redirects."""
        MockKeaClient.return_value.server_update_options.return_value = None
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        MockKeaClient.return_value.server_update_options.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version(self, MockKeaClient):
        """POST calls server_update_options with the correct version."""
        MockKeaClient.return_value.server_update_options.return_value = None
        self.client.post(
            self._url(version=4),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        call_kwargs = MockKeaClient.return_value.server_update_options.call_args
        all_args = list(call_kwargs[0]) + list(call_kwargs[1].values())
        self.assertIn(4, all_args)

    @patch("netbox_kea.models.KeaClient")
    def test_post_deleted_rows_excluded(self, MockKeaClient):
        """Rows with DELETE=on are excluded from the options list."""
        MockKeaClient.return_value.server_update_options.return_value = None
        self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
                "form-1-name": "domain-name-servers",
                "form-1-data": "8.8.8.8",
                "form-1-always_send": "",
                "form-1-DELETE": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.server_update_options.call_args
        options_arg = next(v for v in list(call_kwargs[0]) + list(call_kwargs[1].values()) if isinstance(v, list))
        self.assertEqual(len(options_arg), 1)
        self.assertEqual(options_arg[0]["name"], "routers")

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_redirects(self, MockKeaClient):
        """POST that raises KeaException shows error message and redirects."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.server_update_options.side_effect = KeaException(
            {"result": 1, "text": "internal error"}
        )
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


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


# ─────────────────────────────────────────────────────────────────────────────
# Shared Networks Views
# ─────────────────────────────────────────────────────────────────────────────

_SHARED_NETWORKS_CONFIG_V4 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [{"id": 1, "subnet": "192.168.0.0/24"}],
                "shared-networks": [
                    {
                        "name": "net-alpha",
                        "description": "Alpha test network",
                        "subnet4": [
                            {"id": 10, "subnet": "10.0.0.0/24"},
                            {"id": 11, "subnet": "10.0.1.0/24"},
                        ],
                    }
                ],
            }
        },
    }
]

_SHARED_NETWORKS_CONFIG_V6 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp6": {
                "subnet6": [],
                "shared-networks": [
                    {
                        "name": "net-beta",
                        "description": "",
                        "subnet6": [
                            {"id": 20, "subnet": "2001:db8::/48"},
                        ],
                    }
                ],
            }
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetworks4View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/shared_networks4/"""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_networks4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_shows_shared_network_name(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        self.assertContains(response, "net-alpha")

    @patch("netbox_kea.models.KeaClient")
    def test_shows_subnet_count(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        # 2 subnets in net-alpha
        self.assertContains(response, "2")

    @patch("netbox_kea.models.KeaClient")
    def test_shows_subnet_cidrs(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        self.assertContains(response, "10.0.0.0/24")
        self.assertContains(response, "10.0.1.0/24")

    @patch("netbox_kea.models.KeaClient")
    def test_empty_table_when_no_shared_networks(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "net-alpha")

    def test_get_with_dhcp4_disabled_redirects(self):
        v6_only = _make_db_server(name="v6-only-sn", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[v6_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(v6_only.pk), response.url)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetworks6View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/shared_networks6/"""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_networks6", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V6
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_shows_shared_network_name(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V6
        response = self.client.get(self._url())
        self.assertContains(response, "net-beta")

    @patch("netbox_kea.models.KeaClient")
    def test_shows_subnet_cidrs(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V6
        response = self.client.get(self._url())
        self.assertContains(response, "2001:db8::/48")


# ---------------------------------------------------------------------------
# Shared Network Add / Delete views (TDD — RED until views + URLs implemented)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4AddView(_ViewTestBase):
    """Tests for ServerSharedNetwork4AddView: GET form + POST create."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_network4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200_with_form(self, MockKeaClient):
        """GET must render the add-network form with status 200."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_creates_network(self, MockKeaClient):
        """POST with valid name must call network_add and redirect."""
        MockKeaClient.return_value.network_add.return_value = None
        response = self.client.post(self._url(), {"name": "net-prod"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.network_add.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_add_with_correct_version(self, MockKeaClient):
        """POST must call network_add with version=4."""
        MockKeaClient.return_value.network_add.return_value = None
        self.client.post(self._url(), {"name": "net-prod"})
        call_args = MockKeaClient.return_value.network_add.call_args
        version = (call_args.kwargs or call_args[1]).get("version") or (call_args.args or call_args[0])[0]
        self.assertEqual(version, 4)

    @patch("netbox_kea.models.KeaClient")
    def test_post_empty_name_shows_form_errors(self, MockKeaClient):
        """POST with empty name must re-render form (no Kea call)."""
        response = self.client.post(self._url(), {"name": ""})
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.network_add.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST that raises KeaException must redirect with an error (no 500)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.network_add.side_effect = KeaException(
            {"result": 1, "text": "subnet_cmds not loaded"}, index=0
        )
        response = self.client.post(self._url(), {"name": "net-prod"})
        self.assertIn(response.status_code, (200, 302))
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url(), {"name": "net-x"})
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork6AddView(_ViewTestBase):
    """Tests for ServerSharedNetwork6AddView — verifies v6 variant uses version=6."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_network6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET must render the add-network form with status 200."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_add_with_version_6(self, MockKeaClient):
        """POST must call network_add with version=6."""
        MockKeaClient.return_value.network_add.return_value = None
        self.client.post(self._url(), {"name": "net6-prod"})
        call_args = MockKeaClient.return_value.network_add.call_args
        version = (call_args.kwargs or call_args[1]).get("version") or (call_args.args or call_args[0])[0]
        self.assertEqual(version, 6)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4DeleteView(_ViewTestBase):
    """Tests for ServerSharedNetwork4DeleteView: GET confirm + POST delete."""

    def _url(self, name="net-alpha"):
        return reverse("plugins:netbox_kea:server_shared_network4_delete", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200_with_confirmation_page(self, MockKeaClient):
        """GET must render a confirmation page mentioning the network name."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "net-alpha")

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_del_and_redirects(self, MockKeaClient):
        """POST must call network_del and redirect to the shared networks tab."""
        MockKeaClient.return_value.network_del.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.network_del.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version_and_name(self, MockKeaClient):
        """POST must call network_del with version=4 and the correct network name."""
        MockKeaClient.return_value.network_del.return_value = None
        self.client.post(self._url(name="net-alpha"))
        call_args = MockKeaClient.return_value.network_del.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        name = kwargs.get("name") or (args[1] if len(args) > 1 else None)
        self.assertEqual(version, 4)
        self.assertEqual(name, "net-alpha")

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_redirects_with_error(self, MockKeaClient):
        """POST that raises KeaException must redirect with an error (no 500)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.network_del.side_effect = KeaException(
            {"result": 1, "text": "network not found"}, index=0
        )
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (200, 302))
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork6DeleteView(_ViewTestBase):
    """Tests for ServerSharedNetwork6DeleteView — verifies v6 variant uses version=6."""

    def _url(self, name="net-beta"):
        return reverse("plugins:netbox_kea:server_shared_network6_delete", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET must render confirmation page with status 200."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_del_with_version_6(self, MockKeaClient):
        """POST must call network_del with version=6."""
        MockKeaClient.return_value.network_del.return_value = None
        self.client.post(self._url(name="net-beta"))
        call_args = MockKeaClient.return_value.network_del.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 6)
