# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import yaml
from django.test import override_settings
from django.urls import reverse

from .kea_stub import _res_get, _res_page, queued, stub_kea
from .utils import _PLUGINS_CONFIG, _ViewTestBase


def _catalogue_responses(version: int, subnet_id: int, cidr: str) -> dict:
    subnet_key = f"subnet{version}"
    subnet = {"id": subnet_id, "subnet": cidr}
    return {
        f"subnet{version}-list": {"result": 0, "arguments": {"subnets": [subnet]}},
        "config-get": {
            "result": 0,
            "arguments": {f"Dhcp{version}": {subnet_key: [subnet]}, "hash": "reservation-ui-catalogue"},
        },
    }


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPerServerReservationSnapshots(_ViewTestBase):
    def _url(self, version: int = 4) -> str:
        return reverse(f"plugins:netbox_kea:server_reservations{version}", args=[self.server.pk])

    def test_renders_valid_records_scope_and_incomplete_diagnostics_from_one_page(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        hosts = [
            {"subnet-id": 0, "flex-id": "global-class", "hostname": "global.example.invalid"},
            {
                "subnet-id": 20,
                "hw-address": "AA-BB-CC-DD-EE-FF",
                "ip-address": "198.18.0.20",
                "hostname": "valid.example.invalid",
                "option-data": [{"name": "domain-name-servers", "data": "198.18.0.53"}],
            },
            {"subnet-id": 20, "remote-id": "private rejected value"},
            *[{"subnet-id": 20, "flex-id": f"page-filler-{index}"} for index in range(97)],
        ]
        responses.update(
            {
                "reservation-get-page": _res_page(
                    hosts,
                    next_from=3,
                    next_source=1,
                ),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        )

        with stub_kea(responses) as kea:
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "global.example.invalid")
        self.assertContains(response, "valid.example.invalid")
        self.assertContains(response, "Global")
        self.assertContains(response, "198.18.0.0/24")
        self.assertContains(response, "domain-name-servers")
        self.assertContains(response, "Snapshot is incomplete")
        self.assertContains(response, "1 malformed Reservation")
        self.assertNotContains(response, "private rejected value")
        self.assertContains(response, "Next page")
        self.assertEqual(len(kea.bodies("reservation-get-page")), 1)

        rows = response.context["table"].data.data
        global_row = next(row for row in rows if row["scope_kind"] == "global")
        self.assertIsNone(global_row["edit_url"])
        self.assertIsNone(global_row["delete_url"])
        self.assertIsNone(global_row.get("sync_url"))

    def test_scope_filter_keeps_only_global_records_on_the_current_page(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = _res_page(
            [
                {"subnet-id": 0, "flex-id": "global-class", "hostname": "global.example.invalid"},
                {
                    "subnet-id": 20,
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "hostname": "local.example.invalid",
                },
            ]
        )

        with stub_kea(responses):
            response = self.client.get(self._url(), {"scope": "global"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "global.example.invalid")
        self.assertNotContains(response, "local.example.invalid")

    def test_active_lease_matches_by_identity_when_the_reserved_address_differs(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [
                        {
                            "subnet-id": 20,
                            "hw-address": "aa:bb:cc:dd:ee:ff",
                            "ip-address": "198.18.0.20",
                        }
                    ]
                ),
                "lease4-get-by-state": {
                    "result": 0,
                    "arguments": {
                        "leases": [
                            {
                                "subnet-id": 20,
                                "hw-address": "AA-BB-CC-DD-EE-FF",
                                "ip-address": "198.18.0.21",
                            }
                        ]
                    },
                },
            }
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        row = response.context["table"].data.data[0]
        self.assertTrue(row["has_active_lease"])

    def test_exports_the_complete_current_snapshot_as_yaml(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = _res_page(
            [
                {
                    "subnet-id": 20,
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "ip-address": "198.18.0.20",
                    "hostname": "export.example.invalid",
                }
            ]
        )

        with stub_kea(responses):
            response = self.client.get(self._url(), {"export": "yaml"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/yaml")
        document = yaml.safe_load(response.content)
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["reservations"][0]["scope"]["subnet"]["cidr"], "198.18.0.0/24")
        self.assertNotIn("subnet-id", response.content.decode())

    def test_refuses_to_export_an_incomplete_snapshot(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = _res_page([{"subnet-id": 20, "remote-id": "not-native"}])

        with stub_kea(responses):
            response = self.client.get(self._url(), {"export": "json"})

        self.assertEqual(response.status_code, 409)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservationSnapshots(_ViewTestBase):
    def test_combined_view_fetches_one_bounded_page_and_offers_the_next_cursor(self):
        responses = _catalogue_responses(6, 30, "2001:db8::/64")
        hosts = [
            {
                "subnet-id": 30,
                "duid": "00-01-02-03",
                "ip-addresses": ["2001:db8::20", "2001:db8::21"],
                "prefixes": ["2001:db8:100::/56"],
            },
            *[{"subnet-id": 30, "flex-id": f"page-filler-{index}"} for index in range(99)],
        ]
        responses["reservation-get-page"] = _res_page(
            hosts,
            next_from=1,
            next_source=1,
        )
        responses["lease6-get-by-state"] = {"result": 0, "arguments": {"leases": []}}
        url = reverse("plugins:netbox_kea:combined_reservations6")

        with stub_kea(responses) as kea:
            response = self.client.get(url, {"server": self.server.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2001:db8::20")
        self.assertContains(response, "2001:db8::21")
        self.assertContains(response, "2001:db8:100::/56")
        self.assertContains(response, "Next page")
        self.assertEqual(len(kea.bodies("reservation-get-page")), 1)

    def test_combined_export_uses_the_normalized_json_schema(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = queued(
            _res_page(
                [{"subnet-id": 20, "flex-id": "first-page", "hostname": "first.example.invalid"}],
                next_from=1,
                next_source=1,
            ),
            _res_page([{"subnet-id": 20, "flex-id": "second-page", "hostname": "second.example.invalid"}]),
        )
        url = reverse("plugins:netbox_kea:combined_reservations4")

        with stub_kea(responses) as kea:
            response = self.client.get(url, {"server": self.server.pk, "export": "json"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [record["identity"] for record in response.json()["reservations"]],
            [
                {"type": "flex-id", "value": "first-page"},
                {"type": "flex-id", "value": "second-page"},
            ],
        )
        self.assertEqual(len(kea.bodies("reservation-get-page")), 2)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseReservationIdentityMatching(_ViewTestBase):
    lease = {
        "ip-address": "198.18.0.20",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "subnet-id": 20,
        "hostname": "lease.example.invalid",
        "cltt": 1_700_000_000,
        "valid-lft": 3600,
        "state": 0,
    }

    def _get(self, reservation_responses):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "lease4-get": {"result": 0, "arguments": self.lease},
                "reservation-get": reservation_responses,
            }
        )
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(responses):
            return self.client.get(url, {"by": "ip", "q": "198.18.0.20"}, HTTP_HX_REQUEST="true")

    def test_addressless_reservation_matches_normalized_identity_in_the_same_subnet(self):
        response = self._get(
            queued(
                {"result": 3},
                _res_get({"subnet-id": 20, "hw-address": "AA-BB-CC-DD-EE-FF", "hostname": "classified"}),
            )
        )

        self.assertEqual(response.status_code, 200)
        row = response.context["table"].data.data[0]
        self.assertTrue(row["is_reserved"])
        self.assertFalse(row["pending_ip_change"])
        self.assertIn("identifier_type=hw-address", row["reservation_url"])
        self.assertIn("identifier=aa%3Abb%3Acc%3Add%3Aee%3Aff", row["reservation_url"])

    def test_global_reservation_matches_by_identity_and_has_no_mutation_link(self):
        response = self._get(
            queued(
                {"result": 3},
                {"result": 3},
                _res_get({"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "global"}),
            )
        )

        row = response.context["table"].data.data[0]
        self.assertTrue(row["is_reserved"])
        self.assertIsNone(row["reservation_url"])
        self.assertIsNone(row["create_reservation_url"])

    def test_wrong_subnet_address_result_is_indeterminate_and_offers_no_action(self):
        response = self._get(
            _res_get(
                {
                    "subnet-id": 21,
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "ip-address": "198.18.0.20",
                }
            )
        )

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIsNone(row["reservation_url"])
        self.assertIsNone(row["create_reservation_url"])

    def test_identity_match_at_another_address_reports_one_pending_change(self):
        response = self._get(
            queued(
                {"result": 3},
                _res_get(
                    {
                        "subnet-id": 20,
                        "hw-address": "aa:bb:cc:dd:ee:ff",
                        "ip-address": "198.18.0.21",
                    }
                ),
            )
        )

        row = response.context["table"].data.data[0]
        self.assertTrue(row["is_reserved"])
        self.assertTrue(row["pending_ip_change"])
        self.assertEqual(row["pending_reservation_ip"], "198.18.0.21")
        self.assertIsNone(row["create_reservation_url"])
        self.assertIn("/reservations4/20/edit/", row["reservation_url"])
        self.assertIn("identifier_type=hw-address", row["reservation_url"])

    def test_confirmed_absence_offers_a_prefilled_create_action(self):
        response = self._get(queued({"result": 3}, {"result": 3}, {"result": 3}))

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIn("/reservations4/add/", row["create_reservation_url"])
        self.assertIn("subnet_cidr=198.18.0.0%2F24", row["create_reservation_url"])

    def test_unavailable_host_commands_do_not_offer_a_false_create_action(self):
        response = self._get({"result": 2, "text": "command not supported"})

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIsNone(row["create_reservation_url"])

    def test_lookup_failure_is_indeterminate_and_offers_no_action(self):
        response = self._get(RuntimeError("transport failed"))

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIsNone(row["reservation_url"])
        self.assertIsNone(row["create_reservation_url"])

    def test_address_match_with_a_different_hardware_identity_is_stale(self):
        response = self._get(
            _res_get(
                {
                    "subnet-id": 20,
                    "hw-address": "aa:bb:cc:dd:ee:01",
                    "ip-address": "198.18.0.20",
                }
            )
        )

        row = response.context["table"].data.data[0]
        self.assertTrue(row["is_reserved"])
        self.assertTrue(row["stale_mac"])
        self.assertEqual(row["stale_lease_mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(row["reservation_mac"], "aa:bb:cc:dd:ee:01")
