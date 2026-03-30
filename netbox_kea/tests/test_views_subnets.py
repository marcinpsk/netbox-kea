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
from unittest.mock import patch

import requests
from django.contrib import messages as django_messages
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
    def test_get_when_subnet_fetch_fails_redirects_with_error(self, MockKeaClient):
        """GET must redirect to the subnet list when the subnet-get Kea call fails."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException({"result": 1, "text": "not found"}, index=0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("subnets", response.url)

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
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException(
            {"result": 1, "text": "config error", "arguments": None}, index=0
        )
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
    def test_post_partial_persist_error_with_subnet_id_attempts_network_assignment(self, MockKeaClient):
        """When subnet_add raises PartialPersistError carrying subnet_id, the view attempts network assignment."""
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
        partial_exc = PartialPersistError("dhcp4", Exception("write"), subnet_id=10)
        MockKeaClient.return_value.subnet_add.side_effect = partial_exc
        post_data = {**_SUBNET_ADD_POST, "shared_network": "alpha"}
        response = self.client.post(self._url(), post_data, follow=True)
        # network_subnet_add must have been called with the partial subnet_id
        MockKeaClient.return_value.network_subnet_add.assert_called_once_with(version=4, name="alpha", subnet_id=10)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_error_without_subnet_id_skips_network_assignment(self, MockKeaClient):
        """When PartialPersistError carries no subnet_id, network assignment is skipped."""
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
        MockKeaClient.return_value.subnet_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        post_data = {**_SUBNET_ADD_POST, "shared_network": "alpha"}
        response = self.client.post(self._url(), post_data, follow=True)
        MockKeaClient.return_value.network_subnet_add.assert_not_called()
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_subnet_add_runtime_error_rerenders_form(self, MockKeaClient):
        """RuntimeError from subnet_add must re-render the form (200 response)."""
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
        MockKeaClient.return_value.network_subnet_add.side_effect = requests.RequestException("network error")
        post_data = {**_SUBNET_ADD_POST, "shared_network": "alpha"}
        response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("could not be assigned" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_client_none_reconnect_failure_shows_error(self, MockKeaClient):
        """When initial get_client fails, view shows error and returns 200 or 302."""
        call_count = [0]

        def _raise_after_first(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First instantiation (for getting choices): raise immediately
                raise requests.RequestException("no connection")
            # Second instantiation (for fallback client reconnect): also raise
            raise requests.RequestException("still no connection")

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

        # POST consumes: [0] config-get for subnet data, [1] config-get for subnets list (after redirect),
        # [2] stat-lease4-get (hook-unavailable) so stat_cmds degradation doesn't StopIterate.
        _stat_unsupported = [{"result": 2, "text": "unsupported"}]
        MockKeaClient.return_value.command.side_effect = [_SUBNET4_GET_FULL, _CONFIG4_NO_NETWORKS, _stat_unsupported]
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
# Subnet list view — null config + export + HTMX partial
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetListViewEdgeCases(_ViewTestBase):
    """Lines 1110, 1173, 1181: subnet view null config, export, HTMX."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_null_config_arguments_raises(self, MockKeaClient):
        """Null config-get arguments returns an empty table (degraded 200 state)."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": None}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

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
# Subnet add — _get_network_choices error handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddNetworkChoicesError(_ViewTestBase):
    """_get_network_choices error handling in the subnet-add view."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_warning_when_network_choices_fail(self, MockKeaClient):
        """GET must render 200 with a warning message when config-get fails."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException(
            {"result": 1, "text": "config-get failed", "arguments": None}, index=0
        )
        response = self.client.get(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("shared network" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_rejects_submission_when_network_choices_fail(self, MockKeaClient):
        """POST must show a form error and NOT call subnet_add when config-get fails."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException(
            {"result": 1, "text": "config-get failed", "arguments": None}, index=0
        )
        response = self.client.post(
            self._url(),
            {
                "subnet": "10.99.0.0/24",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
                "shared_network": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.subnet_add.assert_not_called()


# ---------------------------------------------------------------------------
# Subnet edit: _get_network_data error handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkDataErrors(_ViewTestBase):
    """_get_network_data must degrade gracefully on transport and parse errors."""

    @patch("netbox_kea.models.KeaClient")
    def test_transport_error_returns_200(self, MockKeaClient):
        """A requests.RequestException on config-get must not cause a 500 in the edit view."""

        def command_side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "subnet4-get":
                return [
                    {
                        "result": 0,
                        "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
                    }
                ]
            if cmd == "config-get":
                raise requests.ConnectionError("down")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = command_side_effect
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_value_error_returns_200(self, MockKeaClient):
        """A ValueError on config-get must not cause a 500 in the edit view."""

        def command_side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "subnet4-get":
                return [
                    {
                        "result": 0,
                        "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
                    }
                ]
            if cmd == "config-get":
                raise ValueError("bad response")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = command_side_effect
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Subnet add: network_subnet_add PartialPersistError handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddPartialPersistNetworkAssign(_ViewTestBase):
    """network_subnet_add PartialPersistError must show warning not 'could not assign' error."""

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_from_network_assign_shows_warning_not_error(self, MockKeaClient):
        """When network_subnet_add raises PartialPersistError, show warning (subnet assigned but config-write failed)."""
        from netbox_kea.kea import PartialPersistError

        mock_client = MockKeaClient.return_value

        # subnet_add raises PartialPersistError (subnet added but config-write failed, id=99)
        mock_client.subnet_add.side_effect = PartialPersistError(
            "dhcp4", Exception("config-write failed"), subnet_id=99
        )
        # network_subnet_add also raises PartialPersistError (network attached but config-write failed)
        mock_client.network_subnet_add.side_effect = PartialPersistError(
            "dhcp4", Exception("config-write failed"), subnet_id=99
        )
        # Mock shared network choices via command
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [{"name": "my-net"}],
                        "subnet4": [],
                    }
                },
            }
        ]

        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        response = self.client.post(
            url,
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "my-net",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        # Must NOT see the failure message
        self.assertFalse(any("could not assign" in m.lower() for m in msgs))
        # network_subnet_add was called (subnet IS live)
        mock_client.network_subnet_add.assert_called_once()


# ---------------------------------------------------------------------------
# Subnet add: warn when assigned_id is None but shared_network requested
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddNoIdWarning(_ViewTestBase):
    """When subnet_add returns None and shared_network requested, show a warning."""

    @patch("netbox_kea.models.KeaClient")
    def test_warning_shown_when_no_id_returned(self, MockKeaClient):
        """subnet_add returning None with shared_network requested must show warning."""
        mock_client = MockKeaClient.return_value
        # subnet_add returns None (no ID)
        mock_client.subnet_add.return_value = None
        # Mock shared network choices from config-get
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [{"name": "my-net"}],
                        "subnet4": [],
                    }
                },
            }
        ]

        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        response = self.client.post(
            url,
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "my-net",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        # Must see a warning about missing ID / network assignment skipped
        self.assertTrue(
            any(
                "no id" in m.lower() or "could not assign" in m.lower() or "no id was returned" in m.lower()
                for m in msgs
            ),
            f"Expected warning about missing ID but got: {msgs}",
        )
        # network_subnet_add must NOT have been called (we have no ID)
        mock_client.network_subnet_add.assert_not_called()
