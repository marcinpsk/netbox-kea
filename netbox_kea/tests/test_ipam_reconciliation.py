# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IPAM Reconciliation (ADR 0006): the lease and Reservation phases own their IP addresses through links.

The tests use the real ORM and a real KeaClient, and stub only requests.Session.post.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress

from core.exceptions import JobFailed
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from ipam.models import VRF
from ipam.models import IPAddress as NbIP

from netbox_kea import subnet_catalogue
from netbox_kea.ipam_reconciliation import LeasePhase, ReservationPhase, SyncReport, _lock_identity, reconcile
from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.models import (
    CONFIRMATION_SEQUENCE,
    IPAMOwnershipLink,
    IPAMOwnershipSource,
    next_confirmation_number,
)
from netbox_kea.sync import _cleanup_stale_ips
from netbox_kea.tests.kea_stub import _catalogue_responses_for_subnets, _res_page, stub_kea
from netbox_kea.tests.test_jobs import _PLUGINS_CONFIG_CLEANUP, _lease_page, _make_job, _patch_kea
from netbox_kea.tests.utils import _make_db_server, plugins_config

ADDRESS = "10.0.0.5"
SUBNET = {"id": 1, "subnet": "10.0.0.0/24"}
BOTH = ("lease", "reservation")


def _config(mode: str) -> dict:
    return plugins_config(stale_ip_cleanup=mode)


def _server(name: str, **fields):
    return _make_db_server(name=name, ca_url=f"https://{name}.example.com", dhcp6=False, **fields)


def _lease(address: str = ADDRESS, hostname: str = "", **fields) -> dict:
    return {"ip-address": address, "hostname": hostname, "subnet-id": 1, "valid-lft": 3600, "state": 0, **fields}


def _reservation(address: str = ADDRESS, hostname: str = "", *, subnet_id: int = 1) -> dict:
    """A DHCPv4 Reservation on the wire; subnet-id 0 is a Global Reservation."""
    return {"ip-address": address, "flex-id": f"host-{address}", "hostname": hostname, "subnet-id": subnet_id}


def _kea(leases: Sequence[dict] = (), reservations: Sequence[dict] = (), *, subnets=(SUBNET,), responses=None):
    """Stub a DHCPv4 Kea with *subnets* that reports *leases* and *reservations*."""
    return stub_kea(
        {
            **_catalogue_responses_for_subnets(4, list(subnets)),
            "lease4-get-page": _lease_page(list(leases)),
            "reservation-get-page": _res_page(list(reservations)),
            **(responses or {}),
        }
    )


def _lease_phase(max_leases: int | None = None) -> LeasePhase:
    return LeasePhase(max_leases=max_leases, subnet_prefix_lengths={1: 24})


def _phases(server, sources: Sequence[str] = BOTH, *, max_leases: int | None = None) -> list:
    """Build the phases of *sources*. The Reservation phase reads the stubbed Subnet Catalogue, as the job does."""
    phases: list[LeasePhase | ReservationPhase] = []
    if "lease" in sources:
        phases.append(_lease_phase(max_leases))
    if "reservation" in sources:
        phases.append(ReservationPhase(subnet_catalogue.for_synchronization(server, 4)))
    return phases


def _reconcile(
    server,
    leases: Sequence[dict] = (),
    reservations: Sequence[dict] = (),
    *,
    sources: Sequence[str] = BOTH,
    subnets=(SUBNET,),
    responses=None,
) -> SyncReport:
    """Reconcile the DHCPv4 phases *sources* of *server* against a Kea that reports *leases* and *reservations*."""
    with _kea(leases, reservations, subnets=subnets, responses=responses):
        return reconcile(server, 4, _phases(server, sources))


def _row(address: str = ADDRESS) -> NbIP:
    return NbIP.objects.get(address__net_host=address)


def _links(ip: NbIP, source: str = "lease") -> dict[str, IPAMOwnershipLink]:
    """Return the links of *source* to *ip*, by Server name."""
    links = IPAMOwnershipLink.objects.filter(ip_address=ip, source=source).select_related("server")
    return {link.server.name: link for link in links}


def _owners(ip: NbIP) -> set[tuple[str, str]]:
    """Return the (Server name, source) pairs that link *ip*."""
    return set(IPAMOwnershipLink.objects.filter(ip_address=ip).values_list("server__name", "source"))


def _run_job(server, leases: list[dict], reservations: list[dict] | None = None) -> dict:
    """Run the sync job for *server* against a Kea that reports *leases* and *reservations*; return its summary."""
    job = _make_job()
    with _patch_kea(leases4=leases, reservations=reservations), suppress(JobFailed):
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
            reconcile(server, 4, [_lease_phase()])

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
        # The Reservation link keeps the lease link from being the last link of the Server.
        _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="host")])
        ip = _row()

        with _kea([_lease("10.0.0.6"), _lease("10.0.0.7")], [_reservation(hostname="host")]):
            report = reconcile(self.server, 4, _phases(self.server, max_leases=1))

        self.assertFalse(report.complete)
        self.assertEqual(_owners(ip), {("owner", "lease"), ("owner", "reservation")})

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_a_failed_lease_snapshot_counts_one_error_and_keeps_the_stale_links(self):
        _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="host")])
        ip = _row()

        with self.assertLogs("netbox_kea.ipam_reconciliation", "WARNING") as logs:
            report = _reconcile(
                self.server,
                reservations=[_reservation(hostname="host")],
                responses={"lease4-get-page": {"result": 1, "text": "database unavailable"}},
            )

        self.assertEqual((report.errors, report.complete), (1, False))
        self.assertTrue(any("the lease snapshot failed" in line for line in logs.output))
        self.assertEqual(_owners(ip), {("owner", "lease"), ("owner", "reservation")})


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
        self.assertEqual(_owners(ip), set())
        row = _row()
        self.assertEqual((row.description, row.dns_name, row.status), ("Printer on floor 2", "host", "dhcp"))

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_removing_the_marker_releases_the_last_link_without_removing_the_row(self):
        _reconcile(self.server, [_lease(hostname="host")])
        ip = self._curate("Printer on floor 2")

        report = _reconcile(self.server, [])

        self.assertEqual(report.conflicts, {ADDRESS})
        self.assertEqual(report.removed, 0)
        self.assertEqual(_owners(ip), set())
        row = _row()
        self.assertEqual((row.pk, row.description, row.status), (ip.pk, "Printer on floor 2", "dhcp"))

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_a_note_after_the_marker_does_not_release_the_row(self):
        _reconcile(self.server, [_lease(hostname="host")])
        ip = self._curate("[kea-sync: lease] printer on floor 2")

        report = _reconcile(self.server, [_lease(hostname="host")])

        self.assertEqual(report.conflicts, set())
        self.assertEqual(set(_links(ip)), {"owner"})
        self._curate("[kea-sync: lease] printer on floor 2")

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
        ip = NbIP.objects.create(address=f"{ADDRESS}/32", status="dhcp", description="[kea-sync: lease]")

        report = _reconcile(self.server, [_lease(hostname="host")])

        row = _row()
        self.assertEqual((row.pk, str(row.address), row.dns_name), (ip.pk, f"{ADDRESS}/24", "host"))
        self.assertEqual(set(_links(ip)), {"owner"})
        self.assertEqual((report.created, report.updated), (0, 1))

    @override_settings(PLUGINS_CONFIG=_config("remove"))
    def test_a_row_outside_the_sync_vrf_is_neither_linked_nor_changed(self):
        self.server.sync_vrf = VRF.objects.create(name="sync")
        self.server.save()
        other_vrf = NbIP.objects.create(address=f"{ADDRESS}/32", status="dhcp", description="[kea-sync: lease]")

        _reconcile(self.server, [_lease(hostname="host")])

        own = NbIP.objects.get(address__net_host=ADDRESS, vrf=self.server.sync_vrf)
        self.assertEqual(set(_links(own)), {"owner"})
        other_vrf.refresh_from_db()
        self.assertEqual((str(other_vrf.address), other_vrf.dns_name), (f"{ADDRESS}/32", ""))
        self.assertEqual(_links(other_vrf), {})


@override_settings(PLUGINS_CONFIG=_config("remove"))
class LeasePhaseMarkerTest(TestCase):
    """The sync rewrites only the marker block at the start of the description and keeps the operator note."""

    def setUp(self):
        self.server = _server("owner")

    def _describe(self, description: str) -> NbIP:
        ip = _row()
        ip.description = description
        ip.save()
        return ip

    def test_an_operator_note_after_the_block_survives_a_status_change(self):
        _reconcile(self.server, [_lease(hostname="host")])
        self.assertEqual(_row().description, "[kea-sync: lease]")
        self._describe("[kea-sync: lease] printer on floor 2")

        _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="host")])

        row = _row()
        self.assertEqual(row.status, "active")
        self.assertEqual(row.description, "[kea-sync: lease + reservation] printer on floor 2")

    def test_a_legacy_marker_becomes_the_block_and_keeps_its_note(self):
        cases = (
            ("Synced from Kea DHCP lease printer on floor 2", "[kea-sync: lease + reservation] printer on floor 2"),
            ("Synced from Kea DHCP lease + reservation", "[kea-sync: lease + reservation]"),
            ("Synced from Kea DHCP lease, rack 4", "[kea-sync: lease + reservation], rack 4"),
        )
        for legacy, expected in cases:
            with self.subTest(legacy=legacy):
                NbIP.objects.all().delete()
                ip = NbIP.objects.create(address=f"{ADDRESS}/24", status="dhcp", description=legacy)

                report = _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="host")])

                self.assertEqual(report.conflicts, set())
                self.assertEqual((_row().pk, _row().description), (ip.pk, expected))
                self.assertEqual(_owners(ip), {("owner", "lease"), ("owner", "reservation")})

    def test_a_changed_or_moved_block_releases_the_row(self):
        for description in ("[kea-sync: lease ]", "rack 4 [kea-sync: lease]", "[kea-sync: leases]", "[kea-sync:lease]"):
            with self.subTest(description=description):
                NbIP.objects.all().delete()
                _reconcile(self.server, [_lease(hostname="host")])
                ip = self._describe(description)

                report = _reconcile(self.server, [_lease(hostname="renamed")], [_reservation(hostname="renamed")])

                self.assertEqual(report.conflicts, {ADDRESS})
                self.assertEqual(_owners(ip), set())
                row = _row()
                self.assertEqual((row.description, row.dns_name, row.status), (description, "host", "dhcp"))

    def test_a_note_that_does_not_fit_with_the_new_block_keeps_the_link_and_the_row(self):
        _reconcile(self.server, [_lease(hostname="host")])
        description = "[kea-sync: lease] " + "n" * (200 - len("[kea-sync: lease] "))
        ip = self._describe(description)

        report = _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="renamed")])

        self.assertEqual(report.conflicts, {ADDRESS})
        self.assertEqual(report.errors, 0)
        self.assertEqual(_owners(ip), {("owner", "lease"), ("owner", "reservation")})
        self.assertIsNone(_links(ip, "reservation")["owner"].stale_mark)
        row = _row()
        self.assertEqual((row.description, row.dns_name, row.status), (description, "host", "dhcp"))

        self._describe("[kea-sync: lease] short note")
        fitting = _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="renamed")])

        self.assertEqual(fitting.conflicts, set())
        row = _row()
        self.assertEqual(
            (row.description, row.dns_name, row.status),
            ("[kea-sync: lease + reservation] short note", "renamed", "active"),
        )


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


OVERLAPPING = (SUBNET, {"id": 2, "subnet": "10.0.0.0/16"})


@override_settings(PLUGINS_CONFIG=_config("remove"))
class ReservationPhaseTest(TestCase):
    """The Reservation phase links each allocation address of an In-Subnet Reservation with the reservation source."""

    def setUp(self):
        self.server = _server("owner")

    def test_an_in_subnet_reservation_creates_a_reserved_row_with_the_subnet_prefix_length(self):
        report = _reconcile(self.server, reservations=[_reservation(hostname="printer")])

        row = _row()
        self.assertEqual(
            (str(row.address), row.status, row.dns_name, row.description),
            (f"{ADDRESS}/24", "reserved", "printer", "[kea-sync: reservation]"),
        )
        self.assertEqual(_owners(row), {("owner", "reservation")})
        self.assertEqual(_links(row, "reservation")["owner"].facts, {"hostname": "printer", "prefix_length": 24})
        self.assertEqual((report.created, report.complete), (1, True))

    def test_a_hardware_address_reservation_syncs_its_mac_address(self):
        from dcim.models import MACAddress

        reservation = {"ip-address": ADDRESS, "hw-address": "aa:bb:cc:dd:ee:01", "hostname": "printer", "subnet-id": 1}

        _reconcile(self.server, reservations=[reservation])

        self.assertEqual(MACAddress.objects.get(mac_address="aa:bb:cc:dd:ee:01").description, "dhcp_hostname: printer")

    def test_reservation_rows_use_the_sync_vrf(self):
        self.server.sync_vrf = VRF.objects.create(name="sync")
        self.server.save()
        global_row = NbIP.objects.create(
            address=f"{ADDRESS}/24", status="reserved", description="[kea-sync: reservation]"
        )

        _reconcile(self.server, reservations=[_reservation(hostname="printer")])

        own = NbIP.objects.get(address__net_host=ADDRESS, vrf=self.server.sync_vrf)
        self.assertEqual((own.status, own.dns_name), ("reserved", "printer"))
        self.assertEqual(_owners(own), {("owner", "reservation")})
        global_row.refresh_from_db()
        self.assertEqual((global_row.dns_name, _owners(global_row)), ("", set()))

    def test_a_deleted_reservations_addresses_become_stale_and_follow_stale_ip_cleanup(self):
        expected = {
            "remove": (None, set()),
            "deprecate": ("deprecated", {("owner", "reservation")}),
            "none": ("reserved", set()),
        }
        for mode, (status, owners) in expected.items():
            with self.subTest(mode=mode), override_settings(PLUGINS_CONFIG=_config(mode)):
                NbIP.objects.all().delete()
                _reconcile(self.server, reservations=[_reservation(hostname="printer")])
                ip = _row()

                report = _reconcile(self.server)

                row = NbIP.objects.filter(pk=ip.pk).first()
                self.assertEqual(row.status if row else None, status)
                self.assertEqual(_owners(ip), owners)
                self.assertEqual((report.removed, report.deprecated), (int(mode == "remove"), int(mode == "deprecate")))

    def test_a_row_with_a_lease_link_and_a_reservation_link_stays_active_while_both_links_stay(self):
        leases, reservations = [_lease(hostname="host")], [_reservation(hostname="host")]
        _reconcile(self.server, leases, reservations)
        ip = _row()
        self.assertEqual((ip.status, ip.description), ("active", "[kea-sync: lease + reservation]"))
        self.assertEqual(_owners(ip), {("owner", "lease"), ("owner", "reservation")})

        for sources in (("lease",), ("reservation",), BOTH):
            with self.subTest(sources=sources):
                again = _reconcile(self.server, leases, reservations, sources=sources)

                self.assertEqual((again.created, again.updated, again.disagreements), (0, 0, set()))
                self.assertEqual((_row().pk, _row().status), (ip.pk, "active"))

    def test_the_status_follows_when_cleanup_removes_a_link_that_is_not_the_last(self):
        cases = (
            ("the Reservation goes", [_lease(hostname="host")], [], "dhcp", "lease"),
            ("the lease goes", [], [_reservation(hostname="host")], "reserved", "reservation"),
        )
        for case, leases, reservations, status, source in cases:
            with self.subTest(case):
                NbIP.objects.all().delete()
                _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="host")])
                ip = _row()

                report = _reconcile(self.server, leases, reservations)

                row = _row()
                self.assertEqual((row.pk, row.status, row.description), (ip.pk, status, f"[kea-sync: {source}]"))
                self.assertEqual(_owners(ip), {("owner", source)})
                self.assertEqual((report.removed, report.updated), (0, 1))

    def test_an_incomplete_reservation_snapshot_skips_reservation_cleanup_and_the_lease_phase_keeps_last_links(self):
        failures = {
            "a diagnostic": _res_page([{"ip-address": "not-an-ip", "flex-id": "bad", "subnet-id": 1}]),
            "host_cmds unavailable": {"result": 2, "text": "unknown command"},
            "a failed read": {"result": 1, "text": "database unavailable"},
        }
        for case, page in failures.items():
            with self.subTest(case):
                NbIP.objects.all().delete()
                _reconcile(
                    self.server,
                    [_lease("10.0.0.5", "both"), _lease("10.0.0.6", "leased")],
                    [_reservation("10.0.0.5", "both"), _reservation("10.0.0.7", "reserved")],
                )
                both, leased, reserved = _row("10.0.0.5"), _row("10.0.0.6"), _row("10.0.0.7")

                report = _reconcile(self.server, responses={"reservation-get-page": page})

                self.assertFalse(report.complete)
                self.assertEqual(report.removed, 0)
                # The Server keeps its Reservation link, so the lease phase removes the stale lease link.
                self.assertEqual((_owners(both), _row("10.0.0.5").status), ({("owner", "reservation")}, "reserved"))
                self.assertEqual((_owners(leased), _row("10.0.0.6").status), ({("owner", "lease")}, "dhcp"))
                self.assertEqual((_owners(reserved), _row("10.0.0.7").status), ({("owner", "reservation")}, "reserved"))

    def test_a_failed_lease_phase_keeps_the_last_link_to_an_address_that_the_reservation_phase_drops(self):
        _reconcile(
            self.server,
            [_lease("10.0.0.5", "both")],
            [_reservation("10.0.0.5", "both"), _reservation("10.0.0.7", "reserved")],
        )
        both, reserved = _row("10.0.0.5"), _row("10.0.0.7")

        report = _reconcile(self.server, responses={"lease4-get-page": {"result": 1, "text": "database unavailable"}})

        self.assertFalse(report.complete)
        self.assertEqual((_owners(reserved), _row("10.0.0.7").status), ({("owner", "reservation")}, "reserved"))
        # A failed phase does not block the removal of a link when the Server keeps another link to the object.
        self.assertEqual((_owners(both), _row("10.0.0.5").status), ({("owner", "lease")}, "dhcp"))

    def test_a_call_that_runs_only_one_phase_never_removes_the_last_link_of_the_server(self):
        cases = (("lease", [_lease(hostname="host")], []), ("reservation", [], [_reservation(hostname="host")]))
        for source, leases, reservations in cases:
            with self.subTest(source=source):
                NbIP.objects.all().delete()
                _reconcile(self.server, leases, reservations)
                ip = _row()

                report = _reconcile(self.server, sources=(source,))

                self.assertTrue(report.complete)
                self.assertEqual((report.removed, report.deprecated), (0, 0))
                self.assertTrue(NbIP.objects.filter(pk=ip.pk).exists())
                self.assertEqual(_owners(ip), {("owner", source)})

    def test_a_multi_address_dhcpv6_reservation_keeps_all_its_addresses(self):
        server = _make_db_server(name="v6", ca_url="https://v6.example.com", dhcp4=False)
        host = {"ip-addresses": ["2001:db8::20", "2001:db8::21"], "duid": "00:01:02:03", "hostname": "multi"}
        kea = {
            **_catalogue_responses_for_subnets(6, [{"id": 1, "subnet": "2001:db8::/64"}]),
            "lease6-get-page": _lease_page([]),
            "reservation-get-page": _res_page([{**host, "subnet-id": 1}]),
        }

        for _ in range(2):
            with stub_kea(kea):
                phases = [LeasePhase(None, {}), ReservationPhase(subnet_catalogue.for_synchronization(server, 6))]
                report = reconcile(server, 6, phases)
            self.assertEqual((report.complete, report.removed), (True, 0))

        rows = list(NbIP.objects.order_by("address"))
        self.assertEqual([str(row.address) for row in rows], ["2001:db8::20/64", "2001:db8::21/64"])
        for row in rows:
            self.assertEqual((row.status, row.dns_name), ("reserved", "multi"))
            self.assertEqual(_owners(row), {("v6", "reservation")})

    def test_an_address_whose_lease_goes_and_whose_new_reservation_appears_in_the_same_run_keeps_its_id(self):
        _reconcile(self.server, [_lease(hostname="host")])
        ip = _row()

        report = _reconcile(self.server, reservations=[_reservation(hostname="host")])

        row = _row()
        self.assertEqual((row.pk, row.status), (ip.pk, "reserved"))
        self.assertEqual(_owners(row), {("owner", "reservation")})
        self.assertEqual((report.created, report.removed), (0, 0))

    def _overlapping(self, leases: Sequence[dict] = ()) -> SyncReport:
        """Reconcile two Reservations of ADDRESS in overlapping Subnets, with different hostnames and masks."""
        reservations = [_reservation(hostname="host-a"), _reservation(hostname="host-b", subnet_id=2)]
        return _reconcile(self.server, leases, reservations, subnets=OVERLAPPING)

    def test_two_reservations_in_overlapping_subnets_with_different_facts_create_nothing(self):
        report = self._overlapping()

        self.assertFalse(NbIP.objects.exists())
        self.assertEqual((report.disagreements, report.created, report.errors), ({ADDRESS}, 0, 0))

    def test_two_reservations_in_overlapping_subnets_keep_the_stored_facts_of_the_reservation_link(self):
        _reconcile(self.server, reservations=[_reservation(hostname="host-a")], subnets=OVERLAPPING)
        ip = _row()
        stored = _links(ip, "reservation")["owner"]

        report = self._overlapping()

        link = _links(ip, "reservation")["owner"]
        self.assertEqual(report.disagreements, {ADDRESS})
        self.assertEqual(link.facts, {"hostname": "host-a", "prefix_length": 24})
        self.assertGreater(link.confirmation, stored.confirmation)
        row = _row()
        self.assertEqual((str(row.address), row.dns_name, row.status), (f"{ADDRESS}/24", "host-a", "reserved"))

    def test_two_reservations_in_overlapping_subnets_link_an_owned_row_without_facts(self):
        _reconcile(self.server, [_lease(hostname="host")], subnets=OVERLAPPING)
        ip = _row()

        report = self._overlapping([_lease(hostname="host")])

        self.assertEqual(report.disagreements, {ADDRESS})
        self.assertIsNone(_links(ip, "reservation")["owner"].facts)
        row = _row()
        self.assertEqual(
            (row.pk, row.dns_name, row.status, row.description), (ip.pk, "host", "dhcp", "[kea-sync: lease]")
        )

    def test_a_global_reservation_links_an_existing_owned_row_without_facts_and_does_not_change_it(self):
        _reconcile(self.server, [_lease(hostname="host")])
        ip = _row()

        report = _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="other", subnet_id=0)])

        self.assertIsNone(_links(ip, "reservation")["owner"].facts)
        row = _row()
        self.assertEqual((row.dns_name, row.status, row.description), ("host", "dhcp", "[kea-sync: lease]"))
        self.assertEqual((report.updated, report.conflicts, report.disagreements), (0, set(), set()))
        self.assertEqual(len(report.skipped_reservations), 1)

    def test_a_global_reservation_creates_no_row(self):
        report = _reconcile(self.server, reservations=[_reservation(hostname="other", subnet_id=0)])

        self.assertFalse(NbIP.objects.exists())
        self.assertEqual((report.created, report.conflicts, report.complete), (0, set(), True))

    def test_a_global_reservation_leaves_an_unlinked_curated_row_alone(self):
        ip = NbIP.objects.create(address=f"{ADDRESS}/32", status="active", description="Printer on floor 2")

        report = _reconcile(self.server, reservations=[_reservation(hostname="other", subnet_id=0)])

        self.assertEqual((report.conflicts, _owners(ip)), (set(), set()))
        self.assertEqual(_row().description, "Printer on floor 2")

    def test_a_link_without_facts_gives_no_status_and_counts_as_a_link_of_its_server_during_cleanup(self):
        _reconcile(self.server, [_lease(hostname="host")], [_reservation(subnet_id=0)])
        ip = _row()
        self.assertEqual(ip.status, "dhcp")

        lease_gone = _reconcile(self.server, reservations=[_reservation(subnet_id=0)])

        self.assertEqual(lease_gone.removed, 0)
        self.assertEqual(_owners(ip), {("owner", "reservation")})
        self.assertEqual((_row().pk, _row().status), (ip.pk, "dhcp"))

        all_gone = _reconcile(self.server)

        self.assertEqual(all_gone.removed, 1)
        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())

    def test_a_link_without_facts_takes_part_in_no_fact_comparison(self):
        other = _server("other")
        _reconcile(self.server, [_lease(hostname="host")], [_reservation(subnet_id=0)])
        _reconcile(self.server, reservations=[_reservation(subnet_id=0)])

        report = _reconcile(other, [_lease(hostname="renamed")])

        self.assertEqual(report.disagreements, set())
        self.assertEqual(_row().dns_name, "renamed")

    @override_settings(PLUGINS_CONFIG=_config("deprecate"))
    def test_a_stale_reservation_link_whose_reservation_comes_back_restores_the_status(self):
        _reconcile(self.server, reservations=[_reservation(hostname="printer")])
        _reconcile(self.server)
        self.assertEqual(_row().status, "deprecated")
        self.assertIsNotNone(_links(_row(), "reservation")["owner"].stale_mark)

        report = _reconcile(self.server, reservations=[_reservation(hostname="printer")])

        row = _row()
        self.assertEqual((row.status, row.description), ("reserved", "[kea-sync: reservation]"))
        self.assertIsNone(_links(row, "reservation")["owner"].stale_mark)
        self.assertEqual(report.updated, 1)

    @override_settings(PLUGINS_CONFIG=_config("deprecate"))
    def test_a_stale_lease_link_goes_when_a_reservation_of_the_same_server_links_the_object(self):
        _reconcile(self.server, [_lease(hostname="host")])
        _reconcile(self.server)
        ip = _row()
        self.assertEqual((ip.status, _owners(ip)), ("deprecated", {("owner", "lease")}))

        _reconcile(self.server, reservations=[_reservation(hostname="host")])

        self.assertEqual(_owners(ip), {("owner", "reservation")})
        self.assertEqual((_row().pk, _row().status), (ip.pk, "reserved"))

    def test_the_links_of_every_server_count_for_the_status(self):
        other = _server("other")
        _reconcile(self.server, [_lease(hostname="host")])

        _reconcile(other, reservations=[_reservation(hostname="host")])
        self.assertEqual(_row().status, "active")

        reported = {self.server: ([_lease(hostname="host")], []), other: ([], [_reservation(hostname="host")])}
        for server in (self.server, other, self.server):
            again = _reconcile(server, *reported[server])
            self.assertEqual((again.updated, _row().status), (0, "active"))

    def test_a_status_that_does_not_fit_after_cleanup_keeps_the_stale_link_until_the_next_run(self):
        _reconcile(self.server, [_lease(hostname="host")])
        description = "[kea-sync: lease] " + "n" * (200 - len("[kea-sync: lease] "))
        NbIP.objects.filter(pk=_row().pk).update(description=description)
        # The new Reservation makes the status active, but "[kea-sync: lease + reservation]" and the note do not fit.
        _reconcile(self.server, [_lease(hostname="host")], [_reservation(hostname="host")])
        ip = _row()

        report = _reconcile(self.server, reservations=[_reservation(hostname="host")])

        self.assertEqual(report.conflicts, {ADDRESS})
        self.assertEqual(_owners(ip), {("owner", "lease"), ("owner", "reservation")})
        self.assertEqual((_row().status, _row().description), ("dhcp", description))

        NbIP.objects.filter(pk=ip.pk).update(description="[kea-sync: lease] short note")
        fitting = _reconcile(self.server, reservations=[_reservation(hostname="host")])

        self.assertEqual(fitting.conflicts, set())
        self.assertEqual(_owners(ip), {("owner", "reservation")})
        self.assertEqual((_row().status, _row().description), ("reserved", "[kea-sync: reservation] short note"))


@override_settings(PLUGINS_CONFIG=_config("remove"))
class OwnerFactsTest(TestCase):
    """Owners of one source compare hostnames, all owners compare the prefix length, and a Reservation hostname wins."""

    def setUp(self):
        self.server = _server("owner")

    def _twice(self, leases: Sequence[dict], reservations: Sequence[dict]) -> tuple[SyncReport, SyncReport]:
        return _reconcile(self.server, leases, reservations), _reconcile(self.server, leases, reservations)

    def test_a_reservation_with_a_hostname_and_a_lease_without_one_converge(self):
        first, again = self._twice([_lease()], [_reservation(hostname="printer")])

        self.assertEqual((first.disagreements, again.disagreements), (set(), set()))
        self.assertEqual((again.created, again.updated), (0, 0))
        self.assertEqual((_row().dns_name, _row().status), ("printer", "active"))

    def test_a_reservation_hostname_wins_over_a_different_lease_hostname(self):
        first, again = self._twice([_lease(hostname="printer.example.com")], [_reservation(hostname="printer")])

        self.assertEqual((first.disagreements, again.disagreements), (set(), set()))
        self.assertEqual((again.created, again.updated), (0, 0))
        self.assertEqual((_row().dns_name, _row().status), ("printer", "active"))
        self.assertEqual(_links(_row())["owner"].facts["hostname"], "printer.example.com")

    def test_a_lease_hostname_applies_when_the_reservation_has_none(self):
        _, again = self._twice([_lease(hostname="laptop")], [_reservation()])

        self.assertEqual((again.disagreements, again.updated), (set(), 0))
        self.assertEqual(_row().dns_name, "laptop")

    def test_two_servers_that_reserve_one_address_with_different_hostnames_disagree(self):
        other = _server("other")
        _reconcile(self.server, reservations=[_reservation(hostname="host-a")])

        report = _reconcile(other, reservations=[_reservation(hostname="host-b")])

        self.assertEqual(report.disagreements, {ADDRESS})
        self.assertEqual(_row().dns_name, "host-a")

    def test_an_empty_hostname_of_another_owner_makes_no_claim(self):
        other = _server("other")
        _reconcile(self.server, [_lease(hostname="host")])

        report = _reconcile(other, [_lease()])

        self.assertEqual(report.disagreements, set())
        self.assertEqual(_row().dns_name, "host")

    def test_a_lease_and_a_reservation_with_different_prefix_lengths_disagree_and_leave_the_row_unchanged(self):
        _reconcile(self.server, [_lease(hostname="host")], subnets=OVERLAPPING)

        report = _reconcile(
            self.server, [_lease(hostname="host")], [_reservation(hostname="host", subnet_id=2)], subnets=OVERLAPPING
        )

        self.assertEqual(report.disagreements, {ADDRESS})
        row = _row()
        self.assertEqual((str(row.address), row.status), (f"{ADDRESS}/24", "dhcp"))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
class JobReservationPhaseTest(TestCase):
    """The job runs the lease and Reservation phases of each family in one reconcile call."""

    def test_the_job_links_a_leased_and_reserved_address_from_both_phases(self):
        server = _server("owner", sync_prefixes_enabled=False, sync_ip_ranges_enabled=False)

        summary = _run_job(server, [_lease(hostname="host")], [_reservation(hostname="host")])

        row = _row()
        self.assertEqual((row.status, row.dns_name), ("active", "host"))
        self.assertEqual(_owners(row), {("owner", "lease"), ("owner", "reservation")})
        self.assertEqual((summary["created"], summary["errors"], summary["disagreements"]), (1, 0, 0))

    def test_the_old_stale_cleanup_keeps_an_unlinked_row_at_a_reservation_address(self):
        server = _server("owner", sync_vrf=VRF.objects.create(name="sync"))
        legacy = NbIP.objects.create(
            address=f"{ADDRESS}/24", status="reserved", dns_name="printer", description="[kea-sync: reservation]"
        )

        _run_job(server, [_lease("10.0.0.6", "printer")], [_reservation(ADDRESS, "printer")])

        self.assertTrue(NbIP.objects.filter(pk=legacy.pk).exists())
        self.assertEqual(NbIP.objects.filter(vrf=server.sync_vrf).count(), 2)


class _Holder:
    """A transaction on its own connection that holds the locks its statement takes until it commits."""

    def __init__(self, sql: str, params: list) -> None:
        self.connection = connections.create_connection("default")
        self.cursor = self.connection.cursor()
        self.cursor.execute("BEGIN")
        self.cursor.execute(sql, params)

    def commit(self) -> None:
        if self.connection.connection is not None:
            self.cursor.execute("COMMIT")
            self.cursor.close()
            self.connection.close()


@override_settings(PLUGINS_CONFIG=_config("remove"))
class LeasePhaseConcurrencyTest(TransactionTestCase):
    """Concurrent runs, row locks and operator edits, each on its own connection, in a fixed order."""

    def setUp(self) -> None:
        self.results: dict[str, object] = {}
        self.threads: list[threading.Thread] = []
        self.holders: list[_Holder] = []

    def tearDown(self) -> None:
        for holder in self.holders:
            holder.commit()
        for thread in self.threads:
            thread.join(timeout=30)

    def _hold(self, sql: str, params: list) -> _Holder:
        holder = _Holder(sql, params)
        self.holders.append(holder)
        return holder

    def _start(self, name: str, work: Callable[[], object]) -> threading.Thread:
        def run():
            try:
                self.results[name] = work()
            except BaseException as exc:  # noqa: BLE001  the test thread hands every outcome to the test
                self.results[name] = exc
            finally:
                connection.close()

        thread = threading.Thread(target=run, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def _join(self) -> None:
        for thread in self.threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), f"{thread.name} did not finish")

    def _wait_for_lock_waits(self, count: int) -> None:
        deadline = time.monotonic() + 30
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                # Inside a transaction, pg_stat_activity keeps its first snapshot until it is cleared.
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock'"
                )
                if cursor.fetchone()[0] >= count:
                    return
                time.sleep(0.01)
        self.fail(f"{count} lock waits never happened; results: {self.results}")

    def _report(self, name: str) -> SyncReport:
        result = self.results[name]
        if not isinstance(result, SyncReport):
            self.fail(f"{name} did not return a report: {result!r}")
        return result

    def test_two_ha_members_that_run_at_the_same_time_create_one_row(self):
        first, second = _server("first"), _server("second")
        # The table lock lets both runs look the address up, but no run insert it, until the test commits.
        holder = self._hold("LOCK TABLE ipam_ipaddress IN SHARE MODE", [])

        with stub_kea({"lease4-get-page": _lease_page([_lease(hostname="host")])}):
            for server in (first, second):
                self._start(server.name, lambda server=server: reconcile(server, 4, [_lease_phase()]))
            self._wait_for_lock_waits(2)
            holder.commit()
            self._join()

        self.assertEqual(NbIP.objects.filter(address__net_host=ADDRESS).count(), 1)
        self.assertEqual(set(_links(_row())), {"first", "second"})
        self.assertEqual(sorted(self._report(name).created for name in ("first", "second")), [0, 1])

    def test_a_lock_error_on_one_row_fails_only_that_row(self):
        server = _server("owner")
        _reconcile(server, [_lease("10.0.0.1", "one"), _lease("10.0.0.2", "two"), _lease("10.0.0.9", "stale")])
        self._hold("SELECT id FROM ipam_ipaddress WHERE id = %s FOR UPDATE", [_row("10.0.0.1").pk])

        def run(phases):
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '100ms'")
            return reconcile(server, 4, phases)

        with _kea([_lease("10.0.0.1", "one-b"), _lease("10.0.0.2", "two-b")]):
            phases = _phases(server)
            self._start("run", lambda: run(phases))
            self._join()

        report = self._report("run")
        self.assertEqual((report.errors, report.complete, report.updated), (1, False, 1))
        self.assertEqual(_row("10.0.0.1").dns_name, "one")
        self.assertEqual(_row("10.0.0.2").dns_name, "two-b")
        self.assertEqual(set(_links(_row("10.0.0.9"))), {"owner"})

    def test_a_link_confirmed_after_the_cutoff_survives_also_when_its_transaction_started_before(self):
        server = _server("owner")
        _reconcile(server, [_lease(hostname="host")])
        ip = _row()
        holder = self._hold("SELECT id FROM ipam_ipaddress WHERE id = %s FOR UPDATE", [ip.pk])

        def lease_page(body):
            reported = [_lease(hostname="host")] if threading.current_thread().name == "claim" else []
            return _lease_page(reported)

        with _kea(responses={"lease4-get-page": lease_page}):
            # Both phases run, so the cleanup may remove the last link of the Server.
            phases = _phases(server)
            # The claim takes the identity lock, then waits for the row lock inside its transaction.
            self._start("claim", lambda: reconcile(server, 4, phases))
            self._wait_for_lock_waits(1)
            # The cleanup takes its cutoff now, reads a snapshot without the lease, and waits for the identity lock.
            self._start("cleanup", lambda: reconcile(server, 4, phases))
            self._wait_for_lock_waits(2)
            holder.commit()
            self._join()

        self.assertEqual(self._report("cleanup").removed, 0)
        self.assertTrue(NbIP.objects.filter(pk=ip.pk).exists())
        self.assertEqual(set(_links(ip)), {"owner"})

    def test_an_operator_edit_that_removed_the_marker_before_the_row_lock_releases_the_row(self):
        server = _server("owner")
        _reconcile(server, [_lease(hostname="host")])
        ip = _row()
        operator = self._hold("UPDATE ipam_ipaddress SET description = 'Printer on floor 2' WHERE id = %s", [ip.pk])

        with stub_kea({"lease4-get-page": _lease_page([_lease(hostname="renamed")])}):
            self._start("run", lambda: reconcile(server, 4, [_lease_phase()]))
            self._wait_for_lock_waits(1)
            operator.commit()
            self._join()

        self.assertEqual(self._report("run").conflicts, {ADDRESS})
        self.assertEqual(_links(ip), {})
        row = _row()
        self.assertEqual((row.description, row.dns_name), ("Printer on floor 2", "host"))

    def test_the_old_stale_cleanup_keeps_a_row_that_a_concurrent_run_links_under_the_identity_lock(self):
        server = _server("owner")
        ip = NbIP.objects.create(
            address=f"{ADDRESS}/24", status="dhcp", dns_name="host", description="[kea-sync: lease]"
        )

        with transaction.atomic():
            # The test holds the identity lock like a reconcile run, and links the row while the cleanup waits.
            _lock_identity(None, ADDRESS)
            self._start("cleanup", lambda: _cleanup_stale_ips("10.0.0.6", "host", mode="remove"))
            self._wait_for_lock_waits(1)
            IPAMOwnershipLink.objects.create(
                server=server,
                family=4,
                source=IPAMOwnershipSource.LEASE,
                ip_address=ip,
                confirmation=next_confirmation_number(),
            )
        self._join()

        self.assertEqual(self.results["cleanup"], 0)
        self.assertTrue(NbIP.objects.filter(pk=ip.pk).exists())
        self.assertEqual(set(_links(ip)), {"owner"})
