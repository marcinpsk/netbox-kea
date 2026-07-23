# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""REST API tests for the reservation endpoints on ServerViewSet.

These tests cover:
- GET /api/plugins/netbox-kea/servers/{pk}/reservations4/
- GET /api/plugins/netbox-kea/servers/{pk}/reservations6/

These tests drive the **real** ``KeaClient``; only the HTTP boundary is stubbed
via ``kea_stub.stub_kea``. The reservation endpoints issue ``reservation-get``
(lookup by ip/hw/duid + subnet) or ``reservation-get-page`` (paginated, subnet-id
only); the request payloads are exercised and asserted.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_kea.models import Server

from .kea_stub import queued, stub_kea

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

# Single reservation returned by reservation-get (result=0, arguments has the host dict)
_RESERVATION4_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "ip-address": "10.0.0.50",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "subnet-id": 1,
            "hostname": "reserved.example.com",
        },
    }
]

# Not found — reservation-get result=3
_RESERVATION_NOT_FOUND = [{"result": 3, "text": "Host not found."}]

# reservation-get-page returns a "hosts" list under arguments
_RESERVATION4_PAGE_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "hosts": [
                {
                    "ip-address": "10.0.0.50",
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "subnet-id": 1,
                    "hostname": "reserved.example.com",
                }
            ],
            "next": {"from": 0, "source-index": 0},
        },
    }
]

_RESERVATION6_PAGE_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "hosts": [
                {
                    "ip-addresses": ["2001:db8::50"],
                    "duid": "00:01:02:03",
                    "subnet-id": 10,
                }
            ],
            "next": {"from": 0, "source-index": 0},
        },
    }
]

_RESERVATION6_SINGLE = [
    {
        "result": 0,
        "arguments": {
            "ip-addresses": ["2001:db8::50"],
            "duid": "00:01:02:03",
            "subnet-id": 10,
        },
    }
]


def _make_server(**kwargs):
    defaults = {
        "name": "test-kea-res-api",
        "ca_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _APITestBase(TestCase):
    """Creates a superuser + API client and a single Server for API tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="res_api_testuser",
            email="res_api_test@example.com",
            password="res_api_testpass",
        )
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)
        self.server = _make_server()


# ─────────────────────────────────────────────────────────────────────────────
# Authentication tests
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationAPIAuth(_APITestBase):
    """API endpoints must reject unauthenticated requests."""

    def test_reservations4_requires_auth(self):
        """GET reservations4 without token returns 403/401."""
        anon = APIClient()
        url = reverse("plugins-api:netbox_kea-api:server-reservations4", args=[self.server.pk])
        response = anon.get(url, {"subnet_id": "1"})
        self.assertIn(response.status_code, (401, 403))

    def test_reservations6_requires_auth(self):
        """GET reservations6 without token returns 403/401."""
        anon = APIClient()
        url = reverse("plugins-api:netbox_kea-api:server-reservations6", args=[self.server.pk])
        response = anon.get(url, {"subnet_id": "10"})
        self.assertIn(response.status_code, (401, 403))


# ─────────────────────────────────────────────────────────────────────────────
# Reservation4 tests
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4API(_APITestBase):
    """Tests for GET /api/plugins/netbox-kea/servers/{pk}/reservations4/."""

    def _url(self, pk=None):
        return reverse("plugins-api:netbox_kea-api:server-reservations4", args=[pk or self.server.pk])

    def test_no_filter_params_returns_400(self):
        """Requesting reservations4 without any filter param returns HTTP 400."""
        response = self.api_client.get(self._url())
        self.assertEqual(response.status_code, 400)

    def test_non_integer_subnet_id_returns_400(self):
        """?subnet_id=foo returns HTTP 400."""
        response = self.api_client.get(self._url(), {"subnet_id": "foo"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("subnet_id", response.json()["detail"])

    def test_server_not_found_returns_404(self):
        """Non-existent server PK returns HTTP 404."""
        response = self.api_client.get(self._url(pk=99999), {"subnet_id": "1"})
        self.assertEqual(response.status_code, 404)

    def test_get_by_ip_and_subnet_returns_200(self):
        """?ip_address=10.0.0.50&subnet_id=1 returns 200 with reservation data."""
        with stub_kea({"reservation-get": _RESERVATION4_RESPONSE}):
            response = self.api_client.get(self._url(), {"ip_address": "10.0.0.50", "subnet_id": "1"})
        self.assertEqual(response.status_code, 200)

    def test_get_by_ip_and_subnet_returns_count_and_results(self):
        """Response includes 'count' and 'results' keys."""
        with stub_kea({"reservation-get": _RESERVATION4_RESPONSE}):
            response = self.api_client.get(self._url(), {"ip_address": "10.0.0.50", "subnet_id": "1"})
        data = response.json()
        self.assertIn("count", data)
        self.assertIn("results", data)
        self.assertEqual(data["count"], 1)

    def test_get_by_hw_address_and_subnet_returns_200(self):
        """?hw_address=aa:bb&subnet_id=1 returns 200."""
        with stub_kea({"reservation-get": _RESERVATION4_RESPONSE}):
            response = self.api_client.get(self._url(), {"hw_address": "aa:bb:cc:dd:ee:ff", "subnet_id": "1"})
        self.assertEqual(response.status_code, 200)

    def test_get_by_subnet_id_uses_get_page(self):
        """?subnet_id=1 (no IP/hw) drives reservation-get-page and returns results."""
        with stub_kea({"reservation-get-page": _RESERVATION4_PAGE_RESPONSE}) as kea:
            response = self.api_client.get(self._url(), {"subnet_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertIn("reservation-get-page", kea.commands())

    def test_get_by_subnet_id_paginates_all_pages(self):
        """?subnet_id=1 fetches ALL pages from reservation-get-page, not just the first."""
        page1 = {
            "result": 0,
            "arguments": {
                "hosts": [{"ip-address": "10.0.0.51", "hw-address": "aa:bb:cc:dd:ee:01", "subnet-id": 1}],
                "next": {"from": 1, "source-index": 0},  # not exhausted
            },
        }
        page2 = {
            "result": 0,
            "arguments": {
                "hosts": [{"ip-address": "10.0.0.52", "hw-address": "aa:bb:cc:dd:ee:02", "subnet-id": 1}],
                "next": {"from": 0, "source-index": 0},  # exhausted
            },
        }
        with stub_kea({"reservation-get-page": queued(page1, page2)}) as kea:
            response = self.api_client.get(self._url(), {"subnet_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 2)
        self.assertEqual(len(kea.bodies("reservation-get-page")), 2)

    def test_not_found_returns_empty_results(self):
        """When reservation-get returns not-found (result 3), results is empty with count=0."""
        with stub_kea({"reservation-get": _RESERVATION_NOT_FOUND}):
            response = self.api_client.get(self._url(), {"ip_address": "10.0.0.99", "subnet_id": "1"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["results"], [])

    def test_kea_connection_error_returns_502(self):
        """When Kea is unreachable, returns HTTP 502."""
        import requests as rq

        with stub_kea({"reservation-get": rq.ConnectionError("refused")}):
            response = self.api_client.get(self._url(), {"ip_address": "10.0.0.1", "subnet_id": "1"})
        self.assertEqual(response.status_code, 502)

    def test_uses_dhcp4_service(self):
        """The v4 endpoint issues reservation-get to service='dhcp4'."""
        with stub_kea({"reservation-get": _RESERVATION4_RESPONSE}) as kea:
            self.api_client.get(self._url(), {"ip_address": "10.0.0.50", "subnet_id": "1"})
        self.assertEqual(kea.bodies("reservation-get")[0]["service"], ["dhcp4"])


# ─────────────────────────────────────────────────────────────────────────────
# Reservation6 tests
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6API(_APITestBase):
    """Tests for GET /api/plugins/netbox-kea/servers/{pk}/reservations6/."""

    def _url(self):
        return reverse("plugins-api:netbox_kea-api:server-reservations6", args=[self.server.pk])

    def test_no_filter_params_returns_400(self):
        """Requesting reservations6 without any filter param returns HTTP 400."""
        response = self.api_client.get(self._url())
        self.assertEqual(response.status_code, 400)

    def test_non_integer_subnet_id_returns_400(self):
        """?subnet_id=not-a-number returns HTTP 400."""
        response = self.api_client.get(self._url(), {"subnet_id": "not-a-number"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("subnet_id", response.json()["detail"])

    def test_get_by_ip_and_subnet_returns_200(self):
        """?ip_address=2001:db8::50&subnet_id=10 returns 200."""
        with stub_kea({"reservation-get": _RESERVATION6_SINGLE}):
            response = self.api_client.get(self._url(), {"ip_address": "2001:db8::50", "subnet_id": "10"})
        self.assertEqual(response.status_code, 200)

    def test_get_by_duid_and_subnet_returns_200(self):
        """?duid=00:01:02:03&subnet_id=10 returns 200."""
        with stub_kea({"reservation-get": _RESERVATION6_SINGLE}):
            response = self.api_client.get(self._url(), {"duid": "00:01:02:03", "subnet_id": "10"})
        self.assertEqual(response.status_code, 200)

    def test_uses_dhcp6_service(self):
        """The v6 endpoint issues reservation-get to service='dhcp6'."""
        with stub_kea({"reservation-get": _RESERVATION6_SINGLE}) as kea:
            self.api_client.get(self._url(), {"ip_address": "2001:db8::50", "subnet_id": "10"})
        self.assertEqual(kea.bodies("reservation-get")[0]["service"], ["dhcp6"])
