# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import requests
from django.test import SimpleTestCase
from django.urls import reverse

from netbox_kea.reservations import ReservationIdentity, lease_identities

from .kea_stub import _catalogue_responses_for_subnets, _leases_per_subnet, _res_page, stub_kea
from .utils import _ViewTestBase

_SUBNETS4 = [{"id": 20, "subnet": "198.18.0.0/24"}, {"id": 21, "subnet": "198.18.1.0/24"}]
_SUBNETS6 = [{"id": 30, "subnet": "2001:db8::/64"}]


class TestSharedLeaseIdentityRules(SimpleTestCase):
    """Lease enrichment and Reservation enrichment read one identity rule set.

    They receive the same lease from different sources: Kea's own spelling
    (``hw-address``) and the template-safe spelling (``hw_address``). Two rule sets
    would let one side accept an identifier the other silently ignores.
    """

    def test_both_lease_spellings_yield_the_same_normalized_identities(self):
        raw = {"hw-address": "AA-BB-CC-DD-EE-FF", "client-id": "01:AA:BB"}
        enriched = {"hw_address": "AA-BB-CC-DD-EE-FF", "client_id": "01:AA:BB"}

        self.assertEqual(lease_identities(raw, 4), lease_identities(enriched, 4))
        self.assertEqual(
            lease_identities(raw, 4),
            (ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:ff"), ReservationIdentity("client-id", "01:aa:bb")),
        )

    def test_identifiers_a_lease_cannot_carry_are_never_matched(self):
        """Kea leases carry no circuit-id or flex-id, so neither can match from a lease."""
        self.assertEqual(lease_identities({"flex-id": "port-7", "circuit-id": "eth0"}, 4), ())
        self.assertEqual(lease_identities({"client-id": "01:aa:bb"}, 6), ())

    def test_a_malformed_identifier_is_dropped_instead_of_matching(self):
        self.assertEqual(lease_identities({"hw-address": "not-a-mac"}, 4), ())


class TestReservationLeaseRelationship(_ViewTestBase):
    """The Reservation table reports one lease relationship per complete Reservation."""

    def _url(self, version: int = 4) -> str:
        return reverse(f"plugins:netbox_kea:server_reservations{version}", args=[self.server.pk])

    def _rows(self, responses, version: int = 4, subnets=None):
        merged = _catalogue_responses_for_subnets(version, subnets or _SUBNETS4)
        merged.update(responses)
        with stub_kea(merged) as kea:
            response = self.client.get(self._url(version))
        self.assertEqual(response.status_code, 200)
        return response, response.context["table"].data.data, kea

    def test_an_unreadable_lease_observation_reports_no_relationship(self):
        """A failed lease query is unknown, not a confirmed absence of a lease."""
        response, rows, _kea = self._rows(
            {
                "reservation-get-page": _res_page(
                    [{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]
                ),
                "lease4-get-by-state": requests.ConnectionError("kea unreachable"),
            }
        )

        self.assertIsNone(rows[0]["has_active_lease"])
        self.assertNotContains(response, "No Lease")
        self.assertNotContains(response, "Active Lease")

    def test_a_missing_lease_hook_reports_no_relationship(self):
        """Without lease_cmds the plugin cannot observe leases, so it must claim nothing."""
        response, rows, _kea = self._rows(
            {
                "reservation-get-page": _res_page(
                    [{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]
                ),
                "lease4-get-by-state": {"result": 2, "text": "unknown command"},
                "lease4-get-all": {"result": 2, "text": "unknown command"},
            }
        )

        self.assertIsNone(rows[0]["has_active_lease"])
        self.assertNotContains(response, "No Lease")

    def test_an_unexpected_worker_failure_is_logged_at_exception_level(self):
        """An unexpected enrichment failure is visible without DEBUG logging."""
        with self.assertLogs("netbox_kea.views.reservations", level="ERROR") as logs:
            response, rows, _kea = self._rows(
                {
                    "reservation-get-page": _res_page(
                        [{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]
                    ),
                    "lease4-get-by-state": TypeError("unexpected worker failure"),
                }
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(rows[0]["has_active_lease"])
        self.assertIn("Reservation lease enrichment failed", logs.output[0])

    def test_equal_identity_in_another_subnet_is_not_an_active_lease(self):
        """A lease with the same hardware address in another Subnet is a different host."""
        _response, rows, _kea = self._rows(
            {
                "reservation-get-page": _res_page(
                    [{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]
                ),
                "lease4-get-by-state": _leases_per_subnet(
                    {21: [{"subnet-id": 21, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.1.20"}]}
                ),
            }
        )

        self.assertIs(rows[0]["has_active_lease"], False)

    def test_a_global_reservation_matches_a_lease_by_normalized_identity(self):
        """Global Scope has no Subnet, so only the normalized identity can match."""
        _response, rows, kea = self._rows(
            {
                "reservation-get-page": _res_page([{"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:ff"}]),
                "lease4-get-by-hw-address": {
                    "result": 0,
                    "arguments": {
                        "leases": [
                            {
                                "subnet-id": 21,
                                "hw-address": "AA-BB-CC-DD-EE-FF",
                                "ip-address": "198.18.1.20",
                                "state": 0,
                            }
                        ]
                    },
                },
            }
        )

        self.assertIs(rows[0]["has_active_lease"], True)
        self.assertIn("by=hw", rows[0]["lease_url"])
        self.assertIn("q=aa%3Abb%3Acc%3Add%3Aee%3Aff", rows[0]["lease_url"])
        self.assertEqual(kea.bodies("lease4-get-by-hw-address")[0]["arguments"], {"hw-address": "aa:bb:cc:dd:ee:ff"})

    def test_a_malformed_global_lease_state_reports_no_relationship(self):
        """Only a validated Kea state can establish or reject a lease relationship."""
        cases = [
            ("missing", {}),
            ("string", {"state": "0"}),
            ("boolean", {"state": True}),
            ("out of range", {"state": 99}),
        ]

        for label, state_fields in cases:
            with self.subTest(state=label):
                response, rows, _kea = self._rows(
                    {
                        "reservation-get-page": _res_page([{"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:ff"}]),
                        "lease4-get-by-hw-address": {
                            "result": 0,
                            "arguments": {
                                "leases": [
                                    {
                                        "subnet-id": 21,
                                        "hw-address": "AA-BB-CC-DD-EE-FF",
                                        "ip-address": "198.18.1.20",
                                        **state_fields,
                                    }
                                ]
                            },
                        },
                    }
                )

                self.assertIsNone(rows[0]["has_active_lease"])
                self.assertNotContains(response, "No Lease")
                self.assertNotContains(response, "Active Lease")

    def test_a_malformed_subnet_lease_state_reports_no_relationship(self):
        """A filtered Subnet response still requires an assigned lease state."""
        cases = [
            ("missing", {}),
            ("released", {"state": 1}),
            ("string", {"state": "0"}),
            ("boolean", {"state": True}),
            ("out of range", {"state": 99}),
        ]

        for label, state_fields in cases:
            with self.subTest(state=label):
                response, rows, _kea = self._rows(
                    {
                        "reservation-get-page": _res_page(
                            [{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]
                        ),
                        "lease4-get-by-state": _leases_per_subnet(
                            {
                                20: [
                                    {
                                        "subnet-id": 20,
                                        "hw-address": "aa:bb:cc:dd:ee:ff",
                                        "ip-address": "198.18.0.20",
                                        **state_fields,
                                    }
                                ]
                            }
                        ),
                    }
                )

                self.assertIsNone(rows[0]["has_active_lease"])
                self.assertNotContains(response, "No Lease")
                self.assertNotContains(response, "Active Lease")

    def test_a_global_reservation_never_infers_a_subnet_from_its_address(self):
        """An address must not put a Global Reservation into a Subnet lease query."""
        _response, rows, kea = self._rows(
            {
                "reservation-get-page": _res_page(
                    [{"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]
                ),
                "lease4-get-by-hw-address": {"result": 3},
            }
        )

        self.assertIs(rows[0]["has_active_lease"], False)
        self.assertEqual([name for name in kea.commands() if name.startswith("lease4-")], ["lease4-get-by-hw-address"])

    def test_a_global_identity_kea_cannot_search_reports_no_relationship(self):
        """Kea has no lease query for a Flex ID, so its lease state is unknown."""
        response, rows, kea = self._rows({"reservation-get-page": _res_page([{"subnet-id": 0, "flex-id": "port-7"}])})

        self.assertIsNone(rows[0]["has_active_lease"])
        self.assertEqual([name for name in kea.commands() if name.startswith("lease4-")], [])
        self.assertNotContains(response, "No Lease")

    def test_an_addressless_reservation_links_the_lease_search_by_identity(self):
        """An addressless Reservation has no address, so the identity selects the route."""
        _response, rows, _kea = self._rows(
            {
                "reservation-get-page": _res_page([{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff"}]),
                "lease4-get-by-state": _leases_per_subnet(
                    {
                        20: [
                            {
                                "subnet-id": 20,
                                "hw-address": "aa:bb:cc:dd:ee:ff",
                                "ip-address": "198.18.0.77",
                                "state": 0,
                            }
                        ]
                    }
                ),
            }
        )

        self.assertIs(rows[0]["has_active_lease"], True)
        self.assertIn("by=hw", rows[0]["lease_url"])
        self.assertNotIn("by=ip", rows[0]["lease_url"])

    def test_a_multi_address_reservation_links_the_address_that_holds_the_lease(self):
        """No address is the primary one, so the link follows the address that matched."""
        _response, rows, _kea = self._rows(
            {
                "reservation-get-page": _res_page(
                    [
                        {
                            "subnet-id": 30,
                            "duid": "00:01:02:03",
                            "ip-addresses": ["2001:db8::20", "2001:db8::21"],
                        }
                    ]
                ),
                "lease6-get-by-state": _leases_per_subnet(
                    {
                        30: [
                            {
                                "subnet-id": 30,
                                "duid": "00:01:02:04",
                                "ip-address": "2001:db8::21",
                                "state": 0,
                            }
                        ]
                    }
                ),
            },
            version=6,
            subnets=_SUBNETS6,
        )

        self.assertIs(rows[0]["has_active_lease"], True)
        self.assertIn("q=2001%3Adb8%3A%3A21", rows[0]["lease_url"])
