# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IPAM Reconciliation (ADR 0006): the lease phase owns its IP addresses through links.

The tests use the real ORM and a real KeaClient, and stub only requests.Session.post.
"""

from __future__ import annotations

from contextlib import suppress

from core.exceptions import JobFailed
from django.db import connection
from django.test import TestCase, override_settings
from ipam.models import VRF
from ipam.models import IPAddress as NbIP

from netbox_kea.ipam_reconciliation import LeasePhase, SyncReport, reconcile
from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.models import CONFIRMATION_SEQUENCE, IPAMOwnershipLink, next_confirmation_number
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.test_jobs import _PLUGINS_CONFIG, _PLUGINS_CONFIG_CLEANUP, _lease_page, _make_job, _patch_kea
from netbox_kea.tests.utils import _make_db_server

ADDRESS = "10.0.0.5"


def _config(mode: str) -> dict:
    return {"netbox_kea": {**_PLUGINS_CONFIG["netbox_kea"], "stale_ip_cleanup": mode}}


def _server(name: str, **fields):
    return _make_db_server(name=name, ca_url=f"https://{name}.example.com", dhcp6=False, **fields)


def _lease(address: str = ADDRESS, hostname: str = "", **fields) -> dict:
    return {"ip-address": address, "hostname": hostname, "subnet-id": 1, "valid-lft": 3600, "state": 0, **fields}


def _phase(reservation_addresses: frozenset[str] | None = None) -> LeasePhase:
    return LeasePhase(max_leases=None, subnet_prefix_lengths={1: 24}, reservation_addresses=reservation_addresses)


def _reconcile(server, leases: list[dict]) -> SyncReport:
    """Reconcile the DHCPv4 lease phase of *server* against a Kea that reports *leases*."""
    with stub_kea({"lease4-get-page": _lease_page(leases)}):
        return reconcile(server, 4, [_phase()])


def _row(address: str = ADDRESS) -> NbIP:
    return NbIP.objects.get(address__net_host=address)


def _links(ip: NbIP) -> dict[str, IPAMOwnershipLink]:
    return {link.server.name: link for link in IPAMOwnershipLink.objects.filter(ip_address=ip).select_related("server")}


def _run_job(server, leases: list[dict]) -> dict:
    """Run the sync job for *server* against a Kea that reports *leases*, and return its summary entry."""
    job = _make_job()
    with _patch_kea(leases4=leases), suppress(JobFailed):
        KeaIpamSyncJob(job).run(server_pk=server.pk)
    return job.data["summary"][0]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
class JobLeasePhaseOwnershipTest(TestCase):
    """The job's lease phase: cleanup and lookup stay inside the Server's ownership and sync_vrf."""

    def test_one_servers_run_keeps_the_row_of_another_server_with_the_same_hostname(self):
        first, second = _server("first"), _server("second")

        _run_job(first, [_lease("10.0.0.1", "host-x")])
        _run_job(second, [_lease("10.0.0.2", "host-x")])
        _run_job(first, [_lease("10.0.0.1", "host-x")])

        rows = NbIP.objects.filter(dns_name="host-x").order_by("address")
        self.assertEqual([str(row.address.ip) for row in rows], ["10.0.0.1", "10.0.0.2"])

    def test_two_servers_in_two_vrfs_each_own_a_row_in_their_sync_vrf(self):
        vrf_a, vrf_b = VRF.objects.create(name="vrf-a"), VRF.objects.create(name="vrf-b")
        first, second = _server("first", sync_vrf=vrf_a), _server("second", sync_vrf=vrf_b)

        _run_job(first, [_lease("10.0.0.1", "host-a")])
        _run_job(second, [_lease("10.0.0.1", "host-b")])

        rows = {row.vrf_id: row for row in NbIP.objects.filter(address__net_host="10.0.0.1")}
        self.assertEqual(set(rows), {vrf_a.pk, vrf_b.pk})
        self.assertEqual(rows[vrf_a.pk].dns_name, "host-a")
        self.assertEqual(rows[vrf_b.pk].dns_name, "host-b")
        for server, vrf in ((first, vrf_a), (second, vrf_b)):
            self.assertEqual(
                list(IPAMOwnershipLink.objects.filter(ip_address=rows[vrf.pk]).values_list("server", flat=True)),
                [server.pk],
            )

    def test_the_summary_counts_owner_disagreements(self):
        first, second = _server("first"), _server("second")
        _run_job(first, [_lease(hostname="host-a")])

        summary = _run_job(second, [_lease(hostname="host-b")])

        self.assertEqual(summary["disagreements"], 1)
        self.assertEqual(summary["errors"], 0)


@override_settings(PLUGINS_CONFIG=_config("remove"))
class LeasePhaseOwnersTest(TestCase):
    """Several Servers own one object; each run compares its facts with the other owners' facts."""

    def test_two_servers_that_report_one_lease_share_one_object_until_both_drop_it(self):
        first, second = _server("first"), _server("second")
        _reconcile(first, [_lease(hostname="host")])
        _reconcile(second, [_lease(hostname="host")])
        ip = _row()
        self.assertEqual(set(_links(ip)), {"first", "second"})

        dropped_once = _reconcile(first, [])

        self.assertEqual(_row().pk, ip.pk)
        self.assertEqual(set(_links(ip)), {"second"})
        self.assertEqual(dropped_once.removed, 0)

        dropped_twice = _reconcile(second, [])

        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())
        self.assertEqual(dropped_twice.removed, 1)

    def test_owners_with_different_facts_keep_the_object_facts_until_the_disagreeing_link_goes(self):
        first, second = _server("first"), _server("second")
        _reconcile(first, [_lease(hostname="host-a")])

        disagreeing = _reconcile(second, [_lease(hostname="host-b")])

        ip = _row()
        self.assertEqual(ip.dns_name, "host-a")
        self.assertEqual(disagreeing.disagreements, {ADDRESS})
        links = _links(ip)
        self.assertEqual(links["first"].facts, {"hostname": "host-a", "prefix_length": 24})
        self.assertEqual(links["second"].facts, {"hostname": "host-b", "prefix_length": 24})

        _reconcile(first, [])
        applied = _reconcile(second, [_lease(hostname="host-b")])

        self.assertEqual(_row().dns_name, "host-b")
        self.assertEqual(applied.disagreements, set())
        self.assertEqual(applied.updated, 1)

    def test_a_link_without_facts_takes_part_in_no_fact_comparison(self):
        first, second = _server("first"), _server("second")
        _reconcile(first, [_lease(hostname="host-a")])
        # One phase that reports the address twice with different facts keeps a link without facts.
        _reconcile(second, [_lease(hostname="host-b"), _lease(hostname="host-c")])
        self.assertIsNone(_links(_row())["second"].facts)

        agreeing = _reconcile(first, [_lease(hostname="host-a2")])

        self.assertEqual(agreeing.disagreements, set())
        self.assertEqual(_row().dns_name, "host-a2")

    def test_the_cutoff_and_each_confirmation_come_from_the_confirmation_sequence(self):
        server = _server("owner")
        before = next_confirmation_number()
        seen = {}

        def lease_page(body):
            with connection.cursor() as cursor:
                cursor.execute("SELECT currval(%s)", [CONFIRMATION_SEQUENCE])
                (seen["cutoff"],) = cursor.fetchone()
            return _lease_page([_lease()])

        with stub_kea({"lease4-get-page": lease_page}):
            reconcile(server, 4, [_phase()])

        self.assertLess(before, seen["cutoff"])
        self.assertLess(seen["cutoff"], _links(_row())["owner"].confirmation)


class LeasePhaseStaleTest(TestCase):
    """A complete lease phase removes its own stale links; the last link follows stale_ip_cleanup."""

    def setUp(self):
        self.server = _server("owner")

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_an_expired_lease_is_removed_in_remove_mode(self):
        _reconcile(self.server, [_lease(hostname="host")])
        ip = _row()

        report = _reconcile(self.server, [])

        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())
        self.assertEqual(report.removed, 1)

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_a_hostless_lease_is_cleaned_up_too(self):
        _reconcile(self.server, [_lease()])
        ip = _row()
        self.assertEqual(ip.dns_name, "")

        _reconcile(self.server, [])

        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())

    @override_settings(PLUGINS_CONFIG=_config("none"))
    def test_an_expired_lease_keeps_its_row_in_none_mode(self):
        _reconcile(self.server, [_lease(hostname="host")])

        _reconcile(self.server, [])

        self.assertEqual(_row().status, "dhcp")
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_an_incomplete_phase_keeps_its_stale_links(self):
        _reconcile(self.server, [_lease(hostname="host")])
        ip = _row()

        with stub_kea({"lease4-get-page": _lease_page([_lease("10.0.0.6"), _lease("10.0.0.7")])}):
            report = reconcile(self.server, 4, [LeasePhase(1, {1: 24}, None)])

        self.assertFalse(report.complete)
        self.assertTrue(NbIP.objects.filter(pk=ip.pk).exists())
        self.assertEqual(set(_links(ip)), {"owner"})


@override_settings(PLUGINS_CONFIG=_config("deprecate"))
class LeasePhaseDeprecateTest(TestCase):
    """In deprecate mode the last link stays and is marked stale."""

    def setUp(self):
        self.first, self.second = _server("first"), _server("second")
        _reconcile(self.first, [_lease(hostname="host")])
        self.report = _reconcile(self.first, [])
        self.ip = _row()

    def _mark(self) -> int | None:
        return _links(self.ip)["first"].stale_mark

    def test_the_last_link_stays_marked_stale_until_the_owner_reports_the_lease_again(self):
        self.assertEqual(self.ip.status, "deprecated")
        self.assertIsNotNone(self._mark())
        self.assertEqual(self.report.deprecated, 1)

        still_stale = _reconcile(self.first, [])

        self.assertEqual(_row().status, "deprecated")
        self.assertIsNotNone(self._mark())
        self.assertEqual(still_stale.deprecated, 0)

        _reconcile(self.first, [_lease(hostname="host")])

        self.assertEqual(_row().status, "dhcp")
        self.assertIsNone(self._mark())

    def test_another_server_that_links_the_object_drops_the_stale_link_and_sets_the_status(self):
        _reconcile(self.second, [_lease(hostname="host-b")])

        self.assertEqual(set(_links(self.ip)), {"second"})
        self.assertEqual(_row().status, "dhcp")
        self.assertEqual(_row().dns_name, "host-b")

    def test_a_confirmation_without_an_applied_report_keeps_the_mark_and_the_status(self):
        mark = self._mark()

        report = _reconcile(self.first, [_lease(hostname="host-a"), _lease(hostname="host-c")])

        link = _links(self.ip)["first"]
        self.assertEqual(report.disagreements, {ADDRESS})
        self.assertEqual(link.stale_mark, mark)
        self.assertGreater(link.confirmation, mark)
        self.assertEqual(_row().status, "deprecated")
        self.assertEqual(_row().dns_name, "host")

    def test_a_stale_link_that_its_owner_confirmed_after_the_mark_stays_when_another_server_links_the_object(self):
        _reconcile(self.first, [_lease(hostname="host-a"), _lease(hostname="host-c")])

        _reconcile(self.second, [_lease(hostname="host-b")])

        links = _links(self.ip)
        self.assertEqual(set(links), {"first", "second"})
        self.assertIsNotNone(links["first"].stale_mark)
        self.assertEqual(_row().status, "dhcp")

    def test_an_owner_disagreement_keeps_the_mark_of_a_stale_link(self):
        _reconcile(self.first, [_lease(hostname="host-a"), _lease(hostname="host-c")])
        _reconcile(self.second, [_lease(hostname="host-b")])
        mark = self._mark()

        report = _reconcile(self.first, [_lease(hostname="host-a")])

        self.assertEqual(report.disagreements, {ADDRESS})
        self.assertEqual(self._mark(), mark)
        self.assertEqual((_row().dns_name, _row().status), ("host-b", "dhcp"))

        agreeing = _reconcile(self.first, [_lease(hostname="host-b")])

        self.assertEqual(agreeing.disagreements, set())
        self.assertIsNone(self._mark())


class LeasePhaseReleaseTest(TestCase):
    """The marker at the start of the description is the ownership fact that an operator can remove."""

    def setUp(self):
        self.server = _server("owner")

    def _curate(self, description: str) -> NbIP:
        ip = _row()
        ip.description = description
        ip.save()
        return ip

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_removing_the_marker_releases_a_reported_row(self):
        other = _server("other")
        _reconcile(self.server, [_lease(hostname="host")])
        _reconcile(other, [_lease(hostname="host")])
        ip = self._curate("Printer on floor 2")

        report = _reconcile(self.server, [_lease(hostname="renamed")])

        self.assertEqual(report.conflicts, {ADDRESS})
        self.assertEqual(_links(ip), {})
        row = _row()
        self.assertEqual((row.description, row.dns_name, row.status), ("Printer on floor 2", "host", "dhcp"))

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_removing_the_marker_releases_the_last_link_without_removing_the_row(self):
        _reconcile(self.server, [_lease(hostname="host")])
        ip = self._curate("Printer on floor 2")

        report = _reconcile(self.server, [])

        self.assertEqual(report.conflicts, {ADDRESS})
        self.assertEqual(report.removed, 0)
        self.assertEqual(_links(ip), {})
        row = _row()
        self.assertEqual((row.pk, row.description, row.status), (ip.pk, "Printer on floor 2", "dhcp"))

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_a_note_after_the_marker_does_not_release_the_row(self):
        _reconcile(self.server, [_lease(hostname="host")])
        ip = self._curate("Synced from Kea DHCP lease, printer on floor 2")

        report = _reconcile(self.server, [_lease(hostname="host")])

        self.assertEqual(report.conflicts, set())
        self.assertEqual(set(_links(ip)), {"owner"})
        self._curate("Synced from Kea DHCP lease, printer on floor 2")

        _reconcile(self.server, [])

        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_an_unlinked_row_with_a_blank_or_foreign_description_is_a_conflict(self):
        for description in ("", "Router loopback"):
            with self.subTest(description=description):
                NbIP.objects.all().delete()
                ip = NbIP.objects.create(address=f"{ADDRESS}/32", status="active", description=description)

                report = _reconcile(self.server, [_lease(hostname="host")])

                self.assertEqual(report.conflicts, {ADDRESS})
                self.assertEqual(_links(ip), {})
                row = _row()
                self.assertEqual(
                    (str(row.address), row.status, row.dns_name, row.description),
                    (f"{ADDRESS}/32", "active", "", description),
                )

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_an_unlinked_marker_row_in_the_sync_vrf_is_linked_in_place(self):
        ip = NbIP.objects.create(address=f"{ADDRESS}/32", status="dhcp", description="Synced from Kea DHCP lease")

        report = _reconcile(self.server, [_lease(hostname="host")])

        row = _row()
        self.assertEqual((row.pk, str(row.address), row.dns_name), (ip.pk, f"{ADDRESS}/24", "host"))
        self.assertEqual(set(_links(ip)), {"owner"})
        self.assertEqual((report.created, report.updated), (0, 1))

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_a_row_outside_the_sync_vrf_is_neither_linked_nor_changed(self):
        self.server.sync_vrf = VRF.objects.create(name="sync")
        self.server.save()
        other_vrf = NbIP.objects.create(
            address=f"{ADDRESS}/32", status="dhcp", description="Synced from Kea DHCP lease"
        )

        _reconcile(self.server, [_lease(hostname="host")])

        own = NbIP.objects.get(address__net_host=ADDRESS, vrf=self.server.sync_vrf)
        self.assertEqual(set(_links(own)), {"owner"})
        other_vrf.refresh_from_db()
        self.assertEqual((str(other_vrf.address), other_vrf.dns_name), (f"{ADDRESS}/32", ""))
        self.assertEqual(_links(other_vrf), {})


@override_settings(PLUGINS_CONFIG=_config("remove"))
class LeasePhaseRowFailureTest(TestCase):
    """A row failure fails only that row, and a phase with a failed row keeps its stale links."""

    def test_a_database_error_on_one_row_does_not_abort_the_rest_of_the_phase(self):
        server = _server("owner")
        _reconcile(server, [_lease("10.0.0.9", "stale")])

        # dns_name is a varchar(255): PostgreSQL refuses the longer name of the first lease.
        report = _reconcile(server, [_lease("10.0.0.1", "h" * 300), _lease("10.0.0.2", "ok")])

        self.assertEqual(report.errors, 1)
        self.assertFalse(report.complete)
        self.assertFalse(NbIP.objects.filter(address__net_host="10.0.0.1").exists())
        self.assertEqual(_row("10.0.0.2").dns_name, "ok")
        self.assertEqual(set(_links(_row("10.0.0.9"))), {"owner"})

    def test_a_malformed_hostname_fails_only_that_record(self):
        server = _server("owner")

        report = _reconcile(server, [_lease("10.0.0.1", ["not", "a", "name"]), _lease("10.0.0.2", "ok")])

        self.assertEqual((report.errors, report.complete, report.created), (1, False, 1))
        self.assertEqual([lease["ip-address"] for lease in report.lease_records], ["10.0.0.2"])
