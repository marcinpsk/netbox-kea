# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""REST API tests for the lease endpoints on ServerViewSet.

These tests cover:
- GET /api/plugins/netbox-kea/servers/{pk}/leases4/
- GET /api/plugins/netbox-kea/servers/{pk}/leases6/

All Kea HTTP calls are mocked; no running Kea instance required.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_kea.models import Server

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

_LEASE4_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "ip-address": "10.0.0.100",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "subnet-id": 1,
            "hostname": "host.example.com",
            "valid-lft": 3600,
            "cltt": 1700000000,
            "state": 0,
        },
    }
]

_LEASE4_LIST_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "leases": [
                {
                    "ip-address": "10.0.0.100",
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "subnet-id": 1,
                    "hostname": "host.example.com",
                    "valid-lft": 3600,
                    "cltt": 1700000000,
                    "state": 0,
                }
            ]
        },
    }
]

_LEASE4_NOT_FOUND = [{"result": 3, "text": "Lease not found."}]

_LEASE6_LIST_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "leases": [
                {
                    "ip-address": "2001:db8::1",
                    "duid": "00:01:02:03",
                    "iaid": 12345,
                    "subnet-id": 10,
                    "valid-lft": 3600,
                    "cltt": 1700000000,
                    "state": 0,
                }
            ]
        },
    }
]


def _make_server(**kwargs):
    defaults = {
        "name": "test-kea-api",
        "server_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _APITestBase(TestCase):
    """Creates a superuser + API token and a single Server for API tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="api_testuser",
            email="api_test@example.com",
            password="api_testpass",
        )
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)
        self.server = _make_server()


# ─────────────────────────────────────────────────────────────────────────────
# Authentication tests
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAPIAuth(_APITestBase):
    """API endpoints must reject unauthenticated requests."""

    def test_leases4_requires_auth(self):
        """GET leases4 without token returns 403."""
        anon = APIClient()
        url = reverse("plugins-api:netbox_kea-api:server-leases4", args=[self.server.pk])
        response = anon.get(url, {"ip_address": "10.0.0.1"})
        self.assertIn(response.status_code, (401, 403))

    def test_leases6_requires_auth(self):
        """GET leases6 without token returns 403."""
        anon = APIClient()
        url = reverse("plugins-api:netbox_kea-api:server-leases6", args=[self.server.pk])
        response = anon.get(url, {"ip_address": "2001:db8::1"})
        self.assertIn(response.status_code, (401, 403))


# ─────────────────────────────────────────────────────────────────────────────
# Lease4 search tests
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease4API(_APITestBase):
    """Tests for GET /api/plugins/netbox-kea/servers/{pk}/leases4/."""

    def _url(self):
        return reverse("plugins-api:netbox_kea-api:server-leases4", args=[self.server.pk])

    def test_no_filter_params_returns_400(self):
        """Requesting leases4 without any filter param returns HTTP 400."""
        response = self.api_client.get(self._url())
        self.assertEqual(response.status_code, 400)

    def test_server_not_found_returns_404(self):
        """Non-existent server PK returns HTTP 404."""
        url = reverse("plugins-api:netbox_kea-api:server-leases4", args=[99999])
        response = self.api_client.get(url, {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 404)

    @patch("netbox_kea.models.KeaClient")
    def test_get_by_ip_address_returns_200(self, MockKeaClient):
        """?ip_address=10.0.0.100 returns 200 with lease data."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = _LEASE4_RESPONSE
        response = self.api_client.get(self._url(), {"ip_address": "10.0.0.100"})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_by_ip_address_results_in_response(self, MockKeaClient):
        """Response includes a 'results' list and 'count' key."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = _LEASE4_RESPONSE
        response = self.api_client.get(self._url(), {"ip_address": "10.0.0.100"})
        data = response.json()
        self.assertIn("results", data)
        self.assertIn("count", data)
        self.assertEqual(data["count"], 1)

    @patch("netbox_kea.models.KeaClient")
    def test_get_by_hw_address_returns_200(self, MockKeaClient):
        """?hw_address=aa:bb:cc:dd:ee:ff returns 200 with lease list."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = _LEASE4_LIST_RESPONSE
        response = self.api_client.get(self._url(), {"hw_address": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    @patch("netbox_kea.models.KeaClient")
    def test_get_by_hostname_returns_200(self, MockKeaClient):
        """?hostname=host.example.com returns 200."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = _LEASE4_LIST_RESPONSE
        response = self.api_client.get(self._url(), {"hostname": "host.example.com"})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_by_subnet_id_returns_200(self, MockKeaClient):
        """?subnet_id=1 returns 200."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = _LEASE4_LIST_RESPONSE
        response = self.api_client.get(self._url(), {"subnet_id": "1"})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_not_found_returns_empty_results(self, MockKeaClient):
        """When Kea returns result=3 (not found), results is empty list."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = _LEASE4_NOT_FOUND
        response = self.api_client.get(self._url(), {"ip_address": "10.0.0.99"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)
        self.assertEqual(response.json()["results"], [])

    @patch("netbox_kea.models.KeaClient")
    def test_kea_connection_error_returns_502(self, MockKeaClient):
        """When Kea is unreachable, returns HTTP 502."""
        import requests as rq

        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.side_effect = rq.ConnectionError("refused")
        response = self.api_client.get(self._url(), {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 502)


# ─────────────────────────────────────────────────────────────────────────────
# Lease6 search tests
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease6API(_APITestBase):
    """Tests for GET /api/plugins/netbox-kea/servers/{pk}/leases6/."""

    def _url(self):
        return reverse("plugins-api:netbox_kea-api:server-leases6", args=[self.server.pk])

    def test_no_filter_params_returns_400(self):
        """Requesting leases6 without any filter returns HTTP 400."""
        response = self.api_client.get(self._url())
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_get_by_ip_address_returns_200(self, MockKeaClient):
        """?ip_address=2001:db8::1 returns 200 with v6 lease data."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "ip-address": "2001:db8::1",
                    "duid": "00:01:02:03",
                    "iaid": 12345,
                    "subnet-id": 10,
                    "valid-lft": 3600,
                    "cltt": 1700000000,
                    "state": 0,
                },
            }
        ]
        response = self.api_client.get(self._url(), {"ip_address": "2001:db8::1"})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_by_duid_returns_200(self, MockKeaClient):
        """?duid=00:01:02:03 returns 200 with v6 lease list."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = _LEASE6_LIST_RESPONSE
        response = self.api_client.get(self._url(), {"duid": "00:01:02:03"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    @patch("netbox_kea.models.KeaClient")
    def test_uses_dhcp6_service(self, MockKeaClient):
        """The v6 endpoint calls Kea with service=['dhcp6']."""
        mock_client = MagicMock()
        MockKeaClient.return_value = mock_client
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "ip-address": "2001:db8::1",
                    "duid": "00:01:02:03",
                    "iaid": 12345,
                    "valid-lft": 3600,
                    "cltt": 1700000000,
                    "state": 0,
                },
            }
        ]
        self.api_client.get(self._url(), {"ip_address": "2001:db8::1"})
        call_kwargs = mock_client.command.call_args
        service_arg = call_kwargs[1].get("service") or call_kwargs[0][1] if call_kwargs[0] else None
        if service_arg is None:
            # Try keyword arg
            service_arg = call_kwargs.kwargs.get("service") if hasattr(call_kwargs, "kwargs") else None
        # At minimum verify the command was called with dhcp6 in the service
        called_cmd = mock_client.command.call_args
        self.assertIn("dhcp6", str(called_cmd))
