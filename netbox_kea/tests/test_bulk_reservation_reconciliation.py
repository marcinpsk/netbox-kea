# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Bulk Reservation Sync uses real ownership, with only the Kea HTTP boundary stubbed."""

from ipaddress import ip_interface

import requests
from django.contrib import messages
from django.db import connection
from django.urls import reverse
from ipam.models import VRF
from ipam.models import IPAddress as NbIP

from netbox_kea.ipam_reconciliation import ReservationPhase, reconcile
from netbox_kea.models import IPAMOwnershipLink, next_confirmation_number
from netbox_kea.subnet_catalogue import for_synchronization

from .kea_stub import _catalogue_responses, _res_page, queued, stub_kea
from .test_sync_views import _make_server, _SyncViewBase
from .utils import lease_phase, plugins_config


def _reservation(address="198.18.0.10", hostname="", *, subnet_id=1):
    return {"subnet-id": subnet_id, "flex-id": address, "ip-address": address, "hostname": hostname}


def _responses(hosts=()):
    return {**_catalogue_responses(4, 1, "198.18.0.0/24"), "reservation-get-page": _res_page(list(hosts))}


class TestBulkReservationOwnership(_SyncViewBase):
    def _url(self, family=4):
        return reverse(f"plugins:netbox_kea:server_reservation{family}_bulk_sync", args=[self.server.pk])

    def _post(self, responses, family=4):
        with stub_kea(responses):
            response = self.client.post(self._url(family))
        self.assertEqual(response.status_code, 302)
        return " ".join(str(message) for message in messages.get_messages(response.wsgi_request))

    def _owned(self, address="198.18.0.10/24", *, sources=("lease", "reservation"), server=None, **fields):
        ip = NbIP.objects.create(
            address=address, status="active", description="[kea-sync: lease + reservation]", **fields
        )
        for source in sources:
            self._link(ip, source, server=server)
        return ip

    def _link(self, ip, source, *, server=None, family=4):
        return IPAMOwnershipLink.objects.create(
            server=server or self.server,
            family=family,
            source=source,
            ip_address=ip,
            facts={"hostname": "", "prefix_length": ip_interface(str(ip.address)).network.prefixlen},
            confirmation=next_confirmation_number(),
        )

    def test_summary_counts_created_updated_conflicted_and_cleaned_addresses(self):
        stale = self._owned()
        updated = self._owned("198.18.0.11/24", dns_name="old.example.invalid")
        foreign = NbIP.objects.create(address="198.18.0.12/24", description="Operator address")
        summary = self._post(
            _responses(
                [
                    _reservation("198.18.0.11", "new.example.invalid"),
                    _reservation("198.18.0.12"),
                    _reservation("198.18.0.13"),
                ]
            )
        )
        self.assertIn("1 created, 2 updated, 1 conflicts skipped, 1 stale links cleaned, 0 errors", summary)
        stale.refresh_from_db()
        updated.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(stale.status, "dhcp")
        self.assertEqual(updated.dns_name, "new.example.invalid")
        self.assertEqual(foreign.description, "Operator address")
        self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=foreign).exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address__address__net_host="198.18.0.13").exists())

    def test_bulk_preserves_last_link_until_both_phases_complete(self):
        for index, mode in enumerate(("remove", "deprecate", "none"), start=1):
            with self.subTest(mode=mode), self.settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup=mode)):
                ip = self._owned(f"198.18.0.{index}/24", sources=("reservation",))
                link = IPAMOwnershipLink.objects.get(ip_address=ip)
                summary = self._post(_responses())
                ip.refresh_from_db()
                link.refresh_from_db()
                self.assertEqual(ip.status, "active")
                self.assertIsNone(link.stale_mark)
                self.assertIn("0 stale links cleaned", summary)
                responses = {**_responses(), "lease4-get-page": {"result": 3, "text": "0 lease(s) found"}}
                with stub_kea(responses):
                    report = reconcile(
                        self.server,
                        4,
                        [lease_phase(self.server, 4, {1: 24}), ReservationPhase(for_synchronization(self.server, 4))],
                    )
                self.assertTrue(report.complete)
                if mode == "remove":
                    self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())
                    self.assertEqual(report.cleaned, 1)
                    self.assertEqual(report.removed, 1)
                elif mode == "deprecate":
                    ip.refresh_from_db()
                    link.refresh_from_db()
                    self.assertEqual(ip.status, "deprecated")
                    self.assertIsNotNone(link.stale_mark)
                    self.assertEqual(report.cleaned, 0)
                    self.assertEqual(report.deprecated, 1)
                else:
                    ip.refresh_from_db()
                    self.assertEqual(ip.status, "active")
                    self.assertFalse(IPAMOwnershipLink.objects.filter(pk=link.pk).exists())
                    self.assertEqual(report.cleaned, 1)

    def test_another_server_link_does_not_authorize_removing_this_servers_last_link(self):
        ip = self._owned(sources=("reservation",))
        other = _make_server(name="other-owner")
        self._link(ip, "lease", server=other)
        summary = self._post(_responses())
        self.assertEqual(IPAMOwnershipLink.objects.filter(ip_address=ip).count(), 2)
        ip.refresh_from_db()
        self.assertEqual(ip.status, "active")
        self.assertIn("0 stale links cleaned", summary)

    def test_a_confirmation_during_the_snapshot_read_survives_cleanup(self):
        ip = self._owned()
        link = IPAMOwnershipLink.objects.get(ip_address=ip, source="reservation")

        def confirm_during_read(body):
            link.confirmation = next_confirmation_number()
            link.save(update_fields=["confirmation"])
            return _res_page([])

        summary = self._post({**_responses(), "reservation-get-page": confirm_during_read})
        self.assertTrue(IPAMOwnershipLink.objects.filter(pk=link.pk).exists())
        self.assertIn("0 stale links cleaned", summary)

    def test_incomplete_snapshot_keeps_stale_links_and_synchronizes_valid_records(self):
        stale = self._owned()
        responses = _responses()
        responses["reservation-get-page"] = queued(
            _res_page(
                [
                    _reservation("198.18.0.11"),
                    *[{"subnet-id": 0, "flex-id": f"addressless-{index}"} for index in range(99)],
                ],
                next_from=1,
                next_source=1,
            ),
            requests.ConnectionError("private transport detail"),
        )
        summary = self._post(responses)
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address=stale, source="reservation").exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address__address__net_host="198.18.0.11").exists())
        self.assertIn("1 created", summary)
        self.assertIn("cleanup skipped", summary)
        self.assertIn("0 stale links cleaned", summary)
        self.assertNotIn("private transport detail", summary)

    def test_failed_row_rolls_back_preserves_cleanup_and_allows_the_next_record(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE ipam_ipaddress ADD CONSTRAINT reject_test_hostname "
                "CHECK (dns_name <> 'rejected.example.invalid')"
            )
        stale = self._owned()
        summary = self._post(
            _responses(
                [
                    _reservation("198.18.0.11", "rejected.example.invalid"),
                    _reservation("198.18.0.12", "accepted.example.invalid"),
                ]
            )
        )
        self.assertFalse(NbIP.objects.filter(address__net_host="198.18.0.11").exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address__address__net_host="198.18.0.12").exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address=stale, source="reservation").exists())
        self.assertIn("1 created", summary)
        self.assertIn("1 errors", summary)
        self.assertIn("cleanup skipped", summary)

    def test_unavailable_catalogue_preserves_cleanup_and_reports_incompleteness(self):
        stale = self._owned()
        summary = self._post(
            {
                "config-get": requests.ConnectionError("private detail"),
                "subnet4-list": requests.ConnectionError("private detail"),
            }
        )
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address=stale, source="reservation").exists())
        self.assertIn("1 errors", summary)
        self.assertIn("cleanup skipped", summary)
        self.assertNotIn("private detail", summary)

    def test_ipv6_sync_creates_all_addresses_in_the_server_vrf(self):
        vrf = VRF.objects.create(name="Reservation VRF")
        self.server.sync_vrf = vrf
        self.server.save()
        foreign = NbIP.objects.create(address="2001:db8::10/64", description="Global operator address")
        hosts = [{"subnet-id": 1, "duid": "00:01:aa:bb", "ip-addresses": ["2001:db8::10", "2001:db8::11"]}]
        summary = self._post(
            {**_catalogue_responses(6, 1, "2001:db8::/64"), "reservation-get-page": _res_page(hosts)}, family=6
        )
        self.assertIn("2 created, 0 updated, 0 conflicts skipped, 0 stale links cleaned", summary)
        self.assertEqual(IPAMOwnershipLink.objects.filter(server=self.server, family=6, ip_address__vrf=vrf).count(), 2)
        foreign.refresh_from_db()
        self.assertEqual(foreign.description, "Global operator address")

    def test_global_reservation_confirms_an_existing_address_without_changing_its_facts(self):
        ip = self._owned(sources=("lease",))
        summary = self._post(_responses([_reservation(hostname="ignored.example.invalid", subnet_id=0)]))
        link = IPAMOwnershipLink.objects.get(ip_address=ip, source="reservation")
        self.assertIsNone(link.facts)
        ip.refresh_from_db()
        self.assertEqual(ip.status, "active")
        self.assertEqual(ip.dns_name, "")
        self.assertIn("0 created, 0 updated", summary)

    def test_owner_disagreement_is_visible_and_keeps_existing_facts(self):
        ip = self._owned(dns_name="existing.example.invalid")
        other = _make_server(name="other-owner")
        link = self._link(ip, "reservation", server=other)
        link.facts = {"hostname": "existing.example.invalid", "prefix_length": 24}
        link.save(update_fields=["facts"])
        summary = self._post(_responses([_reservation(hostname="different.example.invalid")]))
        self.assertIn("1 owner disagreements", summary)
        self.assertIn("0 created, 0 updated, 0 conflicts skipped, 0 stale links cleaned", summary)
        ip.refresh_from_db()
        self.assertEqual(ip.dns_name, "existing.example.invalid")
        self.assertEqual(IPAMOwnershipLink.objects.filter(ip_address=ip).count(), 3)

    def test_cleanup_conflict_preserves_the_link_and_does_not_count_it_as_cleaned(self):
        ip = self._owned()
        ip.description = "[kea-sync: pool]" + "x" * 184
        ip.save()
        summary = self._post(_responses())
        self.assertIn("1 conflicts skipped, 0 stale links cleaned", summary)
        self.assertEqual(IPAMOwnershipLink.objects.filter(ip_address=ip).count(), 2)
        ip.refresh_from_db()
        self.assertEqual(ip.description, "[kea-sync: pool]" + "x" * 184)

    def test_released_address_loses_links_but_is_not_counted_as_stale_cleanup(self):
        ip = self._owned()
        ip.description = "Operator address"
        ip.save()
        summary = self._post(_responses())
        self.assertIn("1 conflicts skipped, 0 stale links cleaned", summary)
        self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=ip).exists())
        ip.refresh_from_db()
        self.assertEqual(ip.description, "Operator address")
        self.assertEqual(ip.status, "active")
