# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import requests
from django.test import SimpleTestCase
from django.urls import reverse

from netbox_kea.reservations import LeaseIdentities, ReservationIdentity, lease_identifier_types, lease_identities

from .kea_stub import (
    _catalogue_responses_for_subnets,
    _leases_per_subnet,
    _res_page,
    complete_lease,
    lease_record,
    stub_kea,
    typed_lease,
)
from .utils import _ViewTestBase

_SUBNETS4 = [{"id": 20, "subnet": "198.18.0.0/24"}, {"id": 21, "subnet": "198.18.1.0/24"}]
_SUBNETS6 = [{"id": 30, "subnet": "2001:db8::/64"}]


def _with_state(record: dict, state_fields: dict) -> dict:
    """Return a complete lease *record* whose state is exactly *state_fields*: an empty one drops it."""
    complete = complete_lease(record)
    del complete["state"]
    return {**complete, **state_fields}


class TestSharedLeaseIdentityRules(SimpleTestCase):
    """Lease enrichment and Reservation enrichment read the identities of one typed Lease."""

    def test_a_lease_yields_its_normalized_identities_in_match_order(self):
        lease = typed_lease({**lease_record("198.18.0.20"), "hw-address": "AA-BB-CC-DD-EE-FF", "client-id": "01:AA:BB"})

        self.assertEqual(
            lease_identities(lease),
            LeaseIdentities(
                (ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:ff"), ReservationIdentity("client-id", "01:aa:bb")),
                foreign=False,
            ),
        )

    def test_the_identifier_order_of_each_lease_kind_is_the_published_order(self):
        for record in (lease_record("198.18.0.20"), lease_record("2001:db8:1::10")):
            lease = typed_lease(record)
            with self.subTest(family=lease.family):
                types = [identity.identifier_type for identity in lease_identities(lease).identities]
                self.assertEqual(tuple(types), lease_identifier_types(lease.family))

    def test_empty_identifiers_are_no_identity(self):
        """Kea's empty DUID of a declined DHCPv6 lease is no identity, so it matches nothing."""
        declined = typed_lease(lease_record("2001:db8::12", duid="00:00:00", state=1, drop=("hw-address",)))
        self.assertEqual(lease_identities(declined), LeaseIdentities((), foreign=False))

    def test_an_identifier_that_no_reservation_can_hold_is_reported_not_dropped(self):
        long_client_id = ":".join(["01"] * 129)
        lease = typed_lease(lease_record("198.18.0.20", client_id=long_client_id))
        carried = lease_identities(lease)
        self.assertEqual(carried.identities, (ReservationIdentity("hw-address", "aa:bb:cc:00:00:10"),))
        self.assertTrue(carried.foreign)

    def test_lease_identifier_types_validate_the_family(self):
        self.assertEqual(lease_identifier_types(4), ("hw-address", "client-id"))
        self.assertEqual(lease_identifier_types(6), ("duid", "hw-address"))
        for family in (True, False, 4.0, 6.0, "4", 5):
            with self.subTest(family=family), self.assertRaises(ValueError):
                lease_identifier_types(family)


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

    def test_unobservable_scoped_identities_leave_the_lease_relationship_indeterminate(self):
        for version, identifier_type, identifier in (
            (4, "duid", "00:01:02:03"),
            (4, "circuit-id", "port-7"),
            (4, "flex-id", "port-7"),
            (6, "flex-id", "port-7"),
        ):
            with self.subTest(version=version, identifier_type=identifier_type):
                subnet_id = 20 if version == 4 else 30
                response, rows, _kea = self._rows(
                    {
                        "reservation-get-page": _res_page([{"subnet-id": subnet_id, identifier_type: identifier}]),
                        # DHCPv6 reads the whole Subnet, because a registered lease is current too.
                        f"lease{version}-get-by-state" if version == 4 else "lease6-get-all": _leases_per_subnet(
                            {subnet_id: []}
                        ),
                    },
                    version=version,
                    subnets=_SUBNETS4 if version == 4 else _SUBNETS6,
                )
                self.assertIsNone(rows[0]["has_active_lease"])
                self.assertNotContains(response, "No Lease")

    def test_an_unobservable_identity_can_still_match_by_address(self):
        response, rows, _kea = self._rows(
            {
                "reservation-get-page": _res_page(
                    [{"subnet-id": 20, "flex-id": "port-7", "ip-address": "198.18.0.20"}]
                ),
                "lease4-get-by-state": _leases_per_subnet(
                    {
                        20: [
                            complete_lease(
                                {
                                    "subnet-id": 20,
                                    "hw-address": "aa:bb:cc:dd:ee:ff",
                                    "ip-address": "198.18.0.20",
                                    "state": 0,
                                }
                            )
                        ],
                    }
                ),
            }
        )
        self.assertIs(rows[0]["has_active_lease"], True)
        self.assertContains(response, "Active Lease")

    def test_address_bearing_unobservable_identity_reports_no_lease_for_empty_subnet(self):
        for identifier_type in ("flex-id", "circuit-id"):
            with self.subTest(identifier_type=identifier_type):
                response, rows, _kea = self._rows(
                    {
                        "reservation-get-page": _res_page(
                            [{"subnet-id": 20, identifier_type: "port-7", "ip-address": "198.18.0.20"}]
                        ),
                        "lease4-get-by-state": _leases_per_subnet({20: []}),
                    }
                )
                self.assertIs(rows[0]["has_active_lease"], False)
                self.assertContains(response, "No Lease")

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

    def test_a_malformed_subnet_lease_identity_reports_no_relationship(self):
        for identifier in ("not-a-mac", "", False):
            with self.subTest(identifier=identifier):
                response, rows, _kea = self._rows(
                    {
                        "reservation-get-page": _res_page([{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff"}]),
                        "lease4-get-by-state": _leases_per_subnet(
                            {
                                20: [
                                    complete_lease(
                                        {
                                            "subnet-id": 20,
                                            "hw-address": identifier,
                                            "ip-address": "198.18.0.20",
                                            "state": 0,
                                        }
                                    )
                                ]
                            }
                        ),
                    }
                )
                self.assertIsNone(rows[0]["has_active_lease"])
                self.assertNotContains(response, "No Lease")
                self.assertNotContains(response, "Active Lease")
                self.assertContains(response, "Lease Unknown")

    def test_a_template_style_key_in_a_kea_record_is_not_an_identity(self):
        for native, expected in (("aa:bb:cc:dd:ee:ff", False), ("11-22-33-44-55-66", True)):
            with self.subTest(native=native):
                response, rows, _kea = self._rows(
                    {
                        "reservation-get-page": _res_page([{"subnet-id": 20, "hw-address": "11:22:33:44:55:66"}]),
                        "lease4-get-by-state": _leases_per_subnet(
                            {
                                20: [
                                    complete_lease(
                                        {
                                            "subnet-id": 20,
                                            "hw-address": native,
                                            "hw_address": "11:22:33:44:55:66",
                                            "ip-address": "198.18.0.20",
                                            "state": 0,
                                        }
                                    )
                                ]
                            }
                        ),
                    }
                )
                self.assertIs(rows[0]["has_active_lease"], expected)
                self.assertContains(response, "Active Lease" if expected else "No Lease")

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

    def test_a_failed_subnet_lease_query_leaves_only_that_subnet_indeterminate(self):
        """A Kea error for one Subnet is unknown there, and the other Subnet still answers."""

        def leases(body):
            if body["arguments"]["subnet-id"] == 20:
                return {"result": 1, "text": "lease database error"}
            return {"result": 3}

        _response, rows, kea = self._rows(
            {
                "reservation-get-page": _res_page(
                    [
                        {"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:01", "ip-address": "198.18.0.20"},
                        {"subnet-id": 21, "hw-address": "aa:bb:cc:dd:ee:02", "ip-address": "198.18.1.20"},
                    ]
                ),
                "lease4-get-by-state": leases,
            }
        )

        self.assertCountEqual(
            [body["arguments"]["subnet-id"] for body in kea.bodies("lease4-get-by-state")],
            [20, 21],
        )

        by_subnet = {row["subnet_id"]: row["has_active_lease"] for row in rows}
        self.assertEqual(by_subnet, {20: None, 21: False})

    def test_a_global_identity_query_error_reports_by_its_cause(self):
        """A missing identity query hides every relationship; any other Kea error hides only its own."""
        for reply, scoped in (
            ({"result": 2, "text": "unknown command"}, None),
            ({"result": 1, "text": "lease database error"}, False),
        ):
            with self.subTest(result=reply["result"]):
                _response, rows, _kea = self._rows(
                    {
                        "reservation-get-page": _res_page(
                            [
                                {"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:01"},
                                {"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:02", "ip-address": "198.18.0.20"},
                            ]
                        ),
                        "lease4-get-by-hw-address": reply,
                        "lease4-get-by-state": _leases_per_subnet({20: []}),
                    }
                )
                by_subnet = {row["subnet_id"]: row["has_active_lease"] for row in rows}
                self.assertEqual(by_subnet, {0: None, 20: scoped})

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
                    {
                        21: [
                            complete_lease(
                                {"subnet-id": 21, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.1.20"}
                            )
                        ]
                    }
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
                            complete_lease(
                                {
                                    "subnet-id": 21,
                                    "hw-address": "AA-BB-CC-DD-EE-FF",
                                    "ip-address": "198.18.1.20",
                                    "state": 0,
                                }
                            )
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
                                    _with_state(
                                        {
                                            "subnet-id": 21,
                                            "hw-address": "AA-BB-CC-DD-EE-FF",
                                            "ip-address": "198.18.1.20",
                                        },
                                        state_fields,
                                    )
                                ]
                            },
                        },
                    }
                )

                self.assertIsNone(rows[0]["has_active_lease"])
                self.assertNotContains(response, "No Lease")
                self.assertNotContains(response, "Active Lease")

    def test_a_malformed_subnet_lease_state_reports_no_relationship(self):
        """A malformed state excludes the record, so the observation cannot answer either way."""
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
                        "reservation-get-page": _res_page(
                            [{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]
                        ),
                        "lease4-get-by-state": _leases_per_subnet(
                            {
                                20: [
                                    _with_state(
                                        {
                                            "subnet-id": 20,
                                            "hw-address": "aa:bb:cc:dd:ee:ff",
                                            "ip-address": "198.18.0.20",
                                        },
                                        state_fields,
                                    )
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
                            complete_lease(
                                {
                                    "subnet-id": 20,
                                    "hw-address": "aa:bb:cc:dd:ee:ff",
                                    "ip-address": "198.18.0.77",
                                    "state": 0,
                                }
                            )
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
                "lease6-get-all": _leases_per_subnet(
                    {
                        30: [
                            complete_lease(
                                {
                                    "subnet-id": 30,
                                    "duid": "00:01:02:04",
                                    "ip-address": "2001:db8::21",
                                    "state": 0,
                                }
                            )
                        ]
                    }
                ),
            },
            version=6,
            subnets=_SUBNETS6,
        )

        self.assertIs(rows[0]["has_active_lease"], True)
        self.assertIn("q=2001%3Adb8%3A%3A21", rows[0]["lease_url"])
