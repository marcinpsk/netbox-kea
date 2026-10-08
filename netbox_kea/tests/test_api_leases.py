# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""REST API tests for the lease endpoints on ServerViewSet.

These tests cover:
- GET /api/plugins/netbox-kea/servers/{pk}/leases4/
- GET /api/plugins/netbox-kea/servers/{pk}/leases6/

These tests drive the **real** ``KeaClient``; only the HTTP boundary is stubbed
via ``kea_stub.stub_kea``. The lease endpoints issue ``lease{v}-get`` (by IP),
``lease{v}-get-by-hw-address``, ``lease6-get-by-duid``,
``lease{v}-get-by-hostname``, or ``lease{v}-get-all`` (by subnet); the real
request payloads (command + service) are exercised and asserted.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_kea.constants import MAX_SUBNET_ID
from netbox_kea.models import Server

from .kea_stub import complete_lease, stub_kea
from .utils import plugins_config

# A single DHCPv6 lease (result 0) returned by lease6-get.
_LEASE6_RESPONSE = [
    {
        "result": 0,
        "arguments": complete_lease(
            {
                "ip-address": "2001:db8::1",
                "duid": "00:01:02:03",
                "iaid": 12345,
                "subnet-id": 10,
                "valid-lft": 3600,
                "cltt": 1700000000,
                "state": 0,
            }
        ),
    }
]

User = get_user_model()

_PLUGINS_CONFIG = plugins_config(lease_query_max_unpaged_leases=0)

_LEASE4_RESPONSE = [
    {
        "result": 0,
        "arguments": complete_lease(
            {
                "ip-address": "10.0.0.100",
                "hw-address": "aa:bb:cc:dd:ee:ff",
                "subnet-id": 1,
                "hostname": "host.example.com",
                "valid-lft": 3600,
                "cltt": 1700000000,
                "state": 0,
            }
        ),
    }
]

_LEASE4_LIST_RESPONSE = [
    {
        "result": 0,
        "arguments": {"leases": [_LEASE4_RESPONSE[0]["arguments"]]},
    }
]

_LEASE4_NOT_FOUND = [{"result": 3, "text": "Lease not found."}]

_LEASE6_LIST_RESPONSE = [
    {
        "result": 0,
        "arguments": {"leases": [_LEASE6_RESPONSE[0]["arguments"]]},
    }
]


def _make_server(**kwargs):
    defaults = {
        "name": "test-kea-api",
        "ca_url": "https://kea.example.com",
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


class TestLeaseAPISubnetIdBounds(_APITestBase):
    def test_subnet_ids_outside_the_kea_range_are_refused_without_kea_requests(self):
        for family in (4, 6):
            for subnet_id in ("0", str(MAX_SUBNET_ID + 1), "99999999999999999999"):
                with self.subTest(family=family, subnet_id=subnet_id), stub_kea({}) as kea:
                    url = reverse(f"plugins-api:netbox_kea-api:server-leases{family}", args=[self.server.pk])
                    response = self.api_client.get(url, {"subnet_id": subnet_id})

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"detail": f"subnet_id must be from 1 to {MAX_SUBNET_ID}."})
                    self.assertEqual(kea.commands(), [])

    def test_subnet_ids_that_are_not_ascii_decimal_text_are_refused_without_kea_requests(self):
        for family in (4, 6):
            for subnet_id in ("\u0661\u0662", " 12", "12 "):
                with self.subTest(family=family, subnet_id=subnet_id), stub_kea({}) as kea:
                    url = reverse(f"plugins-api:netbox_kea-api:server-leases{family}", args=[self.server.pk])
                    response = self.api_client.get(url, {"subnet_id": subnet_id})

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"detail": "subnet_id must be an integer."})
                    self.assertEqual(kea.commands(), [])


class TestLeaseAPIFormatSuffix(_APITestBase):
    def test_invalid_selectors_have_the_same_response_on_both_routes(self):
        for family in (4, 6):
            with self.subTest(family=family):
                name = f"plugins-api:netbox_kea-api:server-leases{family}"
                plain_url = reverse(name, kwargs={"pk": self.server.pk})
                suffixed_url = reverse(name, kwargs={"pk": self.server.pk, "format": "json"})
                with stub_kea({}) as kea:
                    plain = self.api_client.get(plain_url, {"subnet_id": "not-an-integer"})
                    suffixed = self.api_client.get(suffixed_url, {"subnet_id": "not-an-integer"})

                self.assertEqual(plain.status_code, 400)
                self.assertEqual(suffixed.status_code, 400)
                self.assertEqual(suffixed.json(), {"detail": "subnet_id must be an integer."})
                self.assertEqual(suffixed.json(), plain.json())
                self.assertEqual(kea.commands(), [])

    def test_subnet_id_and_state_must_be_ascii_decimal_text(self):
        url = reverse("plugins-api:netbox_kea-api:server-leases4", args=[self.server.pk])
        cases = (
            ({"subnet_id": "\u0661\u0662"}, "subnet_id must be an integer."),
            ({"subnet_id": " 12"}, "subnet_id must be an integer."),
            ({"subnet_id": "12", "state": "\u0661"}, "A Subnet query supports only the Active or Declined state."),
        )
        for params, message in cases:
            with self.subTest(params=params), stub_kea({}) as kea:
                response = self.api_client.get(url, params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": message})
                self.assertEqual(kea.commands(), [])

    def test_permission_denial_has_the_same_response_without_kea_requests(self):
        denied_user = User.objects.create_user(username="denied_lease_reader")
        self.api_client.force_authenticate(user=denied_user)
        for family, address in ((4, "198.18.0.100"), (6, "2001:db8::1")):
            with self.subTest(family=family):
                name = f"plugins-api:netbox_kea-api:server-leases{family}"
                plain_url = reverse(name, kwargs={"pk": self.server.pk})
                suffixed_url = reverse(name, kwargs={"pk": self.server.pk, "format": "json"})
                with stub_kea({}) as kea:
                    plain = self.api_client.get(plain_url, {"ip_address": address})
                    suffixed = self.api_client.get(suffixed_url, {"ip_address": address})

                self.assertEqual(plain.status_code, 403)
                self.assertEqual(suffixed.status_code, 403)
                self.assertEqual(suffixed.json(), plain.json())
                self.assertEqual(kea.commands(), [])


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

    def test_non_integer_subnet_id_returns_400(self):
        """?subnet_id=abc returns HTTP 400."""
        response = self.api_client.get(self._url(), {"subnet_id": "abc"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("subnet_id", response.json()["detail"])

    def test_duid_returns_400(self):
        """DHCPv4 rejects the DHCPv6-only DUID selector."""
        response = self.api_client.get(self._url(), {"duid": "00:01:02:03"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("DHCPv6", response.json()["detail"])

    def test_nonexistent_server_returns_404(self):
        """Non-existent server PK returns HTTP 404."""
        url = reverse("plugins-api:netbox_kea-api:server-leases4", args=[99999])
        response = self.api_client.get(url, {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 404)

    def test_get_by_ip_address_returns_200(self):
        """?ip_address=10.0.0.100 returns 200 with lease data."""
        with stub_kea({"lease4-get": _LEASE4_RESPONSE}):
            response = self.api_client.get(self._url(), {"ip_address": "10.0.0.100"})
        self.assertEqual(response.status_code, 200)

    def test_json_suffix_returns_the_same_lease_as_the_plain_route(self):
        url = reverse("plugins-api:netbox_kea-api:server-leases4", kwargs={"pk": self.server.pk, "format": "json"})
        reply = {"result": 0, "arguments": {**_LEASE4_RESPONSE[0]["arguments"], "ip-address": "198.18.0.100"}}
        with stub_kea({"lease4-get": reply}) as kea:
            plain = self.api_client.get(self._url(), {"ip_address": "198.18.0.100"})
            suffixed = self.api_client.get(url, {"ip_address": "198.18.0.100"})

        self.assertEqual(plain.status_code, 200)
        self.assertEqual(suffixed.status_code, 200)
        self.assertEqual(suffixed["Content-Type"], "application/json")
        # Each read has its own evaluation time.
        self.assertEqual({**suffixed.json(), "evaluated_at": None}, {**plain.json(), "evaluated_at": None})
        self.assertEqual(suffixed.json()["count"], 1)
        self.assertEqual(suffixed.json()["results"][0]["address"], "198.18.0.100")
        self.assertEqual(suffixed.json()["results"][0]["binding"]["hw_address"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(suffixed.json()["results"][0]["state"], "assigned")
        self.assertEqual(kea.commands(), ["lease4-get", "lease4-get"])

    def test_get_by_ip_address_results_in_response(self):
        """Response includes a 'results' list and 'count' key."""
        with stub_kea({"lease4-get": _LEASE4_RESPONSE}):
            response = self.api_client.get(self._url(), {"ip_address": "10.0.0.100"})
        data = response.json()
        self.assertIn("results", data)
        self.assertIn("count", data)
        self.assertEqual(data["count"], 1)

    def test_blank_subnet_id_does_not_override_the_ip_selector(self):
        """An empty non-selected Subnet filter does not reject an IP search."""
        with stub_kea({"lease4-get": _LEASE4_RESPONSE}) as kea:
            response = self.api_client.get(
                self._url(),
                {"ip_address": "10.0.0.100", "subnet_id": ""},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), ["lease4-get"])

    def test_get_by_hw_address_returns_200(self):
        """?hw_address=aa:bb:cc:dd:ee:ff returns 200 with lease list."""
        with stub_kea({"lease4-get-by-hw-address": _LEASE4_LIST_RESPONSE}):
            response = self.api_client.get(self._url(), {"hw_address": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_get_by_hostname_returns_200(self):
        """?hostname=host.example.com returns 200."""
        with stub_kea({"lease4-get-by-hostname": _LEASE4_LIST_RESPONSE}):
            response = self.api_client.get(self._url(), {"hostname": "host.example.com"})
        self.assertEqual(response.status_code, 200)

    def test_get_by_subnet_id_returns_200(self):
        """?subnet_id=1 returns 200."""
        with stub_kea({"lease4-get-all": _LEASE4_LIST_RESPONSE}):
            response = self.api_client.get(self._url(), {"subnet_id": "1"})
        self.assertEqual(response.status_code, 200)

    def test_not_found_returns_empty_results(self):
        """When Kea returns result=3 (not found), results is empty list."""
        with stub_kea({"lease4-get": _LEASE4_NOT_FOUND}):
            response = self.api_client.get(self._url(), {"ip_address": "10.0.0.99"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)
        self.assertEqual(response.json()["results"], [])

    def test_kea_connection_error_returns_502(self):
        """When Kea is unreachable, returns HTTP 502."""
        import requests as rq

        with stub_kea({"lease4-get": rq.ConnectionError("refused")}):
            response = self.api_client.get(self._url(), {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 502)

    def test_uses_dhcp4_service(self):
        """The v4 endpoint issues lease4-get to service=['dhcp4'] (version=4 routing)."""
        with stub_kea({"lease4-get": _LEASE4_RESPONSE}) as kea:
            self.api_client.get(self._url(), {"ip_address": "10.0.0.100"})
        self.assertEqual(kea.bodies("lease4-get")[0]["service"], ["dhcp4"])


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

    def test_non_integer_subnet_id_returns_400(self):
        """?subnet_id=not-a-number returns HTTP 400."""
        response = self.api_client.get(self._url(), {"subnet_id": "not-a-number"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("subnet_id", response.json()["detail"])

    def test_hardware_address_returns_400(self):
        """DHCPv6 rejects the DHCPv4-only hardware-address selector."""
        response = self.api_client.get(self._url(), {"hw_address": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("DHCPv4", response.json()["detail"])

    def test_nonexistent_server_returns_404(self):
        """Non-existent server PK returns HTTP 404."""
        url = reverse("plugins-api:netbox_kea-api:server-leases6", args=[99999])
        response = self.api_client.get(url, {"ip_address": "2001:db8::1"})
        self.assertEqual(response.status_code, 404)

    def test_get_by_ip_address_returns_200(self):
        """?ip_address=2001:db8::1 returns 200 with v6 lease data."""
        with stub_kea({"lease6-get": _LEASE6_RESPONSE}):
            response = self.api_client.get(self._url(), {"ip_address": "2001:db8::1"})
        self.assertEqual(response.status_code, 200)

    def test_json_suffix_returns_the_same_lease_as_the_plain_route(self):
        url = reverse("plugins-api:netbox_kea-api:server-leases6", kwargs={"pk": self.server.pk, "format": "json"})
        with stub_kea({"lease6-get": _LEASE6_RESPONSE}) as kea:
            plain = self.api_client.get(self._url(), {"ip_address": "2001:db8::1"})
            suffixed = self.api_client.get(url, {"ip_address": "2001:db8::1"})

        self.assertEqual(plain.status_code, 200)
        self.assertEqual(suffixed.status_code, 200)
        self.assertEqual(suffixed["Content-Type"], "application/json")
        # Each read has its own evaluation time.
        self.assertEqual({**suffixed.json(), "evaluated_at": None}, {**plain.json(), "evaluated_at": None})
        self.assertEqual(suffixed.json()["count"], 1)
        self.assertEqual(suffixed.json()["results"][0]["address"], "2001:db8::1")
        self.assertEqual(suffixed.json()["results"][0]["binding"]["duid"], "00:01:02:03")
        self.assertEqual(suffixed.json()["results"][0]["state"], "assigned")
        # Each DHCPv6 address search reads the address and the delegated prefix at that address.
        self.assertEqual(kea.commands(), ["lease6-get"] * 4)

    def test_get_by_duid_returns_200(self):
        """?duid=00:01:02:03 returns 200 with v6 lease list."""
        with stub_kea({"lease6-get-by-duid": _LEASE6_LIST_RESPONSE}):
            response = self.api_client.get(self._url(), {"duid": "00:01:02:03"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_uses_dhcp6_service(self):
        """The v6 endpoint issues lease6-get to service=['dhcp6']."""
        with stub_kea({"lease6-get": _LEASE6_RESPONSE}) as kea:
            self.api_client.get(self._url(), {"ip_address": "2001:db8::1"})
        self.assertEqual(kea.bodies("lease6-get")[0]["service"], ["dhcp6"])

    def test_uses_version_6_command(self):
        """The v6 endpoint selects the DHCPv6 client → issues the lease6-* command variant."""
        with stub_kea({"lease6-get": _LEASE6_RESPONSE}) as kea:
            self.api_client.get(self._url(), {"ip_address": "2001:db8::1"})
        self.assertIn("lease6-get", kea.commands())
        self.assertNotIn("lease4-get", kea.commands())
