# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from ipaddress import ip_address, ip_network

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from ipam.models import IPAddress

from netbox_kea.ipam_reconciliation import claim
from netbox_kea.reservations import (
    GlobalReservationScope,
    InSubnetReservationScope,
    IPv4Reservation,
    IPv6Reservation,
    ReservationIdentity,
    ReservationSynchronizationState,
)
from netbox_kea.subnet_catalogue import SubnetIdentity, for_synchronization
from netbox_kea.sync import reservation_synchronization_state

from .kea_stub import _catalogue_responses_for_subnets, stub_kea
from .utils import _make_db_server, plugins_config


@override_settings(PLUGINS_CONFIG=plugins_config())
class TestTypedReservationSynchronization(TestCase):
    def setUp(self):
        self.server = _make_db_server()

    def _claim(self, reservation):
        subnets = {4: [{"id": 20, "subnet": "198.18.0.0/24"}], 6: [{"id": 30, "subnet": "2001:db8::/64"}]}
        with stub_kea(_catalogue_responses_for_subnets(reservation.family, subnets[reservation.family])):
            catalogue = for_synchronization(self.server, reservation.family)
        return claim(self.server, reservation.family, [reservation], force=False, catalogue=catalogue)

    def test_synchronizes_every_ipv6_address_as_one_result(self):
        reservation = IPv6Reservation(
            scope=InSubnetReservationScope(SubnetIdentity(30, ip_network("2001:db8::/64"))),
            identity=ReservationIdentity("duid", "00:01:02:03"),
            addresses=(ip_address("2001:db8::20"), ip_address("2001:db8::21")),
            delegated_prefixes=(ip_network("2001:db8:100::/56"),),
            hostname="multi.example.invalid",
        )

        result = self._claim(reservation)

        self.assertEqual(
            reservation_synchronization_state(reservation, result.synchronized_addresses).label, "Synchronized"
        )
        self.assertEqual(
            (
                reservation_synchronization_state(reservation, result.synchronized_addresses).synchronized,
                reservation_synchronization_state(reservation, result.synchronized_addresses).total,
            ),
            (2, 2),
        )
        self.assertEqual(sum(row.outcome == "created" for row in result.addresses.values()), 2)
        stored = [str(address) for address in IPAddress.objects.order_by("pk").values_list("address", flat=True)]
        self.assertEqual(stored, ["2001:db8::20/64", "2001:db8::21/64"])

    def test_claim_outcomes_supply_a_badge_without_an_extra_database_query(self):
        reservation = IPv6Reservation(
            scope=InSubnetReservationScope(SubnetIdentity(30, ip_network("2001:db8::/64"))),
            identity=ReservationIdentity("duid", "00:01:02:03"),
            addresses=(ip_address("2001:db8::20"),),
            delegated_prefixes=(),
        )
        result = self._claim(reservation)
        with CaptureQueriesContext(connection) as outcome_read:
            state = reservation_synchronization_state(reservation, result.synchronized_addresses)
        self.assertEqual(state.label, "Synchronized")
        self.assertEqual(outcome_read.captured_queries, [])

    def test_reports_partial_state_for_one_of_two_managed_addresses(self):
        reservation = IPv6Reservation(
            scope=InSubnetReservationScope(SubnetIdentity(30, ip_network("2001:db8::/64"))),
            identity=ReservationIdentity("duid", "00:01:02:03"),
            addresses=(ip_address("2001:db8::20"), ip_address("2001:db8::21")),
            delegated_prefixes=(),
        )
        state = reservation_synchronization_state(reservation, frozenset({"2001:db8::20"}))

        self.assertEqual(state.label, "Partially Synchronized")
        self.assertEqual((state.synchronized, state.total), (1, 2))

    def test_global_and_addressless_reservations_are_not_applicable(self):
        global_reservation = IPv4Reservation(
            scope=GlobalReservationScope(),
            identity=ReservationIdentity("flex-id", "global-class"),
            addresses=(ip_address("198.18.0.20"),),
        )
        addressless = IPv4Reservation(
            scope=InSubnetReservationScope(SubnetIdentity(20, ip_network("198.18.0.0/24"))),
            identity=ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:ff"),
            addresses=(),
        )

        global_result = self._claim(global_reservation)
        addressless_result = self._claim(addressless)

        self.assertEqual(
            reservation_synchronization_state(global_reservation, global_result.synchronized_addresses).label,
            "Not Applicable",
        )
        self.assertIn(
            "Global", reservation_synchronization_state(global_reservation, global_result.synchronized_addresses).reason
        )
        self.assertEqual(
            reservation_synchronization_state(addressless, addressless_result.synchronized_addresses).label,
            "Not Applicable",
        )
        self.assertIn(
            "allocation address",
            reservation_synchronization_state(addressless, addressless_result.synchronized_addresses).reason,
        )
        self.assertFalse(IPAddress.objects.exists())

    def test_state_codes_stay_stable_for_every_label(self):
        cases = (
            (ReservationSynchronizationState.from_counts(2, 2), "synchronized"),
            (ReservationSynchronizationState.from_counts(1, 2), "partially-synchronized"),
            (ReservationSynchronizationState.from_counts(0, 2), "not-synchronized"),
            (ReservationSynchronizationState.not_applicable("no address"), "not-applicable"),
            (ReservationSynchronizationState.unknown(1, "unreadable"), "unknown"),
        )

        for state, expected_code in cases:
            with self.subTest(label=state.label):
                self.assertEqual(state.code, expected_code)
