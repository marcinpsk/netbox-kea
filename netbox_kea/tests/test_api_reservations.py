# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""REST API tests for the normalized Reservation actions on ServerViewSet.

These tests cover:
- GET /api/plugins/netbox-kea/servers/{pk}/reservations4/
- GET /api/plugins/netbox-kea/servers/{pk}/reservations6/

These tests drive the real ``KeaClient`` and stub only its HTTP boundary. Each
request selects one bounded page, exact identity, scoped address, or hostname
query. The tests assert the normalized family-neutral response.
"""

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_kea.models import Server

from .kea_stub import _catalogue_responses, _res_get, _res_page, stub_kea
from .utils import _drop_subnet_choices_cache

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

# Not found — reservation-get result=3
_RESERVATION_NOT_FOUND = [{"result": 3, "text": "Host not found."}]


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
        # Reused test server IDs otherwise serve another test's cached Catalogue
        # snapshot: the DB rolls back per test, the cache backend does not.
        _drop_subnet_choices_cache(self, self.server)


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
        response = self.api_client.get(self._url(), {"ip_address": "198.18.0.20", "subnet_id": "foo"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid scoped address", response.json()["detail"])

    def test_conflicting_selectors_return_400_without_a_kea_request(self):
        with stub_kea({}) as kea:
            response = self.api_client.get(
                self._url(),
                {
                    "ip_address": "10.0.0.50",
                    "subnet_id": "1",
                    "hostname": "host.example.invalid",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("exactly one", response.json()["detail"])
        self.assertEqual(kea.commands(), [])

    def test_query_modes_reject_parameters_from_another_mode(self):
        cases = (
            {"page": "1", "subnet_id": "20"},
            {"hostname": "host.example.invalid", "scope": "global"},
        )

        for params in cases:
            with self.subTest(params=params), stub_kea({}) as kea:
                response = self.api_client.get(self._url(), params)

                self.assertEqual(response.status_code, 400)
                self.assertIn("exactly one", response.json()["detail"])
                self.assertEqual(kea.commands(), [])

    def test_server_not_found_returns_404(self):
        """Non-existent server PK returns HTTP 404."""
        response = self.api_client.get(self._url(pk=99999), {"page": "1"})
        self.assertEqual(response.status_code, 404)

    def test_scoped_address_returns_the_normalized_canonical_record(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get"] = _res_get(
            {
                "subnet-id": 20,
                "client-id": "01-AA-BB-CC-DD-EE-FF",
                "ip-address": "198.18.0.20",
            }
        )

        with stub_kea(responses) as kea:
            response = self.api_client.get(
                self._url(),
                {"ip_address": "198.18.0.20", "subnet_id": "20"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"][0]["identity"],
            {"type": "client-id", "value": "01:aa:bb:cc:dd:ee:ff"},
        )
        self.assertEqual(response.json()["results"][0]["addresses"], ["198.18.0.20"])
        self.assertEqual(kea.commands(), ["subnet4-list", "config-get", "reservation-get"])

    def test_legacy_hw_address_selector_is_rejected_without_a_kea_request(self):
        with stub_kea({}) as kea:
            response = self.api_client.get(self._url(), {"hw_address": "aa:bb:cc:dd:ee:ff", "subnet_id": "1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), [])

    def test_exact_identity_returns_normalized_record(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get"] = _res_get(
            {
                "subnet-id": 20,
                "hw-address": "AA-BB-CC-DD-EE-FF",
                "ip-address": "198.18.0.20",
            }
        )

        with stub_kea(responses) as kea:
            response = self.api_client.get(
                self._url(),
                {
                    "scope": "in-subnet",
                    "subnet_id": "20",
                    "identifier_type": "hw-address",
                    "identifier": "AA-BB-CC-DD-EE-FF",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(
            response.json()["results"][0]["identity"],
            {"type": "hw-address", "value": "aa:bb:cc:dd:ee:ff"},
        )
        self.assertEqual(response.json()["results"][0]["scope"]["subnet"]["cidr"], "198.18.0.0/24")
        self.assertEqual(kea.commands(), ["subnet4-list", "config-get", "reservation-get"])

    def test_subnet_only_selector_is_rejected_without_unbounded_iteration(self):
        with stub_kea({}) as kea:
            response = self.api_client.get(self._url(), {"subnet_id": "1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), [])

    def test_not_found_returns_empty_results(self):
        """When reservation-get returns not-found (result 3), results is empty with count=0."""
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get"] = _RESERVATION_NOT_FOUND
        with stub_kea(responses):
            response = self.api_client.get(self._url(), {"ip_address": "198.18.0.99", "subnet_id": "20"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["results"], [])

    def test_kea_connection_error_returns_502(self):
        """When Kea is unreachable, returns HTTP 502."""
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get"] = requests.ConnectionError("refused")
        with stub_kea(responses):
            response = self.api_client.get(self._url(), {"ip_address": "198.18.0.20", "subnet_id": "20"})
        self.assertEqual(response.status_code, 502)

    def test_uses_dhcp4_service(self):
        """The v4 endpoint issues reservation-get to service='dhcp4'."""
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get"] = _res_get(
            {"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}
        )
        with stub_kea(responses) as kea:
            self.api_client.get(self._url(), {"ip_address": "198.18.0.20", "subnet_id": "20"})
        self.assertEqual(kea.bodies("reservation-get")[0]["service"], ["dhcp4"])

    def test_page_rejects_invalid_limits_without_a_kea_request(self):
        with stub_kea({}) as kea:
            for limit in ("not-an-integer", "0", "501"):
                with self.subTest(limit=limit):
                    response = self.api_client.get(self._url(), {"page": "1", "limit": limit})
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), [])

    def test_page_rejects_an_invalid_cursor_after_catalogue_validation(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        with stub_kea(responses) as kea:
            response = self.api_client.get(self._url(), {"page": "1", "cursor": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), ["subnet4-list", "config-get"])

    def test_page_maps_connection_kea_and_malformed_errors(self):
        cases = (
            (requests.ConnectionError("refused"), 502),
            ({"result": 1, "text": "failed"}, 502),
            ([], 502),
        )
        for page_response, expected_status in cases:
            with self.subTest(page_response=page_response):
                responses = _catalogue_responses(4, 20, "198.18.0.0/24")
                responses["reservation-get-page"] = page_response
                with stub_kea(responses):
                    response = self.api_client.get(self._url(), {"page": "1"})
                self.assertEqual(response.status_code, expected_status)

    def test_all_query_modes_map_request_exceptions_to_502(self):
        cases = (
            ("page", "reservation-get-page", {"page": "1"}),
            (
                "identity",
                "reservation-get",
                {
                    "scope": "in-subnet",
                    "subnet_id": "20",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                },
            ),
            ("address", "reservation-get", {"ip_address": "198.18.0.20", "subnet_id": "20"}),
            ("hostname", "reservation-get-by-hostname", {"hostname": "host.example.invalid"}),
        )

        for query_mode, command, params in cases:
            with self.subTest(query_mode=query_mode):
                responses = _catalogue_responses(4, 20, "198.18.0.0/24")
                responses[command] = requests.TooManyRedirects("redirect loop")
                with stub_kea(responses):
                    response = self.api_client.get(self._url(), params)
                self.assertEqual(response.status_code, 502)

    def test_identity_requires_both_parts_and_a_valid_scope(self):
        with stub_kea({}) as kea:
            response = self.api_client.get(self._url(), {"identifier_type": "hw-address"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), [])

        for params in (
            {"scope": "other", "identifier_type": "hw-address", "identifier": "aa:bb:cc:dd:ee:ff"},
            {
                "scope": "in-subnet",
                "subnet_id": "21",
                "identifier_type": "hw-address",
                "identifier": "aa:bb:cc:dd:ee:ff",
            },
            {"scope": "global", "identifier_type": "hw-address", "identifier": "invalid"},
        ):
            with self.subTest(params=params):
                responses = _catalogue_responses(4, 20, "198.18.0.0/24")
                with stub_kea(responses):
                    response = self.api_client.get(self._url(), params)
                self.assertEqual(response.status_code, 400)

    def test_global_identity_returns_normalized_options(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get"] = _res_get(
            {
                "subnet-id": 0,
                "flex-id": "global-client",
                "option-data": [{"name": "domain-name", "data": "example.invalid"}],
            }
        )
        with stub_kea(responses):
            response = self.api_client.get(
                self._url(),
                {"scope": "global", "identifier_type": "flex-id", "identifier": "global-client"},
            )

        self.assertEqual(response.status_code, 200)
        record = response.json()["results"][0]
        self.assertEqual(record["scope"], {"type": "global"})
        self.assertEqual(record["options"][0]["name"], "domain-name")

    def test_global_identity_rejects_a_subnet_id(self):
        params = {
            "scope": "global",
            "subnet_id": "20",
            "identifier_type": "flex-id",
            "identifier": "global-client",
        }

        with stub_kea({}) as kea:
            response = self.api_client.get(self._url(), params)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid Reservation identity", response.json()["detail"])
        self.assertEqual(kea.commands(), [])

    def test_identity_maps_malformed_connection_kea_and_runtime_errors(self):
        cases = (
            (
                _res_get(
                    {
                        "subnet-id": 20,
                        "hw-address": "aa:bb:cc:dd:ee:ff",
                        "client-id": "01:02",
                    }
                ),
                502,
            ),
            (requests.ConnectionError("refused"), 502),
            ({"result": 1, "text": "failed"}, 502),
            ({"result": 0, "arguments": []}, 502),
        )
        params = {
            "scope": "in-subnet",
            "subnet_id": "20",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
        }
        for reservation_response, expected_status in cases:
            with self.subTest(reservation_response=reservation_response):
                responses = _catalogue_responses(4, 20, "198.18.0.0/24")
                responses["reservation-get"] = reservation_response
                with stub_kea(responses):
                    response = self.api_client.get(self._url(), params)
                self.assertEqual(response.status_code, expected_status)

    def test_address_maps_malformed_kea_and_runtime_errors(self):
        cases = (
            (
                _res_get(
                    {
                        "subnet-id": 20,
                        "hw-address": "aa:bb:cc:dd:ee:ff",
                        "ip-address": "198.18.0.21",
                    }
                ),
                502,
            ),
            ({"result": 1, "text": "failed"}, 502),
            ({"result": 0, "arguments": []}, 502),
        )
        for reservation_response, expected_status in cases:
            with self.subTest(reservation_response=reservation_response):
                responses = _catalogue_responses(4, 20, "198.18.0.0/24")
                responses["reservation-get"] = reservation_response
                with stub_kea(responses):
                    response = self.api_client.get(
                        self._url(),
                        {"ip_address": "198.18.0.20", "subnet_id": "20"},
                    )
                self.assertEqual(response.status_code, expected_status)

    def test_address_rejects_an_unverified_subnet(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        with stub_kea(responses) as kea:
            response = self.api_client.get(
                self._url(),
                {"ip_address": "198.18.1.20", "subnet_id": "21"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), ["subnet4-list", "config-get"])


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
        response = self.api_client.get(self._url(), {"ip_address": "2001:db8::20", "subnet_id": "not-a-number"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid scoped address", response.json()["detail"])

    def test_legacy_duid_selector_is_rejected_without_a_kea_request(self):
        with stub_kea({}) as kea:
            response = self.api_client.get(self._url(), {"duid": "00:01:02:03", "subnet_id": "10"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), [])

    def test_bounded_page_returns_normalized_snapshot(self):
        responses = {
            **_catalogue_responses(6, 10, "2001:db8::/64"),
            "reservation-get-page": _res_page(
                [
                    {
                        "subnet-id": 10,
                        "duid": "00-01-02-03",
                        "ip-addresses": ["2001:db8::20", "2001:db8::10"],
                        "prefixes": ["2001:db8:100::/56"],
                        "hostname": "host.example.invalid",
                    },
                    {"subnet-id": 10, "remote-id": "relay-value"},
                ],
                next_from=2,
                next_source=1,
            ),
        }

        with stub_kea(responses) as kea:
            response = self.api_client.get(self._url(), {"page": "1", "limit": "2"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), ["subnet6-list", "config-get", "reservation-get-page"])
        self.assertEqual(
            response.json(),
            {
                "count": 1,
                "results": [
                    {
                        "family": 6,
                        "scope": {
                            "type": "in-subnet",
                            "subnet": {"id": 10, "cidr": "2001:db8::/64"},
                        },
                        "identity": {"type": "duid", "value": "00:01:02:03"},
                        "addresses": ["2001:db8::20", "2001:db8::10"],
                        "delegated_prefixes": ["2001:db8:100::/56"],
                        "hostname": "host.example.invalid",
                        "options": [],
                    }
                ],
                "diagnostics": [
                    {
                        "code": "unsupported-identifier",
                        "message": (
                            "Relay remote ID is not a native Reservation Identity. "
                            "Configure the Kea Flex ID hook instead."
                        ),
                        "source_position": "hosts[1].remote-id",
                    }
                ],
                "complete": False,
                "next_cursor": "WzEsMl0",
            },
        )

    def test_hostname_returns_a_normalized_snapshot(self):
        responses = _catalogue_responses(6, 10, "2001:db8::/64")
        responses["reservation-get-by-hostname"] = {
            "result": 0,
            "arguments": {
                "hosts": [
                    {
                        "subnet-id": 10,
                        "duid": "00-01-02-03",
                        "ip-addresses": ["2001:db8::20"],
                        "hostname": "host.example.invalid",
                    },
                    {
                        "subnet-id": 10,
                        "duid": "00-01-02-04",
                        "hostname": "different.example.invalid",
                    },
                ]
            },
        }

        with stub_kea(responses) as kea:
            response = self.api_client.get(self._url(), {"hostname": "host.example.invalid"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["identity"], {"type": "duid", "value": "00:01:02:03"})
        self.assertEqual(
            response.json()["diagnostics"],
            [
                {
                    "code": "target-mismatch",
                    "message": "Kea returned a Reservation that does not match the requested hostname.",
                    "source_position": "hosts[1].hostname",
                }
            ],
        )
        self.assertFalse(response.json()["complete"])
        self.assertEqual(kea.commands(), ["subnet6-list", "config-get", "reservation-get-by-hostname"])

    def test_uses_dhcp6_service(self):
        responses = _catalogue_responses(6, 10, "2001:db8::/64")
        responses["reservation-get-by-hostname"] = {"result": 3}
        with stub_kea(responses) as kea:
            self.api_client.get(self._url(), {"hostname": "host.example.invalid"})
        self.assertEqual(kea.bodies("reservation-get-by-hostname")[0]["service"], ["dhcp6"])

    def test_hostname_maps_connection_kea_and_malformed_errors(self):
        cases = (
            requests.ConnectionError("refused"),
            {"result": 1, "text": "failed"},
            [],
        )
        for hostname_response in cases:
            with self.subTest(hostname_response=hostname_response):
                responses = _catalogue_responses(6, 10, "2001:db8::/64")
                responses["reservation-get-by-hostname"] = hostname_response
                with stub_kea(responses):
                    response = self.api_client.get(self._url(), {"hostname": "host.example.invalid"})
                self.assertEqual(response.status_code, 502)
