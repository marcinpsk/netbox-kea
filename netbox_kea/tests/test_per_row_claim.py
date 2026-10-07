# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Per-row HTTP synchronization claims its reported rows without removing other addresses."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress

from netbox_kea.models import Server
from netbox_kea.subnet_catalogue import for_synchronization
from netbox_kea.tests.kea_stub import (
    _catalogue_responses,
    _catalogue_responses_for_subnets,
    complete_lease,
    stub_kea,
    typed_lease,
)
from netbox_kea.tests.utils import plugins_config

_LEASE = {"ip-address": "198.18.0.10", "hostname": "host.example.com", "subnet-id": 1}


def _typed(**changes):
    """Return the typed Lease of the test lease with *changes* to its wire keys."""
    return typed_lease(complete_lease({**_LEASE, **changes}))


@override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
class PerRowLeaseCleanupTest(TestCase):
    def test_lease_sync_uses_the_same_kea_subnet_facts_as_reconciliation(self):
        from ipam.models import Prefix

        from netbox_kea.ipam_reconciliation import LeasePhase, reconcile
        from netbox_kea.models import IPAMOwnershipLink
        from netbox_kea.tests.test_jobs import _lease_page

        user = get_user_model().objects.create_superuser(username="claim-user", password="example-password")
        self.client.force_login(user)
        first = Server.objects.create(name="first-owner", ca_url="https://first.example.com", dhcp4=True, dhcp6=False)
        second = Server.objects.create(
            name="second-owner", ca_url="https://second.example.com", dhcp4=True, dhcp6=False
        )
        lease = complete_lease({"ip-address": "198.18.0.10", "hostname": "", "subnet-id": 1})
        phase = LeasePhase(max_leases=None, subnet_prefix_lengths={1: 24})
        with stub_kea({"lease4-get-page": _lease_page([lease])}):
            reconcile(second, 4, [phase])
        self.assertFalse(Prefix.objects.exists())
        with stub_kea({**_catalogue_responses(4, 1, "198.18.0.0/24"), "lease4-get": {"result": 0, "arguments": lease}}):
            response = self.client.post(
                reverse("plugins:netbox_kea:server_lease4_sync", args=[first.pk]), {"ip_address": "198.18.0.10"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IPAMOwnershipLink.objects.get(server=first).facts, {"hostname": "", "prefix_length": 24})
        self.assertNotContains(response, "Owner disagreement")
        self.assertContains(response, "198.18.0.10/24")
        with stub_kea({"lease4-get-page": _lease_page([{**lease, "hostname": "updated.example.com"}])}):
            report = reconcile(second, 4, [phase])
        self.assertFalse(report.disagreements)
        self.assertEqual(IPAddress.objects.get(address__net_host="198.18.0.10").dns_name, "updated.example.com")

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
        lease = complete_lease({"ip-address": "198.18.0.10", "hostname": "host.example.com", "subnet-id": 1})
        with stub_kea({**_catalogue_responses(4, 1, "198.18.0.0/24"), "lease4-get": {"result": 0, "arguments": lease}}):
            response = self.client.post(
                reverse("plugins:netbox_kea:server_lease4_sync", args=[server.pk]), {"ip_address": "198.18.0.10"}
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(IPAddress.objects.filter(address__net_host="198.18.0.10").exists())
        self.assertTrue(IPAddress.objects.filter(pk=reserved.pk).exists(), "Per-row Sync deleted the reserved address")
        self.assertEqual(IPAddress.objects.values().get(pk=reserved.pk), before)


@override_settings(PLUGINS_CONFIG=plugins_config())
class ClaimOwnershipTest(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="claim-server", ca_url="https://kea.example.com")
        self.lease = _typed()
        transport = stub_kea(_catalogue_responses(4, 1, "198.18.0.0/24"))
        self.kea = transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)

    def test_ambiguous_hardware_rolls_back_reservation_claim_and_keeps_other_rows(self):
        from dcim.models import MACAddress

        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink
        from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot

        hardware = "02:00:00:00:00:01"
        MACAddress.objects.create(mac_address=hardware)
        MACAddress.objects.create(mac_address=hardware)
        ip = IPAddress.objects.create(address="198.18.0.10/24", description="Operator address")
        before = IPAddress.objects.values().get(pk=ip.pk)
        observation = _reservation_snapshot(
            {"subnet4": [{"id": 1, "subnet": "198.18.0.0/24"}]},
            4,
            [
                {"subnet-id": 1, "hw-address": hardware, "ip-address": "198.18.0.10"},
                {"subnet-id": 1, "hw-address": "02:00:00:00:00:02", "ip-address": "198.18.0.11"},
            ],
        )

        result = claim(self.server, 4, observation.snapshot.records, force=True)

        self.assertEqual(result.addresses["198.18.0.10"].outcome, "error")
        self.assertEqual(IPAddress.objects.values().get(pk=ip.pk), before)
        self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=ip).exists())
        self.assertEqual(result.addresses["198.18.0.11"].outcome, "created")
        self.assertEqual(result.synchronized_addresses, frozenset({"198.18.0.11"}))
        self.assertEqual(str(IPAMOwnershipLink.objects.get().ip_address.address.ip), "198.18.0.11")

    def test_a_created_reservation_row_checks_its_mac_once_with_a_bounded_query(self):
        from dcim.models import MACAddress
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot

        observation = _reservation_snapshot(
            {"subnet4": [{"id": 1, "subnet": "198.18.0.0/24"}]},
            4,
            [{"subnet-id": 1, "hw-address": "02:00:00:00:00:03", "ip-address": "198.18.0.12"}],
        )
        table = f'FROM "{MACAddress._meta.db_table}"'

        with CaptureQueriesContext(connection) as queries:
            result = claim(self.server, 4, observation.snapshot.records, force=False)

        self.assertEqual(result.addresses["198.18.0.12"].outcome, "created")
        mac_reads = [q["sql"] for q in queries if table in q["sql"] and not q["sql"].startswith("INSERT")]
        self.assertEqual(sum("COUNT(" in sql for sql in mac_reads), 0, "use a bounded read, not a full count")
        self.assertEqual(sum(sql.endswith("LIMIT 2") for sql in mac_reads), 1, "check each MAC once per row")

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
        self.assertEqual(self.kea.commands(), [])

    def test_a_named_reservation_without_its_catalogue_is_refused_before_writes(self):
        from ipaddress import ip_address, ip_network

        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.reservations import InSubnetReservationScope, IPv4Reservation, ReservationIdentity
        from netbox_kea.subnet_catalogue import SubnetIdentity

        reservation = IPv4Reservation(
            InSubnetReservationScope(SubnetIdentity(1, ip_network("198.18.0.0/24"))),
            ReservationIdentity("flex-id", "named"),
            (ip_address("198.18.0.10"),),
            hostname="host",
        )
        with self.assertRaisesMessage(ValueError, "needs the catalogue"):
            claim(self.server, 4, [reservation], force=True)
        self.assertFalse(IPAddress.objects.exists())
        self.assertEqual(self.kea.commands(), [])

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
                expected = "conflict" if description == "Operator row" else "not-applicable"
                self.assertEqual(result.addresses["198.18.0.10"].outcome, expected)
                self.assertEqual(result.addresses["198.18.0.10"].ip, ip)
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
        result = claim(self.server, 4, [self.lease, _typed(hostname="other.example.com")], force=True)
        self.assertEqual(result.addresses["198.18.0.10"].outcome, "disagreement")
        self.assertEqual(IPAddress.objects.values().get(pk=ip.pk), before)
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_unavailable_catalogue_refuses_force_before_any_ownership_write(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink
        from netbox_kea.subnet_catalogue import CatalogueUnavailable

        ip = IPAddress.objects.create(address="198.18.0.10/24", description="Operator row")
        before = IPAddress.objects.values().get(pk=ip.pk)
        with stub_kea({"subnet4-list": {"result": 1}, "config-get": {"result": 1}}):
            with self.assertRaises(CatalogueUnavailable):
                claim(self.server, 4, [self.lease], force=True)
        self.assertEqual(IPAddress.objects.values().get(pk=ip.pk), before)
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_unknown_subnet_refuses_the_complete_claim_even_when_force_is_set(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        for subnets in ([], [{"id": 1, "subnet": "198.18.0.0/24"}]):
            with self.subTest(subnets=subnets), stub_kea(_catalogue_responses_for_subnets(4, subnets)):
                with self.assertRaisesMessage(ValueError, "Subnet ID"):
                    claim(self.server, 4, [self.lease, _typed(**{"subnet-id": 2})], force=True)
            self.assertFalse(IPAddress.objects.exists())
            self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_one_catalogue_read_supplies_all_lease_facts(self):
        from netbox_kea.ipam_reconciliation import claim

        result = claim(self.server, 4, [self.lease, _typed(**{"ip-address": "198.18.0.11"})], force=False)
        self.assertEqual(len(result.synchronized_addresses), 2)
        self.assertEqual(self.kea.commands().count("subnet4-list"), 1)
        self.assertEqual(self.kea.commands().count("config-get"), 1)

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
                expected_description = "[kea-sync: lease]" + (f" {description}" if description else "")
                self.assertEqual(ip.description, expected_description)
                link = IPAMOwnershipLink.objects.get(ip_address=ip)
                self.assertEqual((link.server_id, link.family, link.source), (self.server.pk, 4, "lease"))
                self.assertEqual(link.facts, {"hostname": "host.example.com", "prefix_length": 24})
                ip.delete()

    def test_kea_subnet_mask_ignores_netbox_prefixes_in_all_vrfs(self):
        from ipam.models import VRF, Prefix

        from netbox_kea.ipam_reconciliation import claim

        vrf = VRF.objects.create(name="claim-vrf")
        self.server.sync_vrf = vrf
        self.server.save()
        Prefix.objects.create(prefix="198.18.0.10/32")
        Prefix.objects.create(prefix="198.18.0.0/16", vrf=vrf)
        result = claim(self.server, 4, [self.lease], force=False)
        self.assertEqual(str(result.primary.address), "198.18.0.10/24")
        self.assertEqual(result.primary.vrf_id, vrf.pk)
        Prefix.objects.create(prefix="198.18.0.10/32", vrf=vrf)
        result = claim(self.server, 4, [self.lease], force=False)
        self.assertEqual(str(result.primary.address), "198.18.0.10/24")

    def test_wrong_family_rejects_the_whole_call_before_writes(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        with self.assertRaisesMessage(ValueError, "family"):
            claim(self.server, 4, [self.lease, _typed(**{"ip-address": "2001:db8::10"})], force=True)
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_same_call_disagreement_does_not_create_and_keeps_existing_facts(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        conflicting = _typed(hostname="other.example.com")
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
        result = claim(self.server, 4, [_typed(hostname="other.example.com")], force=True)
        self.assertEqual(result.addresses["198.18.0.10"].outcome, "disagreement")
        self.assertEqual(IPAddress.objects.values().get(pk=first.primary.pk), before)
        self.assertEqual(
            IPAMOwnershipLink.objects.get(server=self.server).facts,
            {"hostname": "other.example.com", "prefix_length": 24},
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
            claim(self.server, 6, [_typed(**{"ip-address": "2001:db8::22"}), reservation], force=True)
        self.assertFalse(IPAddress.objects.exists())
        foreign = IPAddress.objects.create(address="2001:db8::20/64", description="Operator row")
        before = IPAddress.objects.values().get(pk=foreign.pk)
        with stub_kea(_catalogue_responses_for_subnets(6, [{"id": 30, "subnet": "2001:db8::/64"}])):
            catalogue = for_synchronization(self.server, 6)
        result = claim(self.server, 6, [reservation], force=False, catalogue=catalogue)
        self.assertEqual(
            {address: row.outcome for address, row in result.addresses.items()},
            {"2001:db8::20": "conflict", "2001:db8::21": "created"},
        )
        self.assertEqual(result.synchronized_addresses, frozenset({"2001:db8::21"}))
        self.assertEqual(IPAddress.objects.values().get(pk=foreign.pk), before)
        self.assertEqual(set(IPAMOwnershipLink.objects.values_list("family", "source")), {(6, "reservation")})
        self.assertEqual(str(result.primary.address), "2001:db8::21/64")
        self.assertEqual(self.kea.commands(), [])
