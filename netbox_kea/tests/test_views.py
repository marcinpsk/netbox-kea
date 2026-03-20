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
from netbox_kea.views import _extract_identifier

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
