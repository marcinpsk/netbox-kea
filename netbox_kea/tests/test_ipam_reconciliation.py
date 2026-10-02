# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IPAM Reconciliation (ADR 0006): the lease and Reservation phases own their IP addresses through links.

The tests use the real ORM and a real KeaClient, and stub only requests.Session.post.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress

from core.exceptions import JobFailed
from core.models import Job
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from ipam.models import VRF, Prefix
from ipam.models import IPAddress as NbIP

from netbox_kea import subnet_catalogue
from netbox_kea.ipam_reconciliation import (
    ClaimResult,
    LeasePhase,
    ReservationPhase,
    SyncReport,
    claim,
    reconcile,
)
from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.models import (
    CONFIRMATION_SEQUENCE,
    IPAMOwnershipLink,
    next_confirmation_number,
)
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

    def test_unknown_subnet_marks_the_phase_incomplete_and_keeps_stale_links(self):
        server = _server("owner")
        _reconcile(server, [_lease()])
        existing = _row()
        for mapping in ({}, {1: 24}):
            with self.subTest(mapping=mapping), _kea([_lease("10.0.0.8", **{"subnet-id": 2})]):
                report = reconcile(
                    server,
                    4,
                    [
                        LeasePhase(max_leases=None, subnet_prefix_lengths=mapping),
                        ReservationPhase(subnet_catalogue.for_synchronization(server, 4)),
                    ],
                )
            self.assertEqual(report.errors, 1)
            self.assertIn("lease", report.incomplete)
            self.assertTrue(NbIP.objects.filter(pk=existing.pk).exists())
            self.assertFalse(NbIP.objects.filter(address__net_host="10.0.0.8").exists())
            self.assertEqual(set(_links(existing)), {"owner"})

    def test_valid_lease_still_applies_when_another_lease_has_an_unknown_subnet(self):
        server = _server("owner")
        report = _reconcile(server, [_lease(), _lease("10.0.0.8", **{"subnet-id": 2})])
        self.assertEqual(report.errors, 1)
        self.assertEqual(report.created, 1)
        self.assertIn("lease", report.incomplete)
        self.assertEqual(str(_row().address), "10.0.0.5/24")
        self.assertFalse(NbIP.objects.filter(address__net_host="10.0.0.8").exists())

    def test_malformed_subnet_id_does_not_abort_valid_leases_or_reservations(self):
        server = _server("owner")
        for index, subnet_id in enumerate(([], {}, True, False, 1.0, "1", None, 0, -1, 4_294_967_295)):
            address = f"10.0.0.{20 + index}"
            with self.subTest(subnet_id=subnet_id):
                report = _reconcile(
                    server,
                    [_lease(address, **{"subnet-id": subnet_id}), _lease()],
                    [_reservation("10.0.0.9")],
                )
                self.assertEqual(report.errors, 1)
                self.assertEqual(report.incomplete, {"lease"})
                self.assertFalse(NbIP.objects.filter(address__net_host=address).exists())
                self.assertEqual(str(_row().address), "10.0.0.5/24")
                self.assertEqual(_row("10.0.0.9").status, "reserved")
                self.assertEqual(set(_links(_row())), {"owner"})
                self.assertEqual(set(_links(_row("10.0.0.9"), "reservation")), {"owner"})

    def test_unavailable_catalogue_fallback_still_rejects_a_malformed_subnet_id(self):
        server = _server("owner")
        phase = LeasePhase(max_leases=None, subnet_prefix_lengths=None)
        with _kea([_lease("10.0.0.8", **{"subnet-id": []}), _lease()]):
            report = reconcile(server, 4, [phase])
        self.assertEqual(report.errors, 1)
        self.assertEqual(report.incomplete, {"lease"})
        self.assertFalse(NbIP.objects.filter(address__net_host="10.0.0.8").exists())
        self.assertEqual(str(_row().address), "10.0.0.5/32")

    def test_explicit_unavailable_catalogue_fallback_uses_only_the_server_vrf(self):
        vrf = VRF.objects.create(name="fallback-vrf")
        server = _server("owner", sync_vrf=vrf)
        Prefix.objects.create(prefix="10.0.0.5/32")
        Prefix.objects.create(prefix="10.0.0.0/24", vrf=vrf)
        phase = LeasePhase(max_leases=None, subnet_prefix_lengths=None)
        with _kea([_lease()]):
            report = reconcile(server, 4, [phase])
        self.assertEqual(report.errors, 0)
        self.assertEqual(str(_row().address), "10.0.0.5/24")
        Prefix.objects.create(prefix="10.0.0.5/32", vrf=vrf)
        with _kea([_lease()]):
            reconcile(server, 4, [phase])
        self.assertEqual(str(_row().address), "10.0.0.5/32")
        with _kea([_lease("10.1.0.1")]):
            reconcile(server, 4, [phase])
        self.assertEqual(str(_row("10.1.0.1").address), "10.1.0.1/32")

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
class JobDisabledReservationCleanupTest(TestCase):
    """A complete lease snapshot can finish cleanup when the Server disables Reservations."""

    def _server_with_lease(self, family):
        server = _make_db_server(
            name=f"lease-owner-v{family}",
            dhcp4=family == 4,
            dhcp6=family == 6,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        address = "198.18.0.25" if family == 4 else "2001:db8::25"
        self._run(server, family, [_lease(address, "lease.example.invalid")])
        ip = NbIP.objects.get(address__net_host=address)
        self.assertEqual(ip.status, "dhcp")
        self.assertEqual(IPAMOwnershipLink.objects.filter(ip_address=ip, source="lease").count(), 1)
        return server, ip

    def _run(self, server, family, leases=(), *, responses=None):
        subnet = "198.18.0.0/24" if family == 4 else "2001:db8::/64"
        registry = {
            **_catalogue_responses_for_subnets(family, [{"id": 1, "subnet": subnet}]),
            f"lease{family}-get-page": _lease_page(list(leases)),
            "reservation-get-page": _res_page([]),
            **(responses or {}),
        }
        job = Job.objects.create(name="Kea IPAM Sync", job_id=uuid.uuid4(), data={})
        with stub_kea(registry):
            KeaIpamSyncJob(job).run(server_pk=server.pk)
        return job.data["summary"][0]

    def test_complete_lease_snapshot_removes_the_last_link_when_reservations_are_disabled(self):
        for family in (4, 6):
            with self.subTest(family=family):
                server, ip = self._server_with_lease(family)
                self._run(server, family)
                self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())
                self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address_id=ip.pk).exists())

    @override_settings(PLUGINS_CONFIG=_config("deprecate"))
    def test_complete_lease_snapshot_deprecates_and_retains_a_stale_link(self):
        for family in (4, 6):
            with self.subTest(family=family):
                server, ip = self._server_with_lease(family)
                self._run(server, family)
                ip.refresh_from_db()
                self.assertEqual(ip.status, "deprecated")
                link = IPAMOwnershipLink.objects.get(ip_address=ip)
                self.assertIsNotNone(link.stale_mark)
                self._run(server, family, [_lease(str(ip.address.ip), "lease.example.invalid")])
                ip.refresh_from_db()
                link.refresh_from_db()
                self.assertEqual(ip.status, "dhcp")
                self.assertIsNone(link.stale_mark)

    @override_settings(PLUGINS_CONFIG=_config("none"))
    def test_complete_lease_snapshot_unlinks_without_changing_the_address(self):
        for family in (4, 6):
            with self.subTest(family=family):
                server, ip = self._server_with_lease(family)
                self._run(server, family)
                ip.refresh_from_db()
                self.assertEqual(ip.status, "dhcp")
                self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=ip).exists())

    def test_failed_lease_snapshot_keeps_all_stale_links(self):
        for family in (4, 6):
            with self.subTest(family=family):
                server, ip = self._server_with_lease(family)
                address = "198.18.0.26" if family == 4 else "2001:db8::26"
                self._run(server, family, [_lease(str(ip.address.ip)), _lease(address)])
                links = list(IPAMOwnershipLink.objects.filter(server=server).values_list("pk", "ip_address_id"))
                self.assertEqual(len(links), 2)
                with self.assertRaises(JobFailed):
                    self._run(server, family, responses={f"lease{family}-get-page": {"result": 1}})
                self.assertEqual(
                    list(IPAMOwnershipLink.objects.filter(server=server).values_list("pk", "ip_address_id")), links
                )
                self.assertEqual(NbIP.objects.filter(pk__in=[pk for _, pk in links], status="dhcp").count(), 2)

    def test_truncated_lease_snapshot_keeps_all_stale_links(self):
        for family in (4, 6):
            with self.subTest(family=family):
                server, ip = self._server_with_lease(family)
                address = "198.18.0.26" if family == 4 else "2001:db8::26"
                self._run(server, family, [_lease(str(ip.address.ip)), _lease(address)])
                links = list(IPAMOwnershipLink.objects.filter(server=server).values_list("pk", "ip_address_id"))
                self.assertEqual(len(links), 2)
                live = "198.18.0.30" if family == 4 else "2001:db8::30"
                overflow = "198.18.0.31" if family == 4 else "2001:db8::31"
                with self.settings(PLUGINS_CONFIG=plugins_config(sync_max_leases_per_server=1)):
                    self._run(server, family, [_lease(live), _lease(overflow)])
                self.assertEqual(
                    list(
                        IPAMOwnershipLink.objects.filter(pk__in=[pk for pk, _ in links]).values_list(
                            "pk", "ip_address_id"
                        )
                    ),
                    links,
                )
                self.assertEqual(NbIP.objects.filter(pk__in=[pk for _, pk in links], status="dhcp").count(), 2)
                self.assertTrue(NbIP.objects.filter(address__net_host=live).exists())
                self.assertFalse(NbIP.objects.filter(address__net_host=overflow).exists())

    def test_unavailable_host_hook_keeps_the_last_link_with_config_file_reservations(self):
        for family in (4, 6):
            with self.subTest(family=family):
                server, ip = self._server_with_lease(family)
                server.sync_reservations_enabled = True
                server.save(update_fields=["sync_reservations_enabled"])
                reservation = {"hw-address": "02:00:00:00:00:25"}
                reservation["ip-address" if family == 4 else "ip-addresses"] = (
                    str(ip.address.ip) if family == 4 else [str(ip.address.ip)]
                )
                subnet = "198.18.0.0/24" if family == 4 else "2001:db8::/64"
                responses = {
                    **_catalogue_responses_for_subnets(
                        family, [{"id": 1, "subnet": subnet, "reservations": [reservation]}]
                    ),
                    "reservation-get-page": {"result": 2, "text": "unknown command"},
                }
                self._run(server, family, responses=responses)
                ip.refresh_from_db()
                self.assertEqual(ip.status, "dhcp")
                link = IPAMOwnershipLink.objects.get(ip_address=ip)
                self.assertEqual(link.source, "lease")
                self.assertIsNone(link.stale_mark)

    def test_global_reservation_toggle_does_not_relax_the_server_last_link_guard(self):
        from netbox_kea.tests.test_jobs import _set_sync_config

        server, ip = self._server_with_lease(4)
        server.sync_reservations_enabled = True
        server.save(update_fields=["sync_reservations_enabled"])
        _set_sync_config(sync_reservations_enabled=False)
        self._run(server, 4)
        self.assertTrue(NbIP.objects.filter(pk=ip.pk).exists())
        self.assertIsNone(IPAMOwnershipLink.objects.get(ip_address=ip).stale_mark)

    def test_reservation_only_call_keeps_its_last_link_when_the_server_disables_reservations(self):
        server = _server("partial-reservations", sync_reservations_enabled=False)
        address = "198.18.0.25"
        subnet = {"id": 1, "subnet": "198.18.0.0/24"}
        _reconcile(server, reservations=[_reservation(address)], sources=("reservation",), subnets=[subnet])
        ip = NbIP.objects.get(address__net_host=address)
        report = _reconcile(server, sources=("reservation",), subnets=[subnet])
        self.assertTrue(report.complete)
        self.assertTrue(NbIP.objects.filter(pk=ip.pk, status="reserved").exists())
        self.assertEqual(IPAMOwnershipLink.objects.get(ip_address=ip).source, "reservation")


@override_settings(PLUGINS_CONFIG=_config("deprecate"))
class JobDeprecatedIPReleaseTest(TestCase):
    def test_operator_release_of_deprecated_ip_drops_the_marked_link_and_reports_conflict(self):
        for source in ("lease", "reservation"):
            with self.subTest(source=source):
                server = _server(f"release-{source}", sync_prefixes_enabled=False, sync_ip_ranges_enabled=False)
                address = "198.18.0.10" if source == "lease" else "198.18.0.20"
                subnet = {"id": 1, "subnet": "198.18.0.0/24"}

                def run(leases=(), reservations=(), *, server=server, subnet=subnet):
                    job = Job.objects.create(name="Kea IPAM Sync", job_id=uuid.uuid4(), data={})
                    with _kea(leases, reservations, subnets=[subnet]):
                        KeaIpamSyncJob(job).run(server_pk=server.pk)
                    return job.data["summary"][0]

                run(
                    [_lease(address)] if source == "lease" else (),
                    [_reservation(address)] if source == "reservation" else (),
                )
                run()
                ip = NbIP.objects.get(address__net_host=address)
                link = IPAMOwnershipLink.objects.get(ip_address=ip)
                self.assertEqual((ip.status, link.source), ("deprecated", source))
                self.assertIsNotNone(link.stale_mark)
                self.assertLessEqual(link.confirmation, link.stale_mark)
                NbIP.objects.filter(pk=ip.pk).update(
                    description="Operator retained address", dns_name="operator.example.com"
                )
                operator_row = NbIP.objects.values().get(pk=ip.pk)

                summary = run()

                self.assertEqual(NbIP.objects.values().get(pk=ip.pk), operator_row)
                self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=ip).exists())
                self.assertEqual(summary["conflicts"], 1)
                self.assertEqual(summary["conflict_sample"], [address])


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

        # The cleanup that removes the disagreeing link applies the hostname of the remaining link.
        removal = _reconcile(first, [])
        self.assertEqual((_row().dns_name, removal.updated), ("host-b", 1))
        applied = _reconcile(second, [_lease(hostname="host-b")])

        self.assertEqual((applied.disagreements, applied.updated), (set(), 0))

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
        other_vrf = NbIP.objects.create(
            address=f"{ADDRESS}/32",
            vrf=VRF.objects.create(name="other"),
            status="dhcp",
            description="[kea-sync: lease]",
        )

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

    def test_repeated_bad_rows_bound_logs_and_preserve_stale_ownership(self):
        server = _server("owner")
        _reconcile(server, [_lease("198.18.0.99")])
        leases = [_lease(f"198.18.0.{index}", ["invalid"]) for index in range(1, 13)]
        leases.append(_lease("198.18.0.50", "valid.example"))
        with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING") as logs:
            report = _reconcile(server, leases)
        self.assertEqual((report.errors, report.created), (12, 1))
        self.assertIn("lease", report.incomplete)
        self.assertEqual(_row("198.18.0.50").dns_name, "valid.example")
        self.assertEqual(set(_links(_row("198.18.0.99"))), {"owner"})
        self.assertEqual(sum("IPAM reconciliation of" in line for line in logs.output), 10)
        self.assertEqual(sum("Further row failures" in line for line in logs.output), 1)

    def test_duplicate_phase_rejected_before_requests_or_writes(self):
        server = _server("owner")
        _reconcile(server, [_lease("198.18.0.42")])
        before = list(NbIP.objects.values())
        links = list(IPAMOwnershipLink.objects.values())
        with stub_kea({}) as kea, self.assertRaises(ValueError):
            reconcile(server, 4, [_lease_phase(), _lease_phase()])
        self.assertEqual(kea.commands(), [])
        self.assertEqual(list(NbIP.objects.values()), before)
        self.assertEqual(list(IPAMOwnershipLink.objects.values()), links)

    def test_unexpected_hardware_type_preserves_ip_sync_and_sanitizes_logs(self):
        from dcim.models import MACAddress

        server = _server("owner")
        with self.assertLogs("netbox_kea.sync", level="DEBUG") as logs:
            report = _reconcile(server, [_lease("198.18.0.42", **{"hw-address": {"private-value": "invalid"}})])
        self.assertEqual((report.created, report.errors), (1, 0))
        self.assertEqual(set(_links(_row("198.18.0.42"))), {"owner"})
        self.assertFalse(MACAddress.objects.exists())
        self.assertTrue(any("TypeError" in line for line in logs.output))
        self.assertFalse(any("private-value" in line for line in logs.output))

    def test_link_label_identifies_owner_family_source_and_address(self):
        server = _server("owner")
        _reconcile(server, [_lease("198.18.0.42")])
        link = IPAMOwnershipLink.objects.get()
        self.assertEqual(str(link), "owner IPv4 lease → 198.18.0.42/24")

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
        self.assertFalse(NbIP.objects.filter(address__net_host="10.0.0.1").exists())
        self.assertEqual(str(NbIP.objects.get(address__net_host="10.0.0.2").address), "10.0.0.2/24")


OVERLAPPING = (SUBNET, {"id": 2, "subnet": "10.0.0.0/16"})


@override_settings(PLUGINS_CONFIG=_config("remove"))
class ReservationPhaseTest(TestCase):
    """The Reservation phase links each allocation address of an In-Subnet Reservation with the reservation source."""

    def setUp(self):
        self.server = _server("owner")

    def test_in_subnet_facts_win_over_global_reservation_in_either_order(self):
        global_record = _reservation("198.18.0.42", "global.example", subnet_id=0)
        scoped_record = _reservation("198.18.0.42", "scoped.example")
        for records in ([global_record, scoped_record], [scoped_record, global_record]):
            with self.subTest(global_first=records[0] is global_record):
                NbIP.objects.all().delete()
                report = _reconcile(self.server, reservations=records, subnets=({"id": 1, "subnet": "198.18.0.0/24"},))
                ip = _row("198.18.0.42")
                self.assertEqual(
                    (str(ip.address), ip.status, ip.dns_name), ("198.18.0.42/24", "reserved", "scoped.example")
                )
                self.assertEqual((report.created, report.errors), (1, 0))
                link = IPAMOwnershipLink.objects.get(ip_address=ip)
                self.assertEqual((link.server_id, link.source), (self.server.pk, "reservation"))
                self.assertEqual(link.facts, {"hostname": "scoped.example", "prefix_length": 24})

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
        _server("global-owner")
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

    def test_the_hostname_follows_when_cleanup_removes_a_link_that_is_not_the_last(self):
        cases = (
            ("the Reservation goes", [_lease(hostname="laptop.example.com")], [], "laptop.example.com"),
            ("the Reservation goes, the lease names no host", [_lease()], [], "laptop"),
            ("the lease goes", [], [_reservation(hostname="laptop")], "laptop"),
        )
        for case, leases, reservations, dns_name in cases:
            with self.subTest(case):
                NbIP.objects.all().delete()
                _reconcile(
                    self.server, leases or [_lease(hostname="laptop.example.com")], [_reservation(hostname="laptop")]
                )
                self.assertEqual(_row().dns_name, "laptop")

                _reconcile(self.server, leases, reservations)

                self.assertEqual(_row().dns_name, dns_name)

    def test_cleanup_changes_no_hostname_while_the_remaining_reservation_links_disagree(self):
        _reconcile(self.server, [_lease(hostname="laptop.example.com")], [_reservation(hostname="laptop")])
        for name in ("b", "c"):
            disagreeing = _reconcile(_server(name), reservations=[_reservation(hostname=f"laptop-{name}")])
            self.assertEqual(disagreeing.disagreements, {ADDRESS})

        _reconcile(self.server, [_lease(hostname="laptop.example.com")])

        self.assertEqual(_owners(_row()), {("owner", "lease"), ("b", "reservation"), ("c", "reservation")})
        self.assertEqual((_row().status, _row().dns_name), ("active", "laptop"))

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
                    [_lease("10.0.0.5", "both"), _lease("10.0.0.6", "leased"), _lease("10.0.0.8", "still-leased")],
                    [
                        _reservation("10.0.0.5", "both"),
                        _reservation("10.0.0.7", "reserved"),
                        _reservation("10.0.0.8", "still-leased"),
                    ],
                )
                both, leased, reserved, still_leased = (_row(f"10.0.0.{host}") for host in (5, 6, 7, 8))

                report = _reconcile(
                    self.server, [_lease("10.0.0.8", "still-leased")], responses={"reservation-get-page": page}
                )

                self.assertFalse(report.complete)
                self.assertEqual(report.removed, 0)
                # The Server keeps its Reservation link, so the lease phase removes the stale lease link.
                self.assertEqual((_owners(both), _row("10.0.0.5").status), ({("owner", "reservation")}, "reserved"))
                self.assertEqual((_owners(leased), _row("10.0.0.6").status), ({("owner", "lease")}, "dhcp"))
                self.assertEqual((_owners(reserved), _row("10.0.0.7").status), ({("owner", "reservation")}, "reserved"))
                # The Reservation link is not the last link of the Server, so only a Reservation cleanup could drop it.
                self.assertEqual(
                    (_owners(still_leased), _row("10.0.0.8").status),
                    ({("owner", "lease"), ("owner", "reservation")}, "active"),
                )

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

    def test_one_phase_that_reports_an_address_with_and_without_a_hostname_takes_the_hostname(self):
        def host(hostname: str, identifier: str) -> dict:
            return {"ip-address": ADDRESS, "flex-id": identifier, "hostname": hostname, "subnet-id": 1}

        cases = {
            "leases": ([_lease(), _lease(hostname="host")], []),
            "leases, the hostname first": ([_lease(hostname="host"), _lease()], []),
            "Reservations": ([], [host("", "host-a"), host("host", "host-b")]),
        }
        for case, (leases, reservations) in cases.items():
            with self.subTest(case):
                NbIP.objects.all().delete()

                report = _reconcile(self.server, leases, reservations)

                self.assertEqual((report.disagreements, report.created), (set(), 1))
                source = "lease" if leases else "reservation"
                self.assertEqual(_row().dns_name, "host")
                self.assertEqual(_links(_row(), source)["owner"].facts, {"hostname": "host", "prefix_length": 24})

    def test_one_phase_that_reports_an_address_with_two_hostnames_disagrees(self):
        report = _reconcile(self.server, [_lease(hostname="host-a"), _lease(hostname="host-b")])

        self.assertEqual((report.disagreements, report.created), ({ADDRESS}, 0))
        self.assertFalse(NbIP.objects.exists())

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
class IPAMPhaseConcurrencyTest(TransactionTestCase):
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

    def test_upgrade_competing_claims_move_one_locked_global_row(self):
        vrf = VRF.objects.create(name="shared-upgrade")
        first = _server("first", sync_vrf=vrf)
        second = _server("second", sync_vrf=vrf)
        legacy = NbIP.objects.create(address=f"{ADDRESS}/32", description="[kea-sync: lease]")
        holder = self._hold("SELECT id FROM ipam_ipaddress WHERE id = %s FOR UPDATE", [legacy.pk])
        with _kea():
            self._start("first", lambda: claim(first, 4, [_lease()], force=False))
            self._wait_for_lock_waits(1)
            self._start("second", lambda: claim(second, 4, [_lease()], force=False))
            self._wait_for_lock_waits(2)
            holder.commit()
            self._join()
        for result in self.results.values():
            self.assertIsInstance(result, ClaimResult)
            self.assertEqual(result.primary.pk, legacy.pk)
        legacy.refresh_from_db()
        self.assertEqual(legacy.vrf_id, vrf.pk)
        self.assertEqual(set(_links(legacy)), {"first", "second"})
        self.assertTrue(all(link.adopted for link in _links(legacy).values()))

    def test_upgrade_concurrent_job_and_import_merge_completion_receipts(self):
        from django.apps import apps

        from netbox_kea.tests.test_views_dhcp_plugin import _sync_responses
        from netbox_kea.views.dhcp_plugin_sync import run_dhcp_plugin_import

        if not apps.is_installed("netbox_dhcp"):
            self.skipTest("netbox_dhcp not installed")
        server = _server("shared-upgrade", sync_dhcp_plugin_enabled=True)
        holder = self._hold("SELECT id FROM netbox_kea_server WHERE id = %s FOR UPDATE", [server.pk])
        responses = _sync_responses({4: {"subnet4": []}}, {4: []})
        responses["lease4-get-page"] = _lease_page([])
        with stub_kea(responses):
            self._start("job", lambda: KeaIpamSyncJob(_make_job()).run(server_pk=server.pk))
            self._start("import", lambda: run_dhcp_plugin_import(server))
            self._wait_for_lock_waits(2)
            holder.commit()
            self._join()
        for result in self.results.values():
            self.assertNotIsInstance(result, BaseException)
        server.refresh_from_db()
        self.assertEqual(set(server.ipam_initial_observations), {"job", "import"})
        self.assertIsNotNone(server.ipam_first_complete_at)

    def test_cleanup_retains_ip_and_link_when_operator_moves_address_while_it_waits(self):
        server = _server("owner")
        _reconcile(server, [_lease("198.18.0.42")])
        ip = _row("198.18.0.42")
        before = list(IPAMOwnershipLink.objects.values())
        operator = self._hold("UPDATE ipam_ipaddress SET address = %s WHERE id = %s", ["198.18.0.43/24", ip.pk])
        with _kea():
            phases = _phases(server)
            self._start("cleanup", lambda: reconcile(server, 4, phases))
            self._wait_for_lock_waits(1)
            operator.commit()
            self._join()
        ip.refresh_from_db()
        self.assertEqual(str(ip.address), "198.18.0.43/24")
        self.assertEqual(list(IPAMOwnershipLink.objects.values()), before)
        report = self._report("cleanup")
        self.assertEqual((report.errors, report.removed), (1, 0))
        self.assertIn("lease", report.incomplete)

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

    def test_network_link_confirmed_while_cleanup_waits_for_object_lock_survives(self):
        from ipam.models import IPRange, Prefix

        from netbox_kea.ipam_reconciliation import PoolPhase, SubnetPhase, read_catalogue
        from netbox_kea.tests.test_prefix_pool_reconciliation import SUBNET as NETWORK_SUBNET

        for model, lock_sql, field, phase_type in (
            (Prefix, "SELECT id FROM ipam_prefix WHERE id = %s FOR UPDATE", "prefix", SubnetPhase),
            (IPRange, "SELECT id FROM ipam_iprange WHERE id = %s FOR UPDATE", "ip_range", PoolPhase),
        ):
            with self.subTest(field=field):
                server = _server(f"confirm-{field}", sync_deprecate_prefixes_and_ranges=True)
                with stub_kea(_catalogue_responses_for_subnets(4, [NETWORK_SUBNET])):
                    reconcile(server, 4, [phase_type(read_catalogue(server, 4))])
                obj = model.objects.get()
                link = IPAMOwnershipLink.objects.get(**{field: obj})
                before = model.objects.values().get(pk=obj.pk)
                holder = self._hold(lock_sql, [obj.pk])

                with stub_kea(_catalogue_responses_for_subnets(4, [])):
                    phase = phase_type(read_catalogue(server, 4))
                    self._start(field, lambda server=server, phase=phase: reconcile(server, 4, [phase]))
                    self._wait_for_lock_waits(1)
                    holder.cursor.execute(
                        "UPDATE netbox_kea_ipamownershiplink SET confirmation = nextval(%s) WHERE id = %s "
                        "RETURNING confirmation",
                        [CONFIRMATION_SEQUENCE, link.pk],
                    )
                    confirmation = holder.cursor.fetchone()[0]
                    holder.commit()
                    self._join()

                link.refresh_from_db()
                report = self._report(field)
                self.assertGreater(confirmation, phase.observation.cutoff)
                self.assertEqual(link.confirmation, confirmation)
                self.assertIsNone(link.stale_mark)
                self.assertEqual((report.complete, report.deprecated, report.removed), (True, 0, 0))
                self.assertEqual(model.objects.values().get(pk=obj.pk), before)

    def test_network_identity_changed_while_cleanup_waits_is_not_deprecated(self):
        from ipam.models import IPRange, Prefix

        from netbox_kea.ipam_reconciliation import PoolPhase, SubnetPhase, read_catalogue
        from netbox_kea.tests.test_prefix_pool_reconciliation import SUBNET as NETWORK_SUBNET

        for model, lock_sql, update_sql, field, phase_type, assignment, value in (
            (
                Prefix,
                "SELECT id FROM ipam_prefix WHERE id = %s FOR UPDATE",
                "UPDATE ipam_prefix SET prefix = %s WHERE id = %s",
                "prefix",
                SubnetPhase,
                "prefix",
                "198.18.1.0/24",
            ),
            (
                IPRange,
                "SELECT id FROM ipam_iprange WHERE id = %s FOR UPDATE",
                "UPDATE ipam_iprange SET end_address = %s, size = 21 WHERE id = %s",
                "ip_range",
                PoolPhase,
                "end_address",
                "198.18.0.30/24",
            ),
        ):
            with self.subTest(field=field):
                server = _server(f"changed-{field}", sync_deprecate_prefixes_and_ranges=True)
                with stub_kea(_catalogue_responses_for_subnets(4, [NETWORK_SUBNET])):
                    reconcile(server, 4, [phase_type(read_catalogue(server, 4))])
                obj = model.objects.get()
                link = IPAMOwnershipLink.objects.values().get(**{field: obj})
                holder = self._hold(lock_sql, [obj.pk])

                with stub_kea(_catalogue_responses_for_subnets(4, [])):
                    phase = phase_type(read_catalogue(server, 4))
                    self._start(field, lambda server=server, phase=phase: reconcile(server, 4, [phase]))
                    self._wait_for_lock_waits(1)
                    holder.cursor.execute(update_sql, [value, obj.pk])
                    holder.commit()
                    self._join()

                obj.refresh_from_db()
                report = self._report(field)
                self.assertEqual((report.prefix_errors, report.deprecated, report.removed), (1, 0, 0))
                self.assertEqual(report.incomplete, {phase.source})
                self.assertEqual(str(getattr(obj, assignment)), value)
                self.assertEqual(obj.status, "active")
                self.assertEqual(IPAMOwnershipLink.objects.values().get(pk=link["id"]), link)

    def _claim_result(self, name: str) -> ClaimResult:
        result = self.results[name]
        if not isinstance(result, ClaimResult):
            self.fail(f"{name} did not return claim results: {result!r}")
        return result

    def test_subnet_and_delegated_prefix_claims_share_one_identity_lock(self):
        import ipaddress

        from netbox_kea.ipam_reconciliation import DelegatedPrefixPhase, SubnetClaim
        from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot

        first, second = _server("subnet-owner"), _server("delegated-owner")
        network = ipaddress.ip_network("2001:db8:100::/56")
        observation = _reservation_snapshot(
            {"subnet6": []}, 6, [{"subnet-id": 0, "duid": "01:02:03", "prefixes": [str(network)]}]
        )
        holder = self._hold("LOCK TABLE ipam_prefix IN SHARE MODE", [])
        self._start("subnet", lambda: claim(first, 6, [SubnetClaim(network)], force=False))
        self._start(
            "delegated",
            lambda: reconcile(
                second, 6, [DelegatedPrefixPhase(observation.snapshot.records, observation.cutoff, True)]
            ),
        )
        self._wait_for_lock_waits(2)
        holder.commit()
        self._join()
        prefix = Prefix.objects.get(prefix=str(network))
        self.assertEqual(
            set(IPAMOwnershipLink.objects.filter(prefix=prefix).values_list("source", flat=True)),
            {"subnet", "delegated-prefix"},
        )
        self.assertEqual(self._report("delegated").errors, 0)
        self.assertIn(self._claim_result("subnet").prefixes[str(network)].outcome, {"created", "updated", "unchanged"})

    def test_claim_and_reconcile_create_one_row_under_the_same_identity_lock(self):
        first, second = _server("claim-owner"), _server("reconcile-owner")
        holder = self._hold("LOCK TABLE ipam_ipaddress IN SHARE MODE", [])
        with _kea([_lease(hostname="host")]):
            self._start("claim", lambda: claim(first, 4, [_lease(hostname="host")], force=False))
            self._start("reconcile", lambda: reconcile(second, 4, [_lease_phase()]))
            self._wait_for_lock_waits(2)
            holder.commit()
            self._join()
        self.assertEqual(NbIP.objects.filter(address__net_host=ADDRESS).count(), 1)
        self.assertEqual(set(_links(_row())), {"claim-owner", "reconcile-owner"})
        outcome = self._claim_result("claim").addresses[ADDRESS].outcome
        self.assertIn(outcome, {"created", "unchanged"})
        self.assertEqual(int(outcome == "created") + self._report("reconcile").created, 1)
        self.assertEqual(self._report("reconcile").disagreements, set())

    def test_claim_confirmation_after_reconcile_cutoff_survives_cleanup(self):
        server = _server("owner")
        _reconcile(server, [_lease(hostname="host")])
        ip = _row()
        before = _links(ip)["owner"].confirmation
        holder = self._hold("SELECT id FROM ipam_ipaddress WHERE id = %s FOR UPDATE", [ip.pk])
        with _kea():
            phases = _phases(server)
            self._start("claim", lambda: claim(server, 4, [_lease(hostname="host")], force=False))
            self._wait_for_lock_waits(1)
            self._start("cleanup", lambda: reconcile(server, 4, phases))
            self._wait_for_lock_waits(2)
            holder.commit()
            self._join()
        self.assertEqual(self._claim_result("claim").addresses[ADDRESS].outcome, "unchanged")
        self.assertEqual(self._report("cleanup").removed, 0)
        self.assertTrue(NbIP.objects.filter(pk=ip.pk).exists())
        self.assertGreater(_links(ip)["owner"].confirmation, before)

    def test_claim_reads_operator_release_from_the_locked_row_after_commit(self):
        server = _server("owner")
        _reconcile(server, [_lease(hostname="host")])
        ip = _row()
        operator = self._hold("UPDATE ipam_ipaddress SET description = 'Printer on floor 2' WHERE id = %s", [ip.pk])
        with _kea():
            self._start("claim", lambda: claim(server, 4, [_lease(hostname="renamed")], force=False))
            self._wait_for_lock_waits(1)
            operator.commit()
            committed = NbIP.objects.values().get(pk=ip.pk)
            self._join()
        self.assertEqual(self._claim_result("claim").addresses[ADDRESS].outcome, "conflict")
        self.assertEqual(_links(ip), {})
        self.assertEqual(NbIP.objects.values().get(pk=ip.pk), committed)
        self.assertEqual(_row().description, "Printer on floor 2")

    def test_claim_row_lock_error_does_not_fail_other_addresses_or_clean_up(self):
        server = _server("owner")
        _reconcile(server, [_lease("10.0.0.1", "one"), _lease("10.0.0.2", "two"), _lease("10.0.0.9", "stale")])
        blocked = _row("10.0.0.1")
        before = NbIP.objects.values().get(pk=blocked.pk)
        self._hold("SELECT id FROM ipam_ipaddress WHERE id = %s FOR UPDATE", [blocked.pk])

        def run():
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '500ms'")
            return claim(server, 4, [_lease("10.0.0.1", "one-b"), _lease("10.0.0.2", "two-b")], force=False)

        with _kea():
            self._start("claim", run)
            self._wait_for_lock_waits(1)
            self._join()
        outcomes = self._claim_result("claim").addresses
        self.assertEqual(
            {address: result.outcome for address, result in outcomes.items()},
            {"10.0.0.1": "error", "10.0.0.2": "updated"},
        )
        self.assertEqual(NbIP.objects.values().get(pk=blocked.pk), before)
        self.assertEqual(_row("10.0.0.2").dns_name, "two-b")
        self.assertEqual(set(_links(_row("10.0.0.9"))), {"owner"})
