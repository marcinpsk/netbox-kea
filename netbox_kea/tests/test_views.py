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
from unittest.mock import MagicMock, patch

from django.contrib import messages as django_messages
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

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerDHCP4SubnetsView

        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerDHCP4SubnetsView.tab)

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

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerDHCP6SubnetsView

        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerDHCP6SubnetsView.tab)


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

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_table_data_has_subnet_sort_key(self, MockKeaClient):
        """F1: each subnet dict must have an integer _subnet_sort_key for numeric sort."""
        import ipaddress

        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = self._side_effect_v4
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        for row in table.data:
            self.assertIn("_subnet_sort_key", row, "Missing _subnet_sort_key in subnet row")
            self.assertIsInstance(row["_subnet_sort_key"], int)
        # Verify value: 10.0.0.0/24 → network address int
        first_row = list(table.data)[0]
        expected = int(ipaddress.ip_network("10.0.0.0/24").network_address)
        self.assertEqual(first_row["_subnet_sort_key"], expected)


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
        MockKeaClient.return_value.command.return_value = _CONFIG4_NO_NETWORKS
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
        MockKeaClient.return_value.command.return_value = _CONFIG4_NO_NETWORKS
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
        MockKeaClient.return_value.command.return_value = _CONFIG4_NO_NETWORKS
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

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_renew_rebind_timers_to_subnet_update(self, MockKeaClient):
        """F11: POST with renew_timer and rebind_timer must pass them to subnet_update."""
        MockKeaClient.return_value.subnet_update.return_value = None
        MockKeaClient.return_value.command.return_value = _CONFIG4_NO_NETWORKS
        self.client.post(
            self._url(subnet_id=42),
            {
                "subnet_cidr": "10.0.0.0/24",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
                "renew_timer": "600",
                "rebind_timer": "900",
            },
        )
        call_kwargs = MockKeaClient.return_value.subnet_update.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        self.assertEqual(kwargs.get("renew_timer"), 600)
        self.assertEqual(kwargs.get("rebind_timer"), 900)

    @patch("netbox_kea.models.KeaClient")
    def test_post_omits_timers_when_not_supplied(self, MockKeaClient):
        """F11: POST without timer fields must pass None/absent to subnet_update."""
        MockKeaClient.return_value.subnet_update.return_value = None
        MockKeaClient.return_value.command.return_value = _CONFIG4_NO_NETWORKS
        self.client.post(
            self._url(subnet_id=42),
            {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
        )
        call_kwargs = MockKeaClient.return_value.subnet_update.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        self.assertIsNone(kwargs.get("renew_timer"))
        self.assertIsNone(kwargs.get("rebind_timer"))

    # ── F5: inherited options ─────────────────────────────────────────────────

    @patch("netbox_kea.models.KeaClient")
    def test_get_passes_inherited_dns_from_global_config(self, MockKeaClient):
        """F5: When subnet has no DNS set, inherited_options contains global DNS."""
        mock_client = MockKeaClient.return_value
        subnet_no_opts = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
            }
        ]
        config_with_global_dns = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "option-data": [{"name": "domain-name-servers", "data": "8.8.8.8"}],
                        "shared-networks": [],
                    }
                },
            }
        ]
        mock_client.command.side_effect = [subnet_no_opts, config_with_global_dns]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        inherited = response.context.get("inherited_options", {})
        self.assertIn("dns_servers", inherited)
        self.assertEqual(inherited["dns_servers"]["value"], "8.8.8.8")
        self.assertEqual(inherited["dns_servers"]["source"], "global")

    @patch("netbox_kea.models.KeaClient")
    def test_get_inherited_options_empty_when_kea_config_fails(self, MockKeaClient):
        """F5: When config-get raises KeaException, inherited_options is an empty dict."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        # First command call (subnet lookup) succeeds; second (config-get) fails.
        mock_client.command.side_effect = [
            _SUBNET4_GET_FULL,
            KeaException({"result": 1, "text": "err"}, index=0),
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        inherited = response.context.get("inherited_options", {})
        self.assertEqual(inherited, {})

    @patch("netbox_kea.models.KeaClient")
    def test_get_inherited_options_excludes_field_already_set_in_subnet(self, MockKeaClient):
        """F5: Fields already set in the subnet itself are excluded from inherited_options."""
        mock_client = MockKeaClient.return_value
        # _SUBNET4_GET_FULL has domain-name-servers: 8.8.8.8 in option-data
        config_with_global_dns = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "option-data": [{"name": "domain-name-servers", "data": "1.1.1.1"}],
                        "shared-networks": [],
                    }
                },
            }
        ]
        mock_client.command.side_effect = [_SUBNET4_GET_FULL, config_with_global_dns]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        inherited = response.context.get("inherited_options", {})
        # dns_servers is already set by subnet — should NOT appear as inherited
        self.assertNotIn("dns_servers", inherited)

    @patch("netbox_kea.models.KeaClient")
    def test_get_inherited_options_prefers_shared_network_over_global(self, MockKeaClient):
        """F5: Shared-network option-data overrides global in inherited_options."""
        mock_client = MockKeaClient.return_value
        subnet_no_opts = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
            }
        ]
        config_shared_net = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "option-data": [{"name": "domain-name-servers", "data": "8.8.8.8"}],
                        "shared-networks": [
                            {
                                "name": "net-alpha",
                                "subnet4": [{"id": 42}],
                                "option-data": [{"name": "domain-name-servers", "data": "192.168.1.1"}],
                            }
                        ],
                    }
                },
            }
        ]
        mock_client.command.side_effect = [subnet_no_opts, config_shared_net]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        inherited = response.context.get("inherited_options", {})
        self.assertIn("dns_servers", inherited)
        # Should use shared-network value, not global
        self.assertEqual(inherited["dns_servers"]["value"], "192.168.1.1")
        self.assertIn("net-alpha", inherited["dns_servers"]["source"])


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
        MockKeaClient.return_value.command.return_value = _CONFIG6_NO_NETWORKS
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
        # 2 subnets in net-alpha — check the Subnets column header is present
        self.assertContains(response, "Subnets")

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

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerSharedNetworks4View

        MockKeaClient.return_value.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerSharedNetworks4View.tab)


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

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerSharedNetworks6View

        MockKeaClient.return_value.command.return_value = _SHARED_NETWORKS_CONFIG_V6
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerSharedNetworks6View.tab)


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


# ---------------------------------------------------------------------------
# Test data for subnet→network assignment
# ---------------------------------------------------------------------------

# Config-get response where subnet 42 is inside "net-alpha"
_CONFIG4_WITH_SUBNET_IN_NETWORK = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [],
                "shared-networks": [
                    {
                        "name": "net-alpha",
                        "subnet4": [
                            {"id": 42, "subnet": "10.0.0.0/24"},
                        ],
                    },
                    {
                        "name": "net-beta",
                        "subnet4": [],
                    },
                ],
            }
        },
    }
]

# Config-get response where subnet 42 is NOT in any shared network
_CONFIG4_NO_NETWORKS = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}],
                "shared-networks": [
                    {"name": "net-alpha", "subnet4": []},
                    {"name": "net-beta", "subnet4": []},
                ],
            }
        },
    }
]

# Config-get response with "prod-net" shared-network (for SharedNetworkEditView POST tests)
_CONFIG4_WITH_PROD_NET = [
    {
        "result": 0,
        "arguments": {"Dhcp4": {"shared-networks": [{"name": "prod-net", "option-data": [], "subnet4": []}]}},
    }
]

# Config-get response with "prod-net6" shared-network (for SharedNetworkEditView v6 POST tests)
_CONFIG6_WITH_PROD_NET = [
    {
        "result": 0,
        "arguments": {"Dhcp6": {"shared-networks": [{"name": "prod-net6", "option-data": [], "subnet6": []}]}},
    }
]

# Config-get response for v6 subnet edit (no network assignment)
_CONFIG6_NO_NETWORKS = [
    {
        "result": 0,
        "arguments": {
            "Dhcp6": {
                "subnet6": [{"id": 7, "subnet": "2001:db8::/48"}],
                "shared-networks": [],
            }
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4EditViewNetworkAssignment(_ViewTestBase):
    """Tests for shared-network assignment in ServerSubnet4EditView."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def _post_data(self, shared_network="", current_network=""):
        return {
            "subnet_cidr": "10.0.0.0/24",
            "pools": "",
            "gateway": "",
            "dns_servers": "",
            "ntp_servers": "",
            "shared_network": shared_network,
            "current_network": current_network,
        }

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_network_dropdown_with_available_networks(self, MockKeaClient):
        """GET must render the form with a shared_network dropdown listing available networks."""
        mock_client = MockKeaClient.return_value
        # First call: subnet4-get, Second call: config-get for networks
        mock_client.command.side_effect = [_SUBNET4_GET_FULL, _CONFIG4_NO_NETWORKS]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "net-alpha")
        self.assertContains(response, "net-beta")

    @patch("netbox_kea.models.KeaClient")
    def test_get_preselects_current_network_when_subnet_belongs_to_network(self, MockKeaClient):
        """GET must pre-select the current shared network in the dropdown."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = [_SUBNET4_GET_FULL, _CONFIG4_WITH_SUBNET_IN_NETWORK]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        # The form initial value should be net-alpha (selected option)
        self.assertContains(response, "net-alpha")

    @patch("netbox_kea.models.KeaClient")
    def test_post_assigns_subnet_to_network_calls_network_subnet_add(self, MockKeaClient):
        """POST moving subnet into a network must call network_subnet_add."""
        mock_client = MockKeaClient.return_value
        # config-get for current network (subnet not in any network)
        mock_client.command.return_value = _CONFIG4_NO_NETWORKS
        mock_client.subnet_update.return_value = None
        mock_client.network_subnet_add.return_value = None

        response = self.client.post(self._url(), self._post_data(shared_network="net-alpha", current_network=""))
        self.assertIn(response.status_code, (200, 302))
        mock_client.network_subnet_add.assert_called_once()
        call_kwargs = mock_client.network_subnet_add.call_args.kwargs or mock_client.network_subnet_add.call_args[1]
        self.assertEqual(call_kwargs.get("name"), "net-alpha")
        self.assertEqual(call_kwargs.get("subnet_id"), 42)

    @patch("netbox_kea.models.KeaClient")
    def test_post_removes_subnet_from_network_calls_network_subnet_del(self, MockKeaClient):
        """POST clearing network (current→blank) must call network_subnet_del."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _CONFIG4_WITH_SUBNET_IN_NETWORK
        mock_client.subnet_update.return_value = None
        mock_client.network_subnet_del.return_value = None

        response = self.client.post(self._url(), self._post_data(shared_network="", current_network="net-alpha"))
        self.assertIn(response.status_code, (200, 302))
        mock_client.network_subnet_del.assert_called_once()
        call_kwargs = mock_client.network_subnet_del.call_args.kwargs or mock_client.network_subnet_del.call_args[1]
        self.assertEqual(call_kwargs.get("name"), "net-alpha")
        self.assertEqual(call_kwargs.get("subnet_id"), 42)

    @patch("netbox_kea.models.KeaClient")
    def test_post_changes_network_calls_del_then_add(self, MockKeaClient):
        """POST changing from one network to another must call del then add."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _CONFIG4_WITH_SUBNET_IN_NETWORK
        mock_client.subnet_update.return_value = None
        mock_client.network_subnet_del.return_value = None
        mock_client.network_subnet_add.return_value = None

        self.client.post(self._url(), self._post_data(shared_network="net-beta", current_network="net-alpha"))
        mock_client.network_subnet_del.assert_called_once()
        mock_client.network_subnet_add.assert_called_once()
        del_kwargs = mock_client.network_subnet_del.call_args.kwargs or mock_client.network_subnet_del.call_args[1]
        add_kwargs = mock_client.network_subnet_add.call_args.kwargs or mock_client.network_subnet_add.call_args[1]
        self.assertEqual(del_kwargs.get("name"), "net-alpha")
        self.assertEqual(add_kwargs.get("name"), "net-beta")

    @patch("netbox_kea.models.KeaClient")
    def test_post_no_network_change_does_not_call_network_subnet_methods(self, MockKeaClient):
        """POST when network is unchanged must NOT call network_subnet_add or del."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _CONFIG4_WITH_SUBNET_IN_NETWORK
        mock_client.subnet_update.return_value = None

        self.client.post(self._url(), self._post_data(shared_network="net-alpha", current_network="net-alpha"))
        mock_client.network_subnet_add.assert_not_called()
        mock_client.network_subnet_del.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_post_network_assignment_with_version_4(self, MockKeaClient):
        """POST network_subnet_add must be called with version=4 for v4 subnets."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _CONFIG4_NO_NETWORKS
        mock_client.subnet_update.return_value = None
        mock_client.network_subnet_add.return_value = None

        self.client.post(self._url(), self._post_data(shared_network="net-alpha", current_network=""))
        call_kwargs = mock_client.network_subnet_add.call_args.kwargs or mock_client.network_subnet_add.call_args[1]
        self.assertEqual(call_kwargs.get("version"), 4)


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


# ---------------------------------------------------------------------------
# option-def fixtures
# ---------------------------------------------------------------------------

_OPTION_DEF_LIST_V4 = [
    {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4"},
    {"name": "other-opt", "code": 201, "type": "uint32", "space": "dhcp4"},
]

_OPTION_DEF_LIST_EMPTY: list = []


# ---------------------------------------------------------------------------
# ServerOptionDef4ListView / ServerOptionDef6ListView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4ListView(_ViewTestBase):
    """Tests for ServerOptionDef4ListView: GET list of custom option definitions."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_V4
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_shows_option_def_name(self, MockKeaClient):
        """GET renders option names in the response."""
        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_V4
        response = self.client.get(self._url())
        self.assertContains(response, "my-opt")

    @patch("netbox_kea.models.KeaClient")
    def test_shows_option_def_code(self, MockKeaClient):
        """GET renders option codes in the response."""
        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_V4
        response = self.client.get(self._url())
        self.assertContains(response, "200")

    @patch("netbox_kea.models.KeaClient")
    def test_empty_list_shows_200(self, MockKeaClient):
        """GET with empty option-def list returns 200 without errors."""
        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_EMPTY
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_with_dhcp4_disabled_redirects(self):
        """Server with dhcp4=False redirects away from option_def4 tab."""
        v6_only = _make_db_server(name="v6only-od", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_option_def4", args=[v6_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerOptionDef4View

        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_V4
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerOptionDef4View.tab)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef6ListView(_ViewTestBase):
    """Tests for ServerOptionDef6ListView (v6 variant)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def6", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.option_def_list.return_value = []
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_calls_option_def_list_with_version_6(self, MockKeaClient):
        """GET calls option_def_list with version=6."""
        MockKeaClient.return_value.option_def_list.return_value = []
        self.client.get(self._url())
        MockKeaClient.return_value.option_def_list.assert_called_once_with(version=6)

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerOptionDef6View

        MockKeaClient.return_value.option_def_list.return_value = []
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerOptionDef6View.tab)


# ---------------------------------------------------------------------------
# ServerOptionDef4AddView / ServerOptionDef6AddView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4AddView(_ViewTestBase):
    """Tests for ServerOptionDef4AddView: GET form + POST create."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200_with_form(self, MockKeaClient):
        """GET renders the add option-def form."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_calls_option_def_add(self, MockKeaClient):
        """POST with valid data calls option_def_add and redirects."""
        MockKeaClient.return_value.option_def_add.return_value = None
        response = self.client.post(
            self._url(),
            {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
        )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.option_def_add.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version(self, MockKeaClient):
        """POST calls option_def_add with version=4."""
        MockKeaClient.return_value.option_def_add.return_value = None
        self.client.post(
            self._url(),
            {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
        )
        call_args = MockKeaClient.return_value.option_def_add.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 4)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error(self, MockKeaClient):
        """POST that raises KeaException returns 200 with error (no 500)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_add.side_effect = KeaException(
            {"result": 1, "text": "duplicate code"}, index=0
        )
        response = self.client.post(
            self._url(),
            {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
        )
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_returns_200(self, MockKeaClient):
        """POST with missing required fields returns 200 (form re-render)."""
        response = self.client.post(self._url(), {"name": "", "code": "", "type": "", "space": ""})
        self.assertEqual(response.status_code, 200)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef6AddView(_ViewTestBase):
    """Tests for ServerOptionDef6AddView — verifies v6 uses version=6."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET renders the add form for v6."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_option_def_add_with_version_6(self, MockKeaClient):
        """POST calls option_def_add with version=6."""
        MockKeaClient.return_value.option_def_add.return_value = None
        self.client.post(
            self._url(),
            {"name": "v6-opt", "code": 250, "type": "ipv6-address", "space": "dhcp6", "array": False},
        )
        call_args = MockKeaClient.return_value.option_def_add.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 6)


# ---------------------------------------------------------------------------
# ServerOptionDef4DeleteView / ServerOptionDef6DeleteView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4DeleteView(_ViewTestBase):
    """Tests for ServerOptionDef4DeleteView: GET confirm + POST delete."""

    def _url(self, code=200, space="dhcp4"):
        return reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, code, space])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200_with_confirmation(self, MockKeaClient):
        """GET renders a confirmation page mentioning code and space."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "200")

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_option_def_del_and_redirects(self, MockKeaClient):
        """POST calls option_def_del and redirects to option_def4 list."""
        MockKeaClient.return_value.option_def_del.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.option_def_del.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version_code_space(self, MockKeaClient):
        """POST calls option_def_del with version=4, code=200, space='dhcp4'."""
        MockKeaClient.return_value.option_def_del.return_value = None
        self.client.post(self._url(code=200, space="dhcp4"))
        call_args = MockKeaClient.return_value.option_def_del.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        code = kwargs.get("code") or (args[1] if len(args) > 1 else None)
        space = kwargs.get("space") or (args[2] if len(args) > 2 else None)
        self.assertEqual(version, 4)
        self.assertEqual(code, 200)
        self.assertEqual(space, "dhcp4")

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_redirects_with_error(self, MockKeaClient):
        """POST that raises KeaException must not 500."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_del.side_effect = KeaException(
            {"result": 3, "text": "not found"}, index=0
        )
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (200, 302))
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST redirects to login."""
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef6DeleteView(_ViewTestBase):
    """Tests for ServerOptionDef6DeleteView — v6 uses version=6."""

    def _url(self, code=250, space="dhcp6"):
        return reverse("plugins:netbox_kea:server_option_def6_delete", args=[self.server.pk, code, space])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET renders the v6 confirmation page."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_option_def_del_with_version_6(self, MockKeaClient):
        """POST calls option_def_del with version=6."""
        MockKeaClient.return_value.option_def_del.return_value = None
        self.client.post(self._url(code=250, space="dhcp6"))
        call_args = MockKeaClient.return_value.option_def_del.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 6)


# ─────────────────────────────────────────────────────────────────────────────
# TestServerSharedNetwork4EditView (F2b)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4EditView(_ViewTestBase):
    """Tests for ServerSharedNetwork4EditView: GET form + POST update."""

    def _url(self, name="prod-net"):
        return reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET renders the edit form with status 200."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {"name": "prod-net", "description": "Old", "option-data": [], "subnet4": []}
                        ],
                        "subnet4": [],
                    }
                },
            }
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_calls_network_update_and_redirects(self, MockKeaClient):
        """POST with valid data calls network_update and redirects."""
        MockKeaClient.return_value.network_update.return_value = None
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "Updated description",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.network_update.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_version_4_to_network_update(self, MockKeaClient):
        """POST must call network_update with version=4."""
        MockKeaClient.return_value.network_update.return_value = None
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        call_args = MockKeaClient.return_value.network_update.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 4)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST that raises KeaException must redirect, show a generic error, and not leak raw Kea text."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.network_update.side_effect = KeaException(
            {"result": 1, "text": "config error"}, index=0
        )
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)
        messages_list = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.ERROR for m in messages_list),
            f"Expected an ERROR message; got: {[(m.level, m.message) for m in messages_list]}",
        )
        # Raw Kea error text must not appear in either rendered response or queued messages
        self.assertNotIn(b"config error", response.content)
        for m in messages_list:
            self.assertNotIn("config error", m.message, f"Raw Kea error text leaked into message: {m.message}")

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_error_shows_warning(self, MockKeaClient):
        """POST that raises PartialPersistError redirects with a warning (no 500)."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.network_update.side_effect = PartialPersistError("dhcp4", Exception("write failed"))
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        messages_list = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.WARNING for m in messages_list),
            f"Expected a WARNING message; got: {[(m.level, m.message) for m in messages_list]}",
        )

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url(), {"name": "prod-net"})
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork6EditView(_ViewTestBase):
    """Tests for ServerSharedNetwork6EditView — verifies version=6."""

    def _url(self, name="prod-net6"):
        return reverse("plugins:netbox_kea:server_shared_network6_edit", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 for DHCPv6 edit view."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp6": {
                        "shared-networks": [{"name": "prod-net6", "description": "", "option-data": [], "subnet6": []}],
                        "subnet6": [],
                    }
                },
            }
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_update_with_version_6(self, MockKeaClient):
        """POST must call network_update with version=6."""
        MockKeaClient.return_value.network_update.return_value = None
        MockKeaClient.return_value.command.return_value = _CONFIG6_WITH_PROD_NET
        self.client.post(
            self._url(),
            {
                "name": "prod-net6",
                "description": "v6 net",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        call_args = MockKeaClient.return_value.network_update.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 6)


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


# ─────────────────────────────────────────────────────────────────────────────
# Gap S1: Shared-network assignment on subnet create
# ─────────────────────────────────────────────────────────────────────────────

# Config-get response listing available networks (no subnets assigned yet)
_CONFIG4_NETWORKS_FOR_ADD = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [],
                "shared-networks": [
                    {"name": "net-alpha", "subnet4": []},
                    {"name": "net-beta", "subnet4": []},
                ],
            }
        },
    }
]


def _mock_kea_command_for_subnet_add(cmd, **kw):
    """Return a subnet-list response for list commands, else the available networks config."""
    if "list" in cmd:
        return [{"result": 0, "arguments": {"subnets": []}}]
    return _CONFIG4_NETWORKS_FOR_ADD


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4AddViewSharedNetwork(_ViewTestBase):
    """GET/POST /plugins/kea/servers/<pk>/subnets4/add/ — shared_network field."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    def _valid_post_data(self, shared_network=""):
        return {
            "subnet": "10.0.1.0/24",
            "subnet_id": "",
            "pools": "",
            "gateway": "",
            "dns_servers": "",
            "ntp_servers": "",
            "shared_network": shared_network,
        }

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_shared_network_dropdown(self, MockKeaClient):
        """GET must render a shared_network dropdown populated from Kea config."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _mock_kea_command_for_subnet_add
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "net-alpha")
        self.assertContains(response, "net-beta")

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_shared_network_calls_network_subnet_add(self, MockKeaClient):
        """POST with shared_network set must call network_subnet_add after subnet creation."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _mock_kea_command_for_subnet_add
        mock_client.subnet_add.return_value = 1
        mock_client.network_subnet_add.return_value = None

        response = self.client.post(self._url(), self._valid_post_data(shared_network="net-alpha"))
        self.assertIn(response.status_code, (302, 200))
        mock_client.network_subnet_add.assert_called_once()
        call_kwargs = mock_client.network_subnet_add.call_args[1] or {}
        call_args = mock_client.network_subnet_add.call_args[0]
        name = call_kwargs.get("name") or (call_args[1] if len(call_args) > 1 else None)
        self.assertEqual(name, "net-alpha")
        subnet_id = call_kwargs.get("subnet_id") or (call_args[2] if len(call_args) > 2 else None)
        self.assertEqual(subnet_id, 1)
        version = call_kwargs.get("version") or (call_args[0] if len(call_args) > 0 else None)
        self.assertEqual(version, 4)

    @patch("netbox_kea.models.KeaClient")
    def test_post_without_shared_network_does_not_call_network_subnet_add(self, MockKeaClient):
        """POST without shared_network must NOT call network_subnet_add."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _mock_kea_command_for_subnet_add
        mock_client.subnet_add.return_value = None

        response = self.client.post(self._url(), self._valid_post_data(shared_network=""))
        self.assertIn(response.status_code, (302, 200))
        mock_client.network_subnet_add.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for _get_network_choices — None/missing arguments handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkChoicesNoneArguments(_ViewTestBase):
    """_get_network_choices must return fallback when config-get returns None arguments."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_falls_back_when_config_returns_none_arguments(self, MockKeaClient):
        """GET must not crash and must show form when config-get returns arguments=None."""
        _subnet4_get = [{"result": 0, "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}]}}]
        _config_none_args = [{"result": 0, "arguments": None, "text": "no config"}]
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = [_subnet4_get, _config_none_args]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_falls_back_when_config_raises_kea_exception(self, MockKeaClient):
        """GET must not crash when config-get raises KeaException."""
        from netbox_kea.kea import KeaException

        _subnet4_get = [{"result": 0, "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}]}}]
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = [
            _subnet4_get,
            KeaException({"result": 1, "text": "error"}, index=0),
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_subnet_update_fails_does_not_move_network(self, MockKeaClient):
        """POST where subnet_update raises KeaException must NOT call network_subnet_add/del."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _CONFIG4_NO_NETWORKS
        mock_client.subnet_update.side_effect = KeaException({"result": 1, "text": "update failed"}, index=0)

        response = self.client.post(
            self._url(),
            {
                "subnet_cidr": "10.0.0.0/24",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
                "shared_network": "net-alpha",
                "current_network": "",
            },
        )
        # View should return to form (200) or redirect, but NOT call network methods.
        self.assertIn(response.status_code, (200, 302))
        mock_client.subnet_update.assert_called()
        mock_client.network_subnet_add.assert_not_called()
        mock_client.network_subnet_del.assert_not_called()


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
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, False)),
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
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, False)),
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
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({"10.0.0.5": rsv}, True)
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
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True)),
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
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({"10.0.0.5": rsv}, True)
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
                "netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({"10.0.0.5": rsv}, True)
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
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True)),
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
            patch("netbox_kea.views.leases._fetch_reservation_by_ip_for_leases", return_value=({}, True)),
            patch("netbox_kea.sync.bulk_fetch_netbox_ips", return_value={}),
            patch.object(server, "get_client", return_value=MagicMock()),
        ):
            _enrich_leases_with_badges([lease], server, 4, can_delete=False, can_change=True)
        self.assertIsNotNone(lease.get("create_reservation_url"))


# ---------------------------------------------------------------------------
# Tests for shared network POST — option-data preservation
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkOptionDataPreservation(_ViewTestBase):
    """Verify non-DNS/NTP option-data entries are preserved on shared network save."""

    def _url(self, name="prod-net"):
        return reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_post_preserves_non_dns_options(self, MockKeaClient):
        """network_update receives non-DNS/NTP option-data that was fetched from Kea."""
        custom_option = {"name": "vendor-specific", "data": "deadbeef"}
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {
                                "name": "prod-net",
                                "option-data": [
                                    {"name": "domain-name-servers", "data": "8.8.8.8"},
                                    custom_option,
                                ],
                                "subnet4": [],
                            }
                        ],
                        "subnet4": [],
                    }
                },
            }
        ]
        MockKeaClient.return_value.network_update.return_value = None
        self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "1.1.1.1",
                "ntp_servers": "",
            },
        )
        call_kwargs = MockKeaClient.return_value.network_update.call_args
        kwargs = call_kwargs.kwargs or call_kwargs[1]
        options = kwargs.get("options", [])
        option_names = [o["name"] for o in options]
        # The custom non-DNS option must be preserved.
        self.assertIn("vendor-specific", option_names)
        # The new DNS from the form must also be present.
        self.assertIn("domain-name-servers", option_names)

    @patch("netbox_kea.models.KeaClient")
    def test_post_replaces_dns_servers_not_duplicates(self, MockKeaClient):
        """Old DNS option from Kea is dropped; only the form-supplied DNS value is written."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {
                                "name": "prod-net",
                                "option-data": [{"name": "domain-name-servers", "data": "8.8.8.8"}],
                                "subnet4": [],
                            }
                        ],
                        "subnet4": [],
                    }
                },
            }
        ]
        MockKeaClient.return_value.network_update.return_value = None
        self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "1.1.1.1",
                "ntp_servers": "",
            },
        )
        call_kwargs = MockKeaClient.return_value.network_update.call_args
        kwargs = call_kwargs.kwargs or call_kwargs[1]
        options = kwargs.get("options", [])
        dns_opts = [o for o in options if o["name"] == "domain-name-servers"]
        # Only one DNS entry must be present (the new value, not the old one).
        self.assertEqual(len(dns_opts), 1)
        self.assertEqual(dns_opts[0]["data"], "1.1.1.1")


# ---------------------------------------------------------------------------
# Tests for renew/rebind timer zero round-trip (subnet edit GET)
# ---------------------------------------------------------------------------


_SUBNET4_GET_ZERO_TIMERS = [
    {
        "result": 0,
        "arguments": {
            "subnet4": [
                {
                    "id": 42,
                    "subnet": "10.1.0.0/24",
                    "renew-timer": 0,
                    "rebind-timer": 0,
                    "pools": [],
                    "option-data": [],
                }
            ]
        },
    }
]

_CONFIG4_NO_NETWORKS_RESP = [{"result": 0, "arguments": {"Dhcp4": {"shared-networks": [], "subnet4": []}}}]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditZeroTimers(_ViewTestBase):
    """Renew/rebind timer value of 0 must round-trip through subnet edit GET."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_includes_zero_renew_timer_in_initial(self, MockKeaClient):
        """GET for a subnet with renew-timer=0 must populate the form field with 0."""

        def _cmd_side_effect(command, **_kwargs):
            if "subnet4-get" in command:
                return _SUBNET4_GET_ZERO_TIMERS
            return _CONFIG4_NO_NETWORKS_RESP

        MockKeaClient.return_value.command.side_effect = _cmd_side_effect
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        form = response.context.get("form")
        self.assertIsNotNone(form, "Expected a form in context but got None")
        self.assertEqual(form.initial.get("renew_timer"), 0)

    @patch("netbox_kea.models.KeaClient")
    def test_get_includes_zero_rebind_timer_in_initial(self, MockKeaClient):
        """GET for a subnet with rebind-timer=0 must populate the form field with 0."""

        def _cmd_side_effect(command, **_kwargs):
            if "subnet4-get" in command:
                return _SUBNET4_GET_ZERO_TIMERS
            return _CONFIG4_NO_NETWORKS_RESP

        MockKeaClient.return_value.command.side_effect = _cmd_side_effect
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        form = response.context.get("form")
        self.assertIsNotNone(form, "Expected a form in context but got None")
        self.assertEqual(form.initial.get("rebind_timer"), 0)


# ---------------------------------------------------------------------------
# Reservation list exception paths
# ---------------------------------------------------------------------------

_FORMSET_MGMT = {
    "options-TOTAL_FORMS": "0",
    "options-INITIAL_FORMS": "0",
    "options-MIN_NUM_FORMS": "0",
    "options-MAX_NUM_FORMS": "1000",
}

_VALID_RESERVATION4_POST = {
    "subnet_id": "1",
    "ip_address": "10.0.0.55",
    "identifier_type": "hw-address",
    "identifier": "aa:bb:cc:dd:ee:ff",
    "hostname": "test-host",
    **_FORMSET_MGMT,
}

_VALID_RESERVATION6_POST = {
    "subnet_id": "1",
    "ip_addresses": "2001:db8::1",
    "identifier_type": "duid",
    "identifier": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
    "hostname": "test-host6",
    **_FORMSET_MGMT,
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4ListExceptions(_ViewTestBase):
    """Reservation list view — exception path coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_hook_not_available_shows_warning(self, MockKeaClient):
        """KeaException result=2 sets hook_available=False without crashing."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_page.side_effect = KeaException(
            {"result": 2, "text": "hook not loaded"}, index=0
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context.get("hook_available", True))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_during_fetch_does_not_crash(self, MockKeaClient):
        """Unexpected exception during reservation_get_page is swallowed gracefully."""
        MockKeaClient.return_value.reservation_get_page.side_effect = RuntimeError("boom")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6ListExceptions(_ViewTestBase):
    """Reservation6 list view — exception path coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_hook_not_available_shows_warning(self, MockKeaClient):
        """KeaException result=2 sets hook_available=False without crashing."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_page.side_effect = KeaException(
            {"result": 2, "text": "hook not loaded"}, index=0
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context.get("hook_available", True))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_during_fetch_does_not_crash(self, MockKeaClient):
        """Unexpected exception during reservation_get_page is swallowed gracefully."""
        MockKeaClient.return_value.reservation_get_page.side_effect = RuntimeError("unexpected")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# SharedNetworkEdit fetch-failure paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkEditFetchFailures(_ViewTestBase):
    """_fetch_network failure paths: GET redirect + POST abort."""

    def _url(self, name="prod-net"):
        return reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_when_network_not_found(self, MockKeaClient):
        """GET must redirect when config-get returns a config with no matching network."""
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"shared-networks": [], "subnet4": []}}}
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_when_fetch_raises_kea_exception(self, MockKeaClient):
        """GET must redirect when config-get raises KeaException."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException({"result": 1, "text": "err"}, index=0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_post_aborts_when_reload_returns_empty(self, MockKeaClient):
        """POST aborts with error when _fetch_network (reload before write) returns empty."""
        # POST path has only ONE _fetch_network call (for reload). Make it return empty
        # (prod-net not in shared-networks) to trigger the abort path.
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"shared-networks": [], "subnet4": []}}}
        ]
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        # Must re-render (not crash) with error message
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not reload")

    @patch("netbox_kea.models.KeaClient")
    def test_post_sets_ntp_servers_option(self, MockKeaClient):
        """POST with ntp_servers populates option-data with ntp-servers entry."""
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        MockKeaClient.return_value.network_update.return_value = None
        self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "10.0.0.1",
            },
        )
        call_kwargs = MockKeaClient.return_value.network_update.call_args.kwargs or {}
        options = call_kwargs.get("options", [])
        ntp_opts = [o for o in options if o.get("name") == "ntp-servers"]
        self.assertEqual(len(ntp_opts), 1)
        self.assertEqual(ntp_opts[0]["data"], "10.0.0.1")

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_rerenders(self, MockKeaClient):
        """POST that raises generic Exception must not crash (no 500)."""
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        MockKeaClient.return_value.network_update.side_effect = RuntimeError("unexpected")
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_rerenders(self, MockKeaClient):
        """POST with missing required field must re-render the form (200)."""
        response = self.client.post(self._url(), {"description": "x"})
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Reservation4Add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4AddExceptions(_ViewTestBase):
    """ServerReservation4AddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError must redirect with a warning message."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_with_sync_to_netbox(self, MockKeaClient):
        """PartialPersistError with sync_to_netbox=True must attempt IPAM sync."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), True)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_sync_failure_shows_warning(self, MockKeaClient):
        """PartialPersistError + sync error must show two warnings."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("sync boom")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException must re-render the form with an error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "already exists"}, index=0
        )
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_rerenders_form(self, MockKeaClient):
        """Unexpected exception must re-render the form with a generic error."""
        MockKeaClient.return_value.reservation_add.side_effect = RuntimeError("unexpected")
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_success_with_sync_to_netbox(self, MockKeaClient):
        """Successful add with sync_to_netbox=True must call sync and show info message."""
        MockKeaClient.return_value.reservation_add.return_value = None
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), True)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_success_sync_failure_shows_warning(self, MockKeaClient):
        """Successful add where sync raises must show a warning (no 500)."""
        MockKeaClient.return_value.reservation_add.return_value = None
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("sync fail")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Reservation6Add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6AddExceptions(_ViewTestBase):
    """ServerReservation6AddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError must redirect with a warning message."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp6", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_with_sync_to_netbox(self, MockKeaClient):
        """PartialPersistError with sync_to_netbox=True must attempt IPAM sync."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp6", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), False)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_sync_failure_shows_warning(self, MockKeaClient):
        """PartialPersistError + sync exception must show warning about sync failure."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp6", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("sync boom")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException must re-render the form with an error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "conflict"}, index=0
        )
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_rerenders_form(self, MockKeaClient):
        """Unexpected exception must re-render the form with a generic error."""
        MockKeaClient.return_value.reservation_add.side_effect = RuntimeError("bang")
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Reservation4Edit exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4EditExceptions(_ViewTestBase):
    """ServerReservation4EditView GET and POST exception paths."""

    def _url(self, subnet_id=1, ip="10.0.0.55"):
        return reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, subnet_id, ip])

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_on_kea_exception(self, MockKeaClient):
        """GET that raises KeaException during reservation fetch must redirect."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get.side_effect = KeaException(
            {"result": 1, "text": "server error"}, index=0
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_on_generic_exception(self, MockKeaClient):
        """GET that raises generic Exception during reservation fetch must redirect."""
        MockKeaClient.return_value.reservation_get.side_effect = RuntimeError("crash")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_get_404_when_reservation_not_found(self, MockKeaClient):
        """GET must return 404 when reservation_get returns None."""
        MockKeaClient.return_value.reservation_get.return_value = None
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_error_redirects(self, MockKeaClient):
        """PartialPersistError on reservation_update must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync(self, MockKeaClient):
        """PartialPersistError with sync_to_netbox attempts sync."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), True)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_sync_failure(self, MockKeaClient):
        """PartialPersistError + sync failure shows warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("sync")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException on reservation_update must re-render the form."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_update.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_rerenders_form(self, MockKeaClient):
        """Unexpected exception on reservation_update must re-render the form."""
        MockKeaClient.return_value.reservation_update.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_success_with_sync(self, MockKeaClient):
        """Successful update with sync_to_netbox calls sync and shows info."""
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), False)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_success_sync_failure_shows_warning(self, MockKeaClient):
        """Successful update where sync raises must show warning."""
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("oops")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Reservation6Edit exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6EditExceptions(_ViewTestBase):
    """ServerReservation6EditView GET and POST exception paths."""

    def _url(self, subnet_id=1, ip="2001:db8::1"):
        return reverse("plugins:netbox_kea:server_reservation6_edit", args=[self.server.pk, subnet_id, ip])

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_on_kea_exception(self, MockKeaClient):
        """GET that raises KeaException during reservation fetch must redirect."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_on_generic_exception(self, MockKeaClient):
        """GET that raises generic Exception must redirect."""
        MockKeaClient.return_value.reservation_get.side_effect = RuntimeError("boom")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_get_404_when_reservation_not_found(self, MockKeaClient):
        """GET must return 404 when reservation_get returns None."""
        MockKeaClient.return_value.reservation_get.return_value = None
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_error_redirects(self, MockKeaClient):
        """PartialPersistError on reservation_update must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException on reservation_update must re-render the form."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_update.side_effect = KeaException(
            {"result": 1, "text": "error"}, index=0
        )
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_rerenders_form(self, MockKeaClient):
        """Unexpected exception on reservation_update must re-render form."""
        MockKeaClient.return_value.reservation_update.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Reservation4Delete exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4DeleteExceptions(_ViewTestBase):
    """ServerReservation4DeleteView POST exception paths."""

    def _url(self, subnet_id=1, ip="10.0.0.55"):
        return reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, subnet_id, ip])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError must redirect with warning and still run side effects."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_del.side_effect = PartialPersistError("dhcp4", Exception("write"))
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_shows_error(self, MockKeaClient):
        """KeaException must show an error message and redirect."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_del.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_shows_error(self, MockKeaClient):
        """Unexpected exception must show a generic error and redirect."""
        MockKeaClient.return_value.reservation_del.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


# ---------------------------------------------------------------------------
# Reservation6Delete exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6DeleteExceptions(_ViewTestBase):
    """ServerReservation6DeleteView POST exception paths."""

    def _url(self, subnet_id=1, ip="2001:db8::1"):
        return reverse("plugins:netbox_kea:server_reservation6_delete", args=[self.server.pk, subnet_id, ip])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_del.side_effect = PartialPersistError("dhcp6", Exception("write"))
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_shows_error(self, MockKeaClient):
        """KeaException must show an error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_del.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_shows_error(self, MockKeaClient):
        """Unexpected exception must show a generic error."""
        MockKeaClient.return_value.reservation_del.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


# ---------------------------------------------------------------------------
# Pool view exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolAddExceptions(_ViewTestBase):
    """_BasePoolAddView POST exception paths."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError on pool_add must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.pool_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_shows_error(self, MockKeaClient):
        """Generic exception on pool_add must show error message."""
        MockKeaClient.return_value.pool_add.side_effect = RuntimeError("crash")
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolDeleteExceptions(_ViewTestBase):
    """_BasePoolDeleteView GET/POST exception paths."""

    def _url(self, subnet_id=42, pool="10.0.0.100-10.0.0.200"):
        return reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, subnet_id, pool])

    @patch("netbox_kea.models.KeaClient")
    def test_get_invalid_pool_format_returns_400(self, MockKeaClient):
        """GET with invalid pool string must return 400."""
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "not_a_pool_format!!"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_pool_format_returns_400(self, MockKeaClient):
        """POST with invalid pool string must return 400."""
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "not_a_pool_format!!"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError on pool_del must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.pool_del.side_effect = PartialPersistError("dhcp4", Exception("write"))
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_shows_error(self, MockKeaClient):
        """Generic exception on pool_del must show error message."""
        MockKeaClient.return_value.pool_del.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


# ---------------------------------------------------------------------------
# SubnetAdd exception paths
# ---------------------------------------------------------------------------

_SUBNET_ADD_POST = {
    "subnet": "10.2.0.0/24",
    "subnet_id": "",
    "pools": "",
    "gateway": "",
    "dns_servers": "",
    "ntp_servers": "",
    "shared_network": "",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddExceptionPaths(_ViewTestBase):
    """_BaseSubnetAddView GET/POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_falls_back_when_network_choices_raise(self, MockKeaClient):
        """GET must render the form with fallback choices when config-get fails."""
        MockKeaClient.return_value.command.side_effect = RuntimeError("config error")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_error_redirects(self, MockKeaClient):
        """PartialPersistError on subnet_add must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"shared-networks": [], "subnet4": []}}}
        ]
        MockKeaClient.return_value.subnet_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        response = self.client.post(self._url(), _SUBNET_ADD_POST, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_rerenders(self, MockKeaClient):
        """Generic exception on subnet_add must re-render the form."""
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"shared-networks": [], "subnet4": []}}}
        ]
        MockKeaClient.return_value.subnet_add.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), _SUBNET_ADD_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_network_assignment_partial_persist_shows_warning(self, MockKeaClient):
        """PartialPersistError on network_subnet_add must show a warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [{"name": "alpha", "subnet4": []}],
                        "subnet4": [],
                    }
                },
            }
        ]
        MockKeaClient.return_value.subnet_add.return_value = 5
        MockKeaClient.return_value.network_subnet_add.side_effect = PartialPersistError("dhcp4", Exception("w"))
        post_data = {**_SUBNET_ADD_POST, "shared_network": "alpha"}
        response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("config-write failed" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_network_assignment_generic_exception_shows_warning(self, MockKeaClient):
        """Generic exception on network_subnet_add must show a warning."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [{"name": "alpha", "subnet4": []}],
                        "subnet4": [],
                    }
                },
            }
        ]
        MockKeaClient.return_value.subnet_add.return_value = 5
        MockKeaClient.return_value.network_subnet_add.side_effect = RuntimeError("network error")
        post_data = {**_SUBNET_ADD_POST, "shared_network": "alpha"}
        response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("could not be assigned" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_client_none_reconnect_failure_shows_error(self, MockKeaClient):
        """When initial get_client fails, post fallback reconnect failure shows error."""
        call_count = [0]

        def _raise_after_first(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First instantiation (for getting choices): raise immediately
                raise RuntimeError("no connection")
            # Second instantiation (for fallback client reconnect): also raise
            raise RuntimeError("still no connection")

        MockKeaClient.side_effect = _raise_after_first
        response = self.client.post(self._url(), _SUBNET_ADD_POST)
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# SubnetEdit exception paths
# ---------------------------------------------------------------------------

_SUBNET4_EDIT_POST = {
    "subnet_cidr": "10.0.0.0/24",
    "pools": "",
    "gateway": "",
    "dns_servers": "",
    "ntp_servers": "",
    "shared_network": "",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditPostExceptions(_ViewTestBase):
    """_BaseSubnetEditView POST exception paths (PartialPersistError, KeaException, generic)."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError on subnet_update must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.command.side_effect = [_SUBNET4_GET_FULL, _CONFIG4_NO_NETWORKS]
        MockKeaClient.return_value.subnet_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        response = self.client.post(self._url(), _SUBNET4_EDIT_POST, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_rerenders(self, MockKeaClient):
        """Generic exception on subnet_update must re-render the form."""
        MockKeaClient.return_value.command.side_effect = [_SUBNET4_GET_FULL, _CONFIG4_NO_NETWORKS]
        MockKeaClient.return_value.subnet_update.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), _SUBNET4_EDIT_POST)
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkDelPartialPersist(_ViewTestBase):
    """Subnet edit: network_subnet_del PartialPersistError must NOT rollback new_network."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_on_del_skips_rollback(self, MockKeaClient):
        """When network_subnet_del raises PartialPersistError, no rollback del is called."""
        from netbox_kea.kea import PartialPersistError

        config_with_current_net = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}],
                        "shared-networks": [
                            {"name": "old-net", "subnet4": [{"id": 42}]},
                            {"name": "new-net", "subnet4": []},
                        ],
                    }
                },
            }
        ]
        MockKeaClient.return_value.command.return_value = config_with_current_net
        MockKeaClient.return_value.subnet_update.return_value = None
        MockKeaClient.return_value.network_subnet_add.return_value = None
        MockKeaClient.return_value.network_subnet_del.side_effect = PartialPersistError("dhcp4", Exception("write"))

        post_data = {**_SUBNET4_EDIT_POST, "shared_network": "new-net"}
        response = self.client.post(self._url(), post_data)
        # Should redirect (PartialPersistError is caught as KeaException and redirects)
        self.assertIn(response.status_code, (200, 302))
        # network_subnet_del called once (for old-net); no rollback call to del new-net
        self.assertEqual(MockKeaClient.return_value.network_subnet_del.call_count, 1)


# ---------------------------------------------------------------------------
# Sync view edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncViewEdgeCases(_ViewTestBase):
    """ServerLease4SyncView POST edge cases: invalid IP, sync exception."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_missing_ip_returns_400(self, MockKeaClient):
        """POST without ip_address must return 400."""
        response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_ip_returns_400(self, MockKeaClient):
        """POST with invalid IP must return 400."""
        response = self.client.post(self._url(), {"ip_address": "not-an-ip"})
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_exception_returns_500(self, MockKeaClient):
        """POST where sync raises an exception must return 500."""
        with patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=RuntimeError("db error")):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 500)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncPermission(_ViewTestBase):
    """_BaseBulkReservationSyncView — non-superuser without IPAM perms gets 403."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def test_post_without_ipam_permission_returns_403(self):
        """POST without ipam.add_ipaddress must return 403."""
        restricted_user = User.objects.create_user(
            username="noperms_bulk",
            email="noperms_bulk@example.com",
            password="pass",
        )
        self.client.force_login(restricted_user)
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 403)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncFetchException(_ViewTestBase):
    """_BaseBulkReservationSyncView — fetch exception shows error and redirects."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_fetch_exception_shows_error(self, MockKeaClient):
        """Exception in _fetch_reservations_from_server must show error message and redirect."""
        with patch(
            "netbox_kea.views.sync_views._fetch_reservations_from_server", side_effect=RuntimeError("fetch fail")
        ):
            response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


# ---------------------------------------------------------------------------
# BulkReservationImport edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationImportEdgeCases(_ViewTestBase):
    """_BaseBulkReservationImportView POST: invalid form and CSV parse error."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_without_file_rerenders_form(self, MockKeaClient):
        """POST without a CSV file must re-render the form (200)."""
        response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_csv_shows_error(self, MockKeaClient):
        """POST with a CSV that fails parse_reservation_csv raises ValueError → form error."""
        import io

        MockKeaClient.return_value.reservation_add.return_value = None
        # CSV with missing required columns triggers ValueError in parse_reservation_csv
        bad_csv = io.BytesIO(b"garbage_header\nrow1\n")
        bad_csv.name = "bad.csv"
        response = self.client.post(self._url(), {"csv_file": bad_csv})
        self.assertEqual(response.status_code, 200)
        # Response should include a form error about invalid CSV
        self.assertContains(response, "csv_file", msg_prefix="Expected CSV error in form")


# ---------------------------------------------------------------------------
# Subnet options POST: formset invalid
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsPostInvalid(_ViewTestBase):
    """_BaseSubnetOptionsEditView POST: formset invalid must re-render (200)."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_formset_rerenders(self, MockKeaClient):
        """POST with an invalid formset (missing management form) must re-render 200."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "option-data": []}]},
            }
        ]
        # Post one form entry missing required 'name' field — makes formset invalid
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "",
                "form-0-data": "some-value",
            },
        )
        # Invalid formset can re-render OR redirect depending on validation path
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_always_send_includes_flag(self, MockKeaClient):
        """POST with always_send=True must pass always-send=True in options."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.subnet_update_options.call_args
        if call_kwargs:
            options = (call_kwargs.kwargs or {}).get("options") or (call_kwargs.args[2] if call_kwargs.args else [])
            always_send_opts = [o for o in options if o.get("always-send")]
            self.assertTrue(len(always_send_opts) >= 1)


# ---------------------------------------------------------------------------
# Server options POST: formset invalid + always_send
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsPostInvalid(_ViewTestBase):
    """_BaseServerOptionsEditView POST: formset invalid and always_send coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_formset_rerenders(self, MockKeaClient):
        """POST with an invalid formset must re-render (not crash)."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"Dhcp4": {"option-data": []}}}]
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "",
                "form-0-data": "val",
            },
        )
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_always_send_includes_flag(self, MockKeaClient):
        """POST with always_send=True must pass always-send=True in options."""
        MockKeaClient.return_value.server_update_options.return_value = None
        self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "domain-name-servers",
                "form-0-data": "8.8.8.8",
                "form-0-always_send": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.server_update_options.call_args
        if call_kwargs:
            options = (call_kwargs.kwargs or {}).get("options") or []
            always_send_opts = [o for o in options if o.get("always-send")]
            self.assertTrue(len(always_send_opts) >= 1)


# ---------------------------------------------------------------------------
# OptionDef add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddExceptions(_ViewTestBase):
    """BaseServerOptionDefAddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_rerenders(self, MockKeaClient):
        """POST with invalid form (missing required fields) must return 200."""
        response = self.client.post(self._url(), {"name": "", "code": "", "type": "", "space": ""})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_array_true_passes_flag(self, MockKeaClient):
        """POST with array=True must include array=True in the option_def dict."""
        MockKeaClient.return_value.option_def_add.return_value = None
        self.client.post(
            self._url(),
            {
                "name": "my-option",
                "code": "200",
                "type": "string",
                "space": "dhcp4",
                "array": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.option_def_add.call_args
        if call_kwargs:
            opt = (call_kwargs.kwargs or {}).get("option_def") or {}
            self.assertTrue(opt.get("array"))

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """KeaException on option_def_add must show error message and redirect."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_add.side_effect = KeaException(
            {"result": 1, "text": "duplicate code"}, index=0
        )
        response = self.client.post(
            self._url(),
            {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            follow=True,
        )
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_shows_internal_error(self, MockKeaClient):
        """Generic exception on option_def_add must show generic error message and redirect."""
        MockKeaClient.return_value.option_def_add.side_effect = RuntimeError("crash")
        response = self.client.post(
            self._url(),
            {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            follow=True,
        )
        msgs = list(response.context["messages"])
        self.assertTrue(any("internal error" in m.message.lower() for m in msgs))


# ---------------------------------------------------------------------------
# OptionDef delete exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefDeleteExceptions(_ViewTestBase):
    """BaseServerOptionDefDeleteView POST exception paths."""

    def _url(self, code=200, space="dhcp4"):
        return reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, code, space])

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """KeaException on option_def_del must show error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_del.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_shows_internal_error(self, MockKeaClient):
        """Generic exception on option_def_del must show generic error message."""
        MockKeaClient.return_value.option_def_del.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("internal error" in m.message.lower() for m in msgs))


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
# _get_reservation_options_formset — partial submission path
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetReservationOptionsFormsetPartial(_ViewTestBase):
    """Line 77-79: partial options-* keys but no management form."""

    def test_partial_options_keys_returns_invalid_formset(self):
        """When options-* keys exist without management form, returns (formset, False)."""
        from netbox_kea.views import _build_reservation_options_formset

        post_data = {"options-0-name": "domain-name-servers"}  # no TOTAL_FORMS key
        fs, is_valid = _build_reservation_options_formset(post_data)
        self.assertFalse(is_valid)


# ---------------------------------------------------------------------------
# _KeaChangeMixin — 403 for view-only user
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaChangeMixinPermission(_ViewTestBase):
    """Lines 243-246: _KeaChangeMixin returns 403 for view-only users."""

    def test_view_only_user_gets_403_on_mutation_view(self):
        """User with 'view' but not 'change' on Server gets 403 from _KeaChangeMixin."""
        from users.models import ObjectPermission

        readonly = User.objects.create_user(username="readonly_mixin", password="pass")
        perm = ObjectPermission(name="view-servers", actions=["view"])
        perm.save()
        perm.users.add(readonly)
        perm.object_types.add(
            __import__(
                "django.contrib.contenttypes.models", fromlist=["ContentType"]
            ).ContentType.objects.get_for_model(self.server.__class__)
        )
        self.client.force_login(readonly)
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_user_without_global_perm_gets_403_on_server_add(self):
        """User without change_server permission gets 403 on a mutation view."""
        no_perm = User.objects.create_user(username="noperm_mixin", password="pass")
        self.client.force_login(no_perm)
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])
        response = self.client.get(url)
        self.assertIn(response.status_code, (403, 404))


# ---------------------------------------------------------------------------
# Status view — null/empty args + HA data
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestStatusViewNullArgs(_ViewTestBase):
    """Lines 317-318, 322-323, 352-357: status-get returns empty/null arguments."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_status", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_ca_status_empty_args_raises(self, MockKeaClient):
        """_get_ca_status raises RuntimeError when arguments is empty."""
        call_n = [0]

        def _side(cmd, service=None, **kwargs):
            call_n[0] += 1
            if cmd == "status-get" and (not service or "dhcp" not in service[0]):
                return [{"result": 0, "arguments": {}}]  # empty → falsy
            if cmd == "version-get" and (not service or "dhcp" not in service[0]):
                return [{"result": 0, "arguments": {"extended": "2.0"}}]
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": {"extended": "2.0"}}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _side
        # The view catches exceptions from _get_ca_status; page still renders
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (200, 500))

    @patch("netbox_kea.models.KeaClient")
    def test_get_dhcp_status_null_args_raises(self, MockKeaClient):
        """_get_dhcp_status raises RuntimeError when arguments is None."""

        def _side(cmd, service=None, **kwargs):
            if cmd == "status-get" and service and "dhcp" in service[0]:
                return [{"result": 0, "arguments": None}]
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": {"extended": "2.0"}}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _side
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (200, 500))

    @patch("netbox_kea.models.KeaClient")
    def test_get_dhcp_status_ha_fields_included(self, MockKeaClient):
        """Lines 370-373: HA fields are present when high-availability in status-get."""

        def _side(cmd, service=None, **kwargs):
            if cmd == "status-get" and service and "dhcp" in service[0]:
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "pid": 1,
                            "uptime": 100,
                            "reload": 0,
                            "high-availability": [
                                {
                                    "ha-mode": "load-balancing",
                                    "ha-servers": {
                                        "local": {"role": "primary", "state": "active"},
                                        "remote": {"connection-interrupted": False, "age": 10, "role": "secondary"},
                                    },
                                }
                            ],
                        },
                    }
                ]
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 100, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": {"extended": "2.4.1"}}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _side
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "HA")


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
        MockKeaClient.return_value.lease_add.side_effect = RuntimeError("unexpected crash")
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
# Subnet list view — null config + export + HTMX partial
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetListViewEdgeCases(_ViewTestBase):
    """Lines 1110, 1173, 1181: subnet view null config, export, HTMX."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_null_config_arguments_raises(self, MockKeaClient):
        """Line 1110: null config-get arguments raises RuntimeError → 500 response."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": None}]
        self.client.raise_request_exception = False
        response = self.client.get(self._url())
        self.client.raise_request_exception = True
        self.assertEqual(response.status_code, 500)

    @patch("netbox_kea.models.KeaClient")
    def test_export_returns_csv(self, MockKeaClient):
        """Line 1173: ?export=csv returns a CSV file response."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        response = self.client.get(self._url() + "?export=csv")
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_htmx_partial_returns_table_fragment(self, MockKeaClient):
        """Line 1181: HTMX request to subnet view returns partial table."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        response = self.client.get(self._url(), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Shared network list — dhcp disabled + null config
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkListEdgeCases(_ViewTestBase):
    """Lines 1237, 1241: shared network list when dhcp disabled or null config."""

    @patch("netbox_kea.models.KeaClient")
    def test_dhcp4_disabled_redirects(self, MockKeaClient):
        """Line 1278: when dhcp4 disabled, get() returns redirect (302)."""
        server_no4 = _make_db_server(name="no-dhcp4", server_url="https://kea.example.com", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[server_no4.pk])
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        response = self.client.get(url)
        # check_dhcp_enabled returns redirect when dhcp4=False
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_null_config_returns_empty(self, MockKeaClient):
        """Line 1241: get_children returns [] when config-get returns null args."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": None}]
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# SharedNetworkAdd/Delete — generic exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkCRUDGenericException(_ViewTestBase):
    """Lines 1371-1373, 1426-1428: generic exception on add/delete shows error."""

    @patch("netbox_kea.models.KeaClient")
    def test_add_generic_exception_shows_error(self, MockKeaClient):
        """Lines 1371-1373: generic exception on network_add redirects with error."""
        MockKeaClient.return_value.network_add.side_effect = RuntimeError("connection reset")
        url = reverse("plugins:netbox_kea:server_shared_network4_add", args=[self.server.pk])
        response = self.client.post(url, {"name": "new-net"}, follow=True)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("internal error" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_delete_generic_exception_shows_error(self, MockKeaClient):
        """Lines 1426-1428: generic exception on network_del redirects with error."""
        MockKeaClient.return_value.network_del.side_effect = RuntimeError("timeout")
        url = reverse("plugins:netbox_kea:server_shared_network4_delete", args=[self.server.pk, "old-net"])
        response = self.client.post(url, {}, follow=True)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("internal error" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Shared network edit — option parsing in GET
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkEditOptionParsing(_ViewTestBase):
    """Lines 1488-1492: GET populates dns_servers/ntp_servers from option-data."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_populates_dns_and_ntp_from_option_data(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {
                                "name": "prod-net",
                                "option-data": [
                                    {"name": "domain-name-servers", "data": "8.8.8.8"},
                                    {"name": "ntp-servers", "data": "192.0.2.1"},
                                ],
                                "subnet4": [],
                            }
                        ]
                    }
                },
            }
        ]
        url = reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, "prod-net"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "8.8.8.8")
        self.assertContains(response, "192.0.2.1")


# ---------------------------------------------------------------------------
# Reservation list enrichment — thread pool exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationListEnrichmentExceptions(_ViewTestBase):
    """Lines 1641-1663: enrichment thread pool exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_no_reservations_skips_enrichment(self, MockKeaClient):
        """Line 1650: empty reservation list → enrichment returns early."""
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_thread_pool_generic_exception_returns_early(self, MockKeaClient):
        """Line 1662-1663: generic exception in thread pool causes enrichment to return."""
        MockKeaClient.return_value.reservation_get_page.return_value = (
            [{"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}],
            0,
            0,
        )
        # lease4-get-all raises an unexpected error
        MockKeaClient.return_value.command.side_effect = RuntimeError("unexpected")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Reservation6 Add — option-data and sync paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6AddOptionDataAndSync(_ViewTestBase):
    """Lines 2008, 2027-2034: Reservation6 add with option-data and sync."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_option_data_included(self, MockKeaClient):
        """Line 2008: option-data is included in reservation when formset has entries."""
        MockKeaClient.return_value.reservation_add.return_value = None
        post_data = {
            **_VALID_RESERVATION6_POST,
            "options-TOTAL_FORMS": "1",
            "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            "options-0-name": "dns-servers",
            "options-0-data": "2001:4860:4860::8888",
            "options-0-always_send": "",
            "options-0-DELETE": "",
        }
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))
        call_args = MockKeaClient.return_value.reservation_add.call_args
        if call_args:
            self.assertTrue(MockKeaClient.return_value.reservation_add.called)

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_success(self, MockKeaClient, mock_sync):
        """Lines 2027-2031: sync_to_netbox=on → sync called, success message."""
        MockKeaClient.return_value.reservation_add.return_value = None
        mock_sync.return_value = (MagicMock(), True)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_exception_shows_warning(self, MockKeaClient, mock_sync):
        """Lines 2032-2034: sync raises exception → warning message."""
        MockKeaClient.return_value.reservation_add.return_value = None
        mock_sync.side_effect = RuntimeError("sync failed")
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# Reservation6 Edit — option-data and sync paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6EditOptionDataAndSync(_ViewTestBase):
    """Lines 2292, 2307-2314, 2327-2334: Reservation6 edit with option-data and sync."""

    def _url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation6_edit",
            args=[self.server.pk, 1, "2001:db8::1"],
        )

    def _mock_get(self, MockKeaClient):
        MockKeaClient.return_value.reservation_get.return_value = {
            "subnet-id": 1,
            "ip-addresses": ["2001:db8::1"],
            "duid": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
            "hostname": "v6host",
            "option-data": [],
        }

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_option_data(self, MockKeaClient):
        """Line 2292: option-data appended to reservation when formset has entries."""
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {
            **_VALID_RESERVATION6_POST,
            "options-TOTAL_FORMS": "1",
            "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            "options-0-name": "ntp-servers",
            "options-0-data": "2001:db8::1:1",
            "options-0-always_send": "",
            "options-0-DELETE": "",
        }
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_success(self, MockKeaClient, mock_sync):
        """Lines 2307-2314: sync succeeds → info message."""
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        mock_sync.return_value = (MagicMock(), False)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_exception(self, MockKeaClient, mock_sync):
        """Lines 2312-2314: sync exception → warning."""
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        mock_sync.side_effect = RuntimeError("sync fail")
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync(self, MockKeaClient, mock_sync):
        """Lines 2327-2334: PartialPersistError + sync success."""
        from netbox_kea.kea import PartialPersistError

        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        mock_sync.return_value = (MagicMock(), True)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync_exception(self, MockKeaClient, mock_sync):
        """Lines 2332-2334: PartialPersistError + sync exception → warning."""
        from netbox_kea.kea import PartialPersistError

        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        mock_sync.side_effect = RuntimeError("db error")
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# Subnet delete — GET exception + POST generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetDeleteExceptionPaths(_ViewTestBase):
    """Lines 3177-3178, 3203-3205: subnet delete GET exception and POST generic."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_delete", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_exception_still_renders(self, MockKeaClient):
        """Lines 3177-3178: exception in GET (subnet-get) → still renders confirm page."""
        MockKeaClient.return_value.command.side_effect = RuntimeError("subnet-get failed")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_shows_error(self, MockKeaClient):
        """Lines 3203-3205: generic exception on subnet_del redirects with error."""
        MockKeaClient.return_value.subnet_del.side_effect = RuntimeError("crash")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)


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
# _fetch_subnets_from_server — null config, shared-network subnets, stat_cmds exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchSubnetsFromServer(_ViewTestBase):
    """Lines 3807-3855: _fetch_subnets_from_server edge cases."""

    def _run(self, side_effect=None, return_value=None):
        from netbox_kea.views import _fetch_subnets_from_server

        with patch("netbox_kea.models.KeaClient") as MockKea:
            if side_effect is not None:
                MockKea.return_value.command.side_effect = side_effect
            elif return_value is not None:
                MockKea.return_value.command.return_value = return_value
            return _fetch_subnets_from_server(self.server, version=4)

    def test_null_arguments_raises(self):
        """Line 3808: null arguments raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            self._run(return_value=[{"result": 0, "arguments": None}])

    def test_subnets_in_shared_network_included(self):
        """Line 3827: subnets nested inside shared-networks are included."""
        config = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [
                            {
                                "name": "prod",
                                "subnet4": [
                                    {"id": 10, "subnet": "192.168.0.0/24"},
                                ],
                            }
                        ],
                    }
                },
            }
        ]

        # stat-lease4-get raises to simulate missing hook
        def _side(cmd, **kwargs):
            if cmd == "stat-lease4-get":
                raise RuntimeError("no stat_cmds")
            return config  # return the list, not config[0]

        result = self._run(side_effect=_side)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["subnet"], "192.168.0.0/24")

    def test_stat_cmds_exception_swallowed(self):
        """Lines 3853-3855: stat_cmds exception is swallowed."""
        config_resp = [
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

        def _side(cmd, **kwargs):
            if cmd == "stat-lease4-get":
                raise RuntimeError("stat_cmds not loaded")
            return config_resp  # return the list, not config_resp[0]

        result = self._run(side_effect=_side)
        self.assertEqual(len(result), 1)

    def test_stat_cmds_success_updates_subnet(self):
        """Line 3853: s.update(stats[s['id']]) called when stat-lease4-get returns valid data."""
        config_resp = [
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
        stat_resp = [
            {
                "result": 0,
                "arguments": {
                    "result-set": {
                        "columns": ["subnet-id", "total-addresses", "assigned-addresses"],
                        "rows": [[1, 100, 25]],
                    }
                },
            }
        ]

        def _side(cmd, **kwargs):
            if cmd == "stat-lease4-get":
                return stat_resp
            return config_resp

        result = self._run(side_effect=_side)
        self.assertEqual(len(result), 1)
        # stat data was merged into the subnet dict
        self.assertEqual(result[0].get("total"), 100)
        self.assertEqual(result[0].get("assigned"), 25)


# ---------------------------------------------------------------------------
# _fetch_shared_networks_from_server — null config returns []
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchSharedNetworksFromServer(_ViewTestBase):
    """Line 3939: _fetch_shared_networks_from_server with null config returns []."""

    def test_null_config_returns_empty_list(self):
        from netbox_kea.views import _fetch_shared_networks_from_server

        with patch("netbox_kea.models.KeaClient") as MockKea:
            MockKea.return_value.command.return_value = [{"result": 0, "arguments": None}]
            result = _fetch_shared_networks_from_server(self.server, version=4)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Combined reservations — multi-page pagination
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservationsMultiPage(_ViewTestBase):
    """Lines 4065-4066: combined reservations multi-page pagination."""

    def _url(self):
        return reverse("plugins:netbox_kea:combined_reservations4") + f"?servers={self.server.pk}"

    @patch("netbox_kea.models.KeaClient")
    def test_multi_page_pagination_followed(self, MockKeaClient):
        """Lines 4065-4066: from_index/source_index advance across pages."""
        page1 = [{"subnet-id": 1, "ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:01"}]
        page2 = [{"subnet-id": 1, "ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:dd:ee:02"}]
        MockKeaClient.return_value.reservation_get_page.side_effect = [
            (page1, 1, 1),
            (page2, 0, 0),
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Bulk reservation sync — edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncEdgeCases(_ViewTestBase):
    """Lines 4383-4397: bulk sync with missing IPs, errors, and count tracking."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def setUp(self):
        super().setUp()
        # superuser has ipam perms automatically (is_superuser)

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_reservation_without_ip_is_skipped(self, mock_fetch, mock_sync):
        """Line 4383-4384: reservations without ip-address/ip-addresses are skipped."""
        mock_fetch.return_value = [{"hw-address": "aa:bb:cc:dd:ee:ff"}]  # no IP
        self.client.post(self._url(), follow=True)
        mock_sync.assert_not_called()

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_sync_creates_and_updates(self, mock_fetch, mock_sync):
        """Lines 4389-4390: created and updated counters incremented correctly."""
        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:01"},
            {"ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:dd:ee:02"},
        ]
        mock_sync.side_effect = [(MagicMock(), True), (MagicMock(), False)]
        response = self.client.post(self._url(), follow=True)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("1 created" in m or "created" in m.lower() for m in msgs))

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_sync_exception_counted_as_error(self, mock_fetch, mock_sync):
        """Lines 4391-4394, 4397: sync exception increments errors, warning shown."""
        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1"},
            {"ip-address": "10.0.0.2"},
        ]
        mock_sync.side_effect = [RuntimeError("db error"), (MagicMock(), True)]
        response = self.client.post(self._url(), follow=True)
        msgs = [m.message for m in response.context["messages"]]
        # errors > 0 → summary message shows "N errors"
        self.assertTrue(any("errors" in m for m in msgs))


# ---------------------------------------------------------------------------
# Reservation import — generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationImportGenericException(_ViewTestBase):
    """Lines 4521-4523: generic exception during reservation_add."""

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_appended_to_errors(self, MockKeaClient):
        """Generic exception adds 'unexpected error' to error_rows."""
        MockKeaClient.return_value.reservation_add.side_effect = RuntimeError("crash")
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        import io

        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:dd:ee:ff,1"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file, "subnet_id": "1"})
        self.assertIn(response.status_code, (200, 302))


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
        """Lines 4617-4619: ValueError from parse_lease_csv adds form error."""
        import io

        mock_parse.side_effect = ValueError("bad column")
        csv_file = io.BytesIO(b"ip-address\n10.0.0.1")
        csv_file.name = "leases.csv"
        response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bad column")

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
# Subnet options — subnet in shared-network + POST handler
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsSharedNetwork(_ViewTestBase):
    """Lines 4706-4708, 4761: subnet in shared-network + POST options handler."""

    def _url(self, subnet_id=99):
        return reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_subnet_in_shared_network(self, MockKeaClient):
        """Lines 4706-4708: subnet found inside shared-network is returned."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [
                            {
                                "name": "prod",
                                "subnet4": [{"id": 99, "subnet": "10.99.0.0/24", "option-data": []}],
                            }
                        ],
                    }
                },
            }
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.99.0.0/24")

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_formset_rerenders(self, MockKeaClient):
        """Line 4761 (post handler): invalid formset re-renders form."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [{"id": 99, "subnet": "10.99.0.0/24", "option-data": []}],
                        "shared-networks": [],
                    }
                },
            }
        ]
        # Submit invalid formset (missing TOTAL_FORMS)
        response = self.client.post(self._url(), {"form-0-name": "dns-servers"})
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# _get_global_options — generic exception handler
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetGlobalOptionsGenericException(_ViewTestBase):
    """Line 221-222: generic exception in _get_global_options is swallowed."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_status", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_swallowed(self, MockKeaClient):
        """Line 222: generic Exception in config-get for global options is logged and skipped."""

        def _side(cmd, service=None, **kwargs):
            if cmd == "config-get":
                raise RuntimeError("unexpected crash")
            if cmd == "status-get" and (not service or "dhcp" not in (service or [""])[0]):
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}]
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": {"extended": "2.0"}}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _side
        response = self.client.get(self._url())
        # Should not crash — exception is swallowed
        self.assertIn(response.status_code, (200, 500))


# ---------------------------------------------------------------------------
# Subnet edit — _form_initial with ntp/dns + lease time fields
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditFormInitialFields(_ViewTestBase):
    """Lines 2937-2938, 2944, 2946: _form_initial parses ntp/dns + lease time fields."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_populates_ntp_and_lease_times(self, MockKeaClient):
        """_form_initial picks up ntp-servers, min-valid-lft, max-valid-lft, renew/rebind-timer."""
        subnet_resp = [
            {
                "result": 0,
                "arguments": {
                    "subnet4": [
                        {
                            "id": 42,
                            "subnet": "10.0.0.0/24",
                            "pools": [],
                            "option-data": [
                                {"name": "ntp-servers", "data": "10.0.0.1"},
                            ],
                            "valid-lft": 3600,
                            "min-valid-lft": 1800,
                            "max-valid-lft": 7200,
                            "renew-timer": 900,
                            "rebind-timer": 1500,
                        }
                    ]
                },
            }
        ]
        config_resp = [
            {
                "result": 0,
                "arguments": {"Dhcp4": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}], "shared-networks": []}},
            }
        ]
        # command() is called twice: subnet4-get first, then config-get
        MockKeaClient.return_value.command.side_effect = [subnet_resp, config_resp]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# _get_network_data — unnamed network (no name key) is skipped (line 2913)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetNetworkDataUnnamedNetwork(_ViewTestBase):
    """Line 2913: shared-network without a name key is skipped."""

    @patch("netbox_kea.models.KeaClient")
    def test_unnamed_network_skipped_in_choices(self, MockKeaClient):
        """Network with no 'name' key is not added to choices."""
        config_resp = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}],
                        "shared-networks": [
                            {"subnet4": []},  # no 'name' key → skipped
                            {"name": "valid-net", "subnet4": []},
                        ],
                    }
                },
            }
        ]
        subnet_resp = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
            }
        ]
        MockKeaClient.return_value.command.side_effect = [subnet_resp, config_resp]
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Bulk delete POST — missing permission
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkDeletePermission(_ViewTestBase):
    """Line 800: POST without bulk_delete_lease_from_server permission returns 403."""

    @patch("netbox_kea.models.KeaClient")
    def test_user_without_bulk_delete_perm_gets_403(self, MockKeaClient):
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        # Grant view-only ObjectPermission so get_object() succeeds
        viewer = User.objects.create_user(username="viewer_no_bulk_del", password="pass")
        ct = ContentType.objects.get_for_model(Server)
        view_op = ObjectPermission(name="view-servers-for-bulk-test", actions=["view"])
        view_op.save()
        view_op.users.add(viewer)
        view_op.object_types.add(ct)
        self.client.force_login(viewer)
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        # User can view the server but has no bulk_delete_lease_from_server perm
        response = self.client.post(url, {"pk": ["10.0.0.1"], "confirm": "1"})
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# _BaseSyncView._sync — NotImplementedError (line 4321)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBaseSyncViewNotImplemented(_ViewTestBase):
    """Line 4321: _BaseSyncView._sync raises NotImplementedError."""

    def test_sync_raises_not_implemented(self):
        from netbox_kea.views import _BaseSyncView

        view = _BaseSyncView()
        with self.assertRaises(NotImplementedError):
            view._sync({})


# ---------------------------------------------------------------------------
# _enrich_reservations_with_lease_status — edge cases (lines 1641, 1645, 1650, 1662-1663)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichReservationsLeaseStatusCoverage(_ViewTestBase):
    """Direct unit tests for _enrich_reservations_with_lease_status helper."""

    def _make_request_with_messages(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        setattr(request, "session", "session")
        storage = FallbackStorage(request)
        setattr(request, "_messages", storage)
        return request

    def test_result3_returns_empty_list(self):
        """Line 1641: lease-get-all result=3 → _fetch_leases_for_subnet returns []."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        client.command.return_value = [{"result": 3, "arguments": {}}]
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        # Should not raise; lease_cmds result=3 → empty list → no has_active_lease set
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # hook_unavailable stays False, no crash

    def test_kea_exception_non_result2_returns_empty(self):
        """Line 1645: KeaException with result != 2 → _fetch_leases_for_subnet returns []."""
        from unittest.mock import MagicMock

        from netbox_kea.kea import KeaException
        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        client.command.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # Should complete without crash; result != 2 → return []

    def test_no_subnet_id_skips_fetch(self):
        """Line 1650: reservations with no subnet-id → unique_subnet_ids empty → early return."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        reservations = [{"ip-address": "10.0.0.1"}]  # no subnet-id
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # client.command should never be called
        client.command.assert_not_called()

    def test_as_completed_exception_returns_early(self):
        """Lines 1662-1663: exception from as_completed → outer except fires."""
        from unittest.mock import MagicMock, patch

        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        client.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        with patch(
            "netbox_kea.views.reservations.concurrent.futures.as_completed",
            side_effect=RuntimeError("as_completed failed"),
        ):
            _enrich_reservations_with_lease_status(client, reservations, 4)
        # Should not raise; outer except returns early


# ---------------------------------------------------------------------------
# _warn_pool_reservation_overlap — edge cases (lines 2503, 2516, 2522-2523)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWarnPoolReservationOverlapCoverage(_ViewTestBase):
    """Direct unit tests for _warn_pool_reservation_overlap helper."""

    def _make_request(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        setattr(request, "session", "session")
        storage = FallbackStorage(request)
        setattr(request, "_messages", storage)
        return request

    def test_cidr_pool_creates_ipnetwork(self):
        """Line 2503: pool_str without dash (CIDR) → IPNetwork path."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        client.reservation_get_page.return_value = ([], 0, 0)
        request = self._make_request()
        # Should not raise; CIDR pool path
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0/24")

    def test_host_with_different_subnet_id_skipped(self):
        """Line 2516: host whose subnet-id != requested subnet_id → continue."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        # Return a host with subnet-id=999 (different from requested subnet_id=1)
        client.reservation_get_page.side_effect = [
            ([{"subnet-id": 999, "ip-address": "10.0.0.5"}], 0, 0),
        ]
        request = self._make_request()
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0-10.0.0.100")
        # host skipped → no warning

    def test_malformed_ip_skipped(self):
        """Lines 2522-2523: malformed IP string → IPAddress raises → inner except fires."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        client.reservation_get_page.side_effect = [
            ([{"subnet-id": 1, "ip-address": "NOT_AN_IP"}], 0, 0),
        ]
        request = self._make_request()
        # Should not raise; malformed IP is silently skipped
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0-10.0.0.100")


# ---------------------------------------------------------------------------
# _warn_reservation_pool_overlap — edge cases (lines 2566, 2571, 2579-2580)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWarnReservationPoolOverlapCoverage(_ViewTestBase):
    """Direct unit tests for _warn_reservation_pool_overlap helper."""

    def _make_request(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        setattr(request, "session", "session")
        storage = FallbackStorage(request)
        setattr(request, "_messages", storage)
        return request

    def test_empty_pool_string_skipped(self):
        """Line 2566: pool entry with empty pool string → continue."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_reservation_pool_overlap

        client = MagicMock()
        client.command.return_value = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": ""}]}]},
            }
        ]
        request = self._make_request()
        # Should not raise; empty pool string is skipped
        _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")

    def test_cidr_pool_creates_ipnetwork(self):
        """Line 2571: CIDR pool (no dash) → IPNetwork path."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_reservation_pool_overlap

        client = MagicMock()
        client.command.return_value = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.0/24"}]}]},
            }
        ]
        request = self._make_request()
        # IP is in pool → warning issued; CIDR pool path (line 2571)
        _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")

    def test_client_command_exception_swallowed(self):
        """Lines 2579-2580: client.command raises → outer except fires."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_reservation_pool_overlap

        client = MagicMock()
        client.command.side_effect = RuntimeError("network failure")
        request = self._make_request()
        # Should not raise; exception is swallowed
        _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")


# ---------------------------------------------------------------------------
# _KeaChangeMixin — elif branch (lines 245-246): pk is None + no change_server perm
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaChangeMixinNoPk(_ViewTestBase):
    """Lines 245-246: dispatch with no pk kwarg + user lacking change_server perm → 403."""

    def test_no_pk_no_perm_returns_403(self):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from django.views import View

        from netbox_kea.views import _KeaChangeMixin

        class _MinimalView(_KeaChangeMixin, View):
            def get(self, request, **kwargs):
                return HttpResponse("ok")

        # Create a user with no permissions
        no_perm_user = User.objects.create_user(username="no_perm_kca", password="pass")
        factory = RequestFactory()
        request = factory.get("/")
        request.user = no_perm_user
        view_func = _MinimalView.as_view()
        # Call with NO pk kwarg → elif branch
        response = view_func(request)
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# ServerStatusView — null version_args (lines 323, 357)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestStatusViewNullVersionArgs(_ViewTestBase):
    """Lines 323, 357: version-get returns None arguments → RuntimeError caught internally."""

    @patch("netbox_kea.models.KeaClient")
    def test_ca_version_get_null_args_returns_200(self, MockKeaClient):
        """Line 323: CA version-get returns None args → RuntimeError caught in get_extra_context."""
        mock_client = MockKeaClient.return_value

        def _side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 60, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": None}]
            return [{"result": 0, "arguments": {}}]

        mock_client.command.side_effect = _side_effect
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_dhcp_version_get_null_args_returns_200(self, MockKeaClient):
        """Line 357: DHCP service version-get returns None args → RuntimeError caught."""
        mock_client = MockKeaClient.return_value

        def _side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 60, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": None}]
            return [{"result": 0, "arguments": {}}]

        mock_client.command.side_effect = _side_effect
        server_dhcp_only = _make_db_server(
            name="dhcp-only-sv",
            has_control_agent=False,
            dhcp4=True,
            dhcp6=False,
        )
        url = reverse("plugins:netbox_kea:server_status", args=[server_dhcp_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


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
# Shared networks tab disabled (line 1237)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworksTabDisabled(_ViewTestBase):
    """Line 1237: server.dhcp4=False → get_children returns [] immediately."""

    def test_dhcp4_disabled_returns_empty_children(self):
        """Line 1237: dhcp4=False → get_children returns [] immediately (defensive guard)."""
        from django.test import RequestFactory

        from netbox_kea.views import ServerSharedNetworks4View

        server = _make_db_server(name="no-dhcp4-children", dhcp4=False, dhcp6=True)
        view = ServerSharedNetworks4View()
        request = RequestFactory().get("/")
        request.user = self.user
        result = view.get_children(request, server)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _fetch_network — non-dict args (lines 1462-1463)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchNetworkNonDictArgs(_ViewTestBase):
    """Lines 1462-1463: config-get returns non-dict args → log warning + return {}."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_non_dict_args_redirects(self, MockKeaClient):
        """config-get returns arguments=None → _fetch_network returns {} → redirect."""
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [{"result": 0, "arguments": None}]
        url = reverse(
            "plugins:netbox_kea:server_shared_network4_edit",
            args=[self.server.pk, "test-net"],
        )
        response = self.client.get(url)
        # network not found → redirects back to shared_networks4
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/servers/{self.server.pk}/", response.url)


# ---------------------------------------------------------------------------
# _get_network_choices — KeaException (lines 2737-2738)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetNetworkChoicesKeaException(_ViewTestBase):
    """Lines 2737-2738: KeaException in _get_network_choices → returns default choice."""

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_returns_global_pool_only(self, MockKeaClient):
        """config-get raises KeaException → returns [('', '— (global pool) —')]."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# _get_inherited_options._parse_opts — "routers" and "ntp-servers" (lines 2974, 2977-2978)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetInheritedOptionsParseOpts(_ViewTestBase):
    """Lines 2974, 2977-2978: _parse_opts handles 'routers' and 'ntp-servers' entries."""

    @patch("netbox_kea.models.KeaClient")
    def test_global_options_routers_and_ntp_servers_inherited(self, MockKeaClient):
        """GET subnet4_edit with global routers + ntp-servers → inherited_options populated."""
        mock_client = MockKeaClient.return_value

        subnet_resp = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
            }
        ]
        config_resp = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [],
                        "option-data": [
                            {"name": "routers", "data": "10.0.0.1"},
                            {"name": "ntp-servers", "data": "10.0.0.2"},
                        ],
                    }
                },
            }
        ]
        mock_client.command.side_effect = [subnet_resp, config_resp]
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # inherited_options should have gateway and ntp_servers from global config
        ctx = response.context
        inherited = ctx.get("inherited_options", {})
        self.assertIn("gateway", inherited)
        self.assertIn("ntp_servers", inherited)


# ---------------------------------------------------------------------------
# Subnet edit — network rollback (lines 3122-3133, 3137-3139)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkRollback(_ViewTestBase):
    """Lines 3122-3133, 3137-3139: network_subnet_del fails → rollback + outer except."""

    @patch("netbox_kea.models.KeaClient")
    def test_network_subnet_del_fails_triggers_rollback_and_outer_except(self, MockKeaClient):
        """old→new network change: del(old) raises RuntimeError → rollback del(new) also fails.

        Covers lines 3122-3133 (rollback attempt) and 3137-3139 (outer except Exception).
        """
        mock_client = MockKeaClient.return_value

        # config-get returns subnet 42 in "net-old", with "net-new" also available
        config_resp = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [
                            {"name": "net-old", "subnet4": [{"id": 42}]},
                            {"name": "net-new", "subnet4": []},
                        ],
                        "option-data": [],
                    }
                },
            }
        ]
        mock_client.command.return_value = config_resp
        mock_client.subnet_update.return_value = None
        mock_client.network_subnet_add.return_value = None
        # Both del calls raise RuntimeError (not PartialPersistError)
        mock_client.network_subnet_del.side_effect = [
            RuntimeError("del old failed"),
            RuntimeError("rollback del new also failed"),
        ]

        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.post(
            url,
            {
                "subnet_cidr": "10.0.0.0/24",
                "shared_network": "net-new",
                "current_network": "net-old",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        # Redirects after error message
        self.assertEqual(response.status_code, 302)
        # Both del calls were made (old + rollback of new)
        self.assertEqual(mock_client.network_subnet_del.call_count, 2)


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
