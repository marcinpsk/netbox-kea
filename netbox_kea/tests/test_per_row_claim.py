# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Per-row HTTP synchronization claims its reported rows without removing other addresses."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress

from netbox_kea.models import Server
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.utils import plugins_config


@override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
class PerRowLeaseCleanupTest(TestCase):
    def test_lease_sync_keeps_reserved_address_of_the_same_host(self):
        user = get_user_model().objects.create_superuser(username="claim-user", password="example-password")
        self.client.force_login(user)
        server = Server.objects.create(name="claim-server", ca_url="https://kea.example.com", dhcp4=True, dhcp6=False)
        reserved = IPAddress.objects.create(
            address="198.18.0.20/24",
            status="reserved",
            dns_name="host.example.com",
            description="Synced from Kea DHCP reservation",
        )
        before = IPAddress.objects.values().get(pk=reserved.pk)
        lease = {"ip-address": "198.18.0.10", "hostname": "host.example.com", "subnet-id": 1, "valid-lft": 3600}
        with stub_kea({"lease4-get": {"result": 0, "arguments": lease}}):
            response = self.client.post(
                reverse("plugins:netbox_kea:server_lease4_sync", args=[server.pk]), {"ip_address": "198.18.0.10"}
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(IPAddress.objects.filter(address__net_host="198.18.0.10").exists())
        self.assertTrue(IPAddress.objects.filter(pk=reserved.pk).exists(), "Per-row Sync deleted the reserved address")
        self.assertEqual(IPAddress.objects.values().get(pk=reserved.pk), before)


class ClaimOwnershipTest(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="claim-server", ca_url="https://kea.example.com")
        self.lease = {"ip-address": "198.18.0.10", "hostname": "host.example.com", "subnet-id": 1}

    def test_empty_claim_and_addressless_reservation_do_not_write(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink
        from netbox_kea.reservations import GlobalReservationScope, IPv4Reservation, ReservationIdentity

        reservation = IPv4Reservation(GlobalReservationScope(), ReservationIdentity("flex-id", "addressless"), ())
        for records in ([], [reservation]):
            result = claim(self.server, 4, records, force=True)
            self.assertEqual(result.addresses, {})
            self.assertIsNone(result.primary)
            self.assertEqual(result.synchronized_addresses, frozenset())
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_global_reservation_only_links_existing_marker_rows_without_facts(self):
        from ipaddress import ip_address

        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink
        from netbox_kea.reservations import GlobalReservationScope, IPv4Reservation, ReservationIdentity

        reservation = IPv4Reservation(
            GlobalReservationScope(), ReservationIdentity("flex-id", "global"), (ip_address("198.18.0.10"),)
        )
        result = claim(self.server, 4, [reservation], force=True)
        self.assertEqual(result.addresses["198.18.0.10"].outcome, "not-applicable")
        self.assertFalse(IPAddress.objects.exists())
        for description in ("Operator row", "[kea-sync: lease]"):
            with self.subTest(description=description):
                ip = IPAddress.objects.create(address="198.18.0.10/24", description=description)
                before = IPAddress.objects.values().get(pk=ip.pk)
                result = claim(self.server, 4, [reservation], force=True)
                self.assertEqual(result.addresses["198.18.0.10"].outcome, "not-applicable")
                self.assertIsNone(result.primary)
                self.assertEqual(IPAddress.objects.values().get(pk=ip.pk), before)
                if description == "Operator row":
                    self.assertFalse(IPAMOwnershipLink.objects.exists())
                else:
                    link = IPAMOwnershipLink.objects.get(ip_address=ip)
                    self.assertEqual((link.server_id, link.source), (self.server.pk, "reservation"))
                    self.assertIsNone(link.facts)
                ip.delete()

    def test_invalid_family_and_mismatched_reservation_reject_before_writes(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.reservations import GlobalReservationScope, IPv4Reservation, ReservationIdentity

        reservation = IPv4Reservation(GlobalReservationScope(), ReservationIdentity("flex-id", "addressless"), ())
        for family in (True, 5, 6):
            with self.subTest(family=family), self.assertRaisesMessage(ValueError, "family"):
                claim(self.server, family, [reservation], force=True)
        self.assertFalse(IPAddress.objects.exists())

    def test_same_call_disagreement_never_forces_a_foreign_row(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        ip = IPAddress.objects.create(address="198.18.0.10/24", description="Operator row")
        before = IPAddress.objects.values().get(pk=ip.pk)
        result = claim(self.server, 4, [self.lease, {**self.lease, "hostname": "other.example.com"}], force=True)
        self.assertEqual(result.addresses["198.18.0.10"].outcome, "disagreement")
        self.assertEqual(IPAddress.objects.values().get(pk=ip.pk), before)
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_blank_or_foreign_row_requires_force(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        for description in ("", "Operator note"):
            with self.subTest(description=description):
                ip = IPAddress.objects.create(address="198.18.0.10/24", description=description)
                before = IPAddress.objects.values().get(pk=ip.pk)
                refused = claim(self.server, 4, [self.lease], force=False)
                self.assertEqual(refused.addresses["198.18.0.10"].outcome, "conflict")
                self.assertEqual(IPAddress.objects.values().get(pk=ip.pk), before)
                self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=ip).exists())
                accepted = claim(self.server, 4, [self.lease], force=True)
                self.assertEqual(accepted.addresses["198.18.0.10"].outcome, "updated")
                self.assertEqual(accepted.primary.pk, ip.pk)
                ip.refresh_from_db()
                self.assertEqual(ip.description, "[kea-sync: lease]")
                link = IPAMOwnershipLink.objects.get(ip_address=ip)
                self.assertEqual((link.server_id, link.family, link.source), (self.server.pk, 4, "lease"))
                self.assertEqual(link.facts, {"hostname": "host.example.com", "prefix_length": 32})
                ip.delete()

    def test_longest_prefix_is_scoped_to_the_server_vrf_and_includes_host_prefixes(self):
        from ipam.models import VRF, Prefix

        from netbox_kea.ipam_reconciliation import claim

        vrf = VRF.objects.create(name="claim-vrf")
        self.server.sync_vrf = vrf
        self.server.save()
        Prefix.objects.create(prefix="198.18.0.10/32")
        Prefix.objects.create(prefix="198.18.0.0/24", vrf=vrf)
        result = claim(self.server, 4, [self.lease], force=False)
        self.assertEqual(str(result.primary.address), "198.18.0.10/24")
        self.assertEqual(result.primary.vrf_id, vrf.pk)
        Prefix.objects.create(prefix="198.18.0.10/32", vrf=vrf)
        result = claim(self.server, 4, [self.lease], force=False)
        self.assertEqual(str(result.primary.address), "198.18.0.10/32")

    def test_wrong_family_rejects_the_whole_call_before_writes(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        with self.assertRaisesMessage(ValueError, "family"):
            claim(self.server, 4, [self.lease, {"ip-address": "2001:db8::10"}], force=True)
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_same_call_disagreement_does_not_create_and_keeps_existing_facts(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        conflicting = {**self.lease, "hostname": "other.example.com"}
        for force in (False, True):
            result = claim(self.server, 4, [self.lease, conflicting], force=force)
            self.assertEqual(result.addresses["198.18.0.10"].outcome, "disagreement")
            self.assertFalse(IPAddress.objects.exists())
        first = claim(self.server, 4, [self.lease], force=False)
        before = IPAddress.objects.values().get(pk=first.primary.pk)
        link = IPAMOwnershipLink.objects.get(ip_address=first.primary)
        facts = link.facts
        confirmation = link.confirmation
        result = claim(self.server, 4, [self.lease, conflicting], force=True)
        self.assertEqual(result.addresses["198.18.0.10"].outcome, "disagreement")
        self.assertEqual(IPAddress.objects.values().get(pk=first.primary.pk), before)
        link.refresh_from_db()
        self.assertEqual(link.facts, facts)
        self.assertGreater(link.confirmation, confirmation)
        link.delete()
        result = claim(self.server, 4, [self.lease, conflicting], force=False)
        self.assertEqual(result.addresses["198.18.0.10"].outcome, "disagreement")
        self.assertIsNone(IPAMOwnershipLink.objects.get(ip_address=first.primary).facts)
        self.assertEqual(IPAddress.objects.values().get(pk=first.primary.pk), before)

    def test_competing_server_disagreement_stores_report_without_changing_object(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        other = Server.objects.create(name="other-server", ca_url="https://other.example.com")
        first = claim(other, 4, [self.lease], force=False)
        before = IPAddress.objects.values().get(pk=first.primary.pk)
        result = claim(self.server, 4, [{**self.lease, "hostname": "other.example.com"}], force=True)
        self.assertEqual(result.addresses["198.18.0.10"].outcome, "disagreement")
        self.assertEqual(IPAddress.objects.values().get(pk=first.primary.pk), before)
        self.assertEqual(
            IPAMOwnershipLink.objects.get(server=self.server).facts,
            {"hostname": "other.example.com", "prefix_length": 32},
        )

    def test_owned_marker_note_is_preserved(self):
        from netbox_kea.ipam_reconciliation import claim

        ip = IPAddress.objects.create(address="198.18.0.10/24", description="[kea-sync: reservation] Operator note")
        result = claim(self.server, 4, [self.lease], force=True)
        self.assertEqual(result.primary.pk, ip.pk)
        ip.refresh_from_db()
        self.assertEqual(ip.description, "[kea-sync: lease] Operator note")

    def test_multi_address_reservation_reports_each_address_and_rejects_mixed_source(self):
        from ipaddress import ip_address, ip_network

        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink
        from netbox_kea.reservations import InSubnetReservationScope, IPv6Reservation, ReservationIdentity
        from netbox_kea.subnet_catalogue import SubnetIdentity

        reservation = IPv6Reservation(
            scope=InSubnetReservationScope(SubnetIdentity(30, ip_network("2001:db8::/64"))),
            identity=ReservationIdentity("duid", "00:01:02:03"),
            addresses=(ip_address("2001:db8::20"), ip_address("2001:db8::21")),
            delegated_prefixes=(),
            hostname="multi.example.com",
        )
        with self.assertRaisesMessage(ValueError, "one source"):
            claim(self.server, 6, [{"ip-address": "2001:db8::22"}, reservation], force=True)
        self.assertFalse(IPAddress.objects.exists())
        foreign = IPAddress.objects.create(address="2001:db8::20/64", description="Operator row")
        before = IPAddress.objects.values().get(pk=foreign.pk)
        result = claim(self.server, 6, [reservation], force=False)
        self.assertEqual(
            {address: row.outcome for address, row in result.addresses.items()},
            {"2001:db8::20": "conflict", "2001:db8::21": "created"},
        )
        self.assertEqual(result.synchronized_addresses, frozenset({"2001:db8::21"}))
        self.assertEqual(IPAddress.objects.values().get(pk=foreign.pk), before)
        self.assertEqual(set(IPAMOwnershipLink.objects.values_list("family", "source")), {(6, "reservation")})
        self.assertEqual(str(result.primary.address), "2001:db8::21/64")
