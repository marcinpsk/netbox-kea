# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from ipaddress import ip_address, ip_network

from django.test import TestCase
from ipam.models import IPAddress

from netbox_kea.reservations import (
    GlobalReservationScope,
    InSubnetReservationScope,
    IPv4Reservation,
    IPv6Reservation,
    ReservationIdentity,
)
from netbox_kea.subnet_catalogue import SubnetIdentity
from netbox_kea.sync import reservation_synchronization_state, sync_reservation_to_netbox


class TestTypedReservationSynchronization(TestCase):
    def test_synchronizes_every_ipv6_address_as_one_result(self):
        reservation = IPv6Reservation(
            scope=InSubnetReservationScope(SubnetIdentity(30, ip_network("2001:db8::/64"))),
            identity=ReservationIdentity("duid", "00:01:02:03"),
            addresses=(ip_address("2001:db8::20"), ip_address("2001:db8::21")),
            delegated_prefixes=(ip_network("2001:db8:100::/56"),),
            hostname="multi.example.invalid",
        )

        result = sync_reservation_to_netbox(reservation, cleanup=False)

        self.assertEqual(result.state.label, "Synchronized")
        self.assertEqual((result.state.synchronized, result.state.total), (2, 2))
        self.assertEqual(result.created, 2)
        stored = [str(address) for address in IPAddress.objects.order_by("pk").values_list("address", flat=True)]
        self.assertEqual(stored, ["2001:db8::20/64", "2001:db8::21/64"])

    def test_reports_partial_state_for_one_of_two_managed_addresses(self):
        reservation = IPv6Reservation(
            scope=InSubnetReservationScope(SubnetIdentity(30, ip_network("2001:db8::/64"))),
            identity=ReservationIdentity("duid", "00:01:02:03"),
            addresses=(ip_address("2001:db8::20"), ip_address("2001:db8::21")),
            delegated_prefixes=(),
        )
        IPAddress.objects.create(
            address="2001:db8::20/64",
            status="reserved",
            description="Synced from Kea DHCP reservation",
        )

        state = reservation_synchronization_state(reservation)

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

        global_result = sync_reservation_to_netbox(global_reservation, cleanup=False)
        addressless_result = sync_reservation_to_netbox(addressless, cleanup=False)

        self.assertEqual(global_result.state.label, "Not Applicable")
        self.assertIn("Global", global_result.state.reason)
        self.assertEqual(addressless_result.state.label, "Not Applicable")
        self.assertIn("allocation address", addressless_result.state.reason)
        self.assertFalse(IPAddress.objects.exists())
