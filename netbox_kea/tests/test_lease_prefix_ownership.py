# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Live delegated-prefix Lease ownership (#294): Prefixes from Current PD Leases, and the repair of PD-as-IP links.

The tests run the real job, claim and reconcile interfaces with the real ORM and KeaClient; only
requests.Session.post is stubbed.
"""

from __future__ import annotations

from contextlib import suppress

from core.exceptions import JobFailed
from django.test import TestCase, override_settings
from django.utils import timezone
from ipam.models import VRF, Prefix
from ipam.models import IPAddress as NbIP

from netbox_kea.ipam_reconciliation import (
    _LOCK_CLASS,
    LeasePhase,
    LeasePrefixPhase,
    SubnetPhase,
    _int4,
    claim,
    read_catalogue,
    read_leases,
    reconcile,
)
from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.models import CONFIRMATION_SEQUENCE, IPAMOwnershipLink, Server, SyncConfig, next_confirmation_number
from netbox_kea.tests.kea_stub import _catalogue_responses_for_subnets, lease_record, stub_kea, typed_lease
from netbox_kea.tests.test_ipam_reconciliation import ConcurrencyHarness
from netbox_kea.tests.test_jobs import _make_job, _patch_kea
from netbox_kea.tests.test_models import _MigrationTestCase
from netbox_kea.tests.utils import _make_db_server, plugins_config

PD = "2001:db8:1:100::"
PD_NETWORK = f"{PD}/56"
ADDRESS6 = "2001:db8::10"
# The Subnet of the job stub's DHCPv6 leases.
SUBNET6 = {"id": 1, "subnet": "2001:db8::/64"}


def _server(name: str = "owner", **fields):
    return _make_db_server(name=name, ca_url=f"https://{name}.example.com", dhcp4=False, **fields)


def _pd(prefix: str = PD, length: int = 56, **changes) -> dict:
    return lease_record(prefix, type="IA_PD", prefix_len=length, **{"subnet_id": 1, **changes})


def _address(address: str = ADDRESS6, **changes) -> dict:
    return lease_record(address, **{"subnet_id": 1, **changes})


def _run_job(server, leases6: list[dict]) -> dict:
    """Run the job for *server* against a DHCPv6 Kea with *leases6* and no Reservations; return its summary."""
    job = _make_job()
    with _patch_kea(leases6=leases6), suppress(JobFailed):
        KeaIpamSyncJob(job).run(server_pk=server.pk)
    return job.data["summary"][0]


def _legacy_pd_ip(server, *, vrf=None, prefix_length: int = 64, **link) -> NbIP:
    """Create the IP address that the lease source made of a delegated prefix before #294, with its lease link."""
    ip = NbIP.objects.create(address=f"{PD}/{prefix_length}", vrf=vrf, status="dhcp", description="[kea-sync: lease]")
    IPAMOwnershipLink.objects.create(
        server=server,
        family=6,
        source="lease",
        ip_address=ip,
        facts={"hostname": "", "prefix_length": prefix_length},
        confirmation=next_confirmation_number(),
        **link,
    )
    return ip


def _sources(obj) -> set[tuple[str, str]]:
    field = "prefix" if isinstance(obj, Prefix) else "ip_address"
    return set(IPAMOwnershipLink.objects.filter(**{field: obj}).values_list("server__name", "source"))


@override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
class LivePrefixLeaseJobTest(TestCase):
    def test_a_current_delegated_prefix_becomes_a_prefix_in_the_server_vrf_and_never_an_ip_address(self):
        vrf = VRF.objects.create(name="pd-vrf")
        server = _server(sync_vrf=vrf)
        summary = _run_job(server, [_pd(), _address()])

        self.assertEqual((summary["errors"], summary["prefix_errors"]), (0, 0))
        prefix = Prefix.objects.get(prefix=PD_NETWORK)
        self.assertEqual(
            (prefix.vrf, prefix.status, prefix.description), (vrf, "active", "[kea-sync: delegated prefix]")
        )
        self.assertEqual(_sources(prefix), {("owner", "lease-prefix")})
        self.assertFalse(NbIP.objects.filter(address__net_host=PD).exists())
        self.assertEqual(
            IPAMOwnershipLink.objects.get(ip_address__address__net_host=ADDRESS6).allocation_kind, "address"
        )

    def test_live_prefix_sync_needs_the_lease_and_the_prefix_flags(self):
        for name, server_fields, global_fields in (
            ("no-server-prefixes", {"sync_prefixes_enabled": False}, {}),
            ("no-global-prefixes", {}, {"sync_prefixes_enabled": False}),
            ("no-server-leases", {"sync_leases_enabled": False}, {}),
        ):
            with self.subTest(name):
                SyncConfig.objects.filter(pk=1).update(sync_prefixes_enabled=True, sync_leases_enabled=True)
                SyncConfig.objects.filter(pk=1).update(**global_fields)
                server = _server(name, **server_fields)
                _run_job(server, [_pd()])
                self.assertFalse(Prefix.objects.filter(prefix=PD_NETWORK).exists())
                self.assertFalse(NbIP.objects.filter(address__net_host=PD).exists())
                server.refresh_from_db()
                self.assertNotIn([6, "lease-prefix"], server.ipam_initial_observations["job"]["sources"])

    def test_an_inactive_delegated_prefix_or_address_owns_nothing(self):
        server = _server()
        # Kea states: 2 is expired-reclaimed, 3 is released.
        _run_job(server, [_pd(state=2), _address(state=3)])
        self.assertFalse(Prefix.objects.filter(prefix=PD_NETWORK).exists())
        self.assertFalse(NbIP.objects.filter(address__net_host=ADDRESS6).exists())

    def test_an_address_whose_lease_stops_being_current_loses_its_ip_on_a_complete_run(self):
        server = _server()
        _run_job(server, [_address()])
        self.assertTrue(NbIP.objects.filter(address__net_host=ADDRESS6).exists())
        _run_job(server, [_address(state=3)])
        self.assertFalse(NbIP.objects.filter(address__net_host=ADDRESS6).exists())

    def test_a_dropped_delegated_prefix_keeps_the_prefix_and_deprecates_it_only_with_the_opt_in(self):
        for opt_in, status in ((False, "active"), (True, "deprecated")):
            with self.subTest(opt_in=opt_in):
                Prefix.objects.all().delete()
                server = _server(f"opt-in-{opt_in}", sync_deprecate_prefixes_and_ranges=opt_in)
                _run_job(server, [_pd()])
                _run_job(server, [])
                prefix = Prefix.objects.get(prefix=PD_NETWORK)
                self.assertEqual(prefix.status, status)
                self.assertEqual(bool(_sources(prefix)), opt_in)

    def test_live_and_imported_delegated_prefix_owners_never_clean_up_each_other(self):
        from netbox_kea.ipam_reconciliation import DelegatedPrefixPhase

        server = _server(sync_deprecate_prefixes_and_ranges=True)
        _run_job(server, [_pd()])
        prefix = Prefix.objects.get(prefix=PD_NETWORK)
        IPAMOwnershipLink.objects.create(
            server=server,
            family=6,
            source="delegated-prefix",
            prefix=prefix,
            facts={"prefix_length": 56},
            confirmation=next_confirmation_number(),
        )

        _run_job(server, [])
        prefix.refresh_from_db()
        self.assertEqual(_sources(prefix), {("owner", "delegated-prefix")})
        self.assertEqual(prefix.status, "active")

        _run_job(server, [_pd()])
        report = reconcile(server, 6, [DelegatedPrefixPhase([], next_confirmation_number(), complete=True)])
        self.assertEqual(report.errors + report.prefix_errors, 0)
        self.assertEqual(_sources(prefix), {("owner", "lease-prefix")})
        self.assertTrue(Prefix.objects.filter(pk=prefix.pk).exists())

    def test_a_shared_live_prefix_survives_one_owner_becoming_stale(self):
        first, second = _server("first"), _server("second")
        for server in (first, second):
            _run_job(server, [_pd()])
        _run_job(first, [])
        prefix = Prefix.objects.get(prefix=PD_NETWORK)
        self.assertEqual(_sources(prefix), {("second", "lease-prefix")})

    def test_an_operator_prefix_without_the_marker_stays_untouched(self):
        server = _server()
        operator = Prefix.objects.create(prefix=PD_NETWORK, description="Customer site")
        summary = _run_job(server, [_pd()])
        operator.refresh_from_db()
        self.assertEqual(operator.description, "Customer site")
        self.assertFalse(IPAMOwnershipLink.objects.filter(prefix=operator).exists())
        self.assertEqual(summary["conflicts"], 1)

    def test_a_delegated_prefix_of_an_unknown_subnet_fails_its_row_and_blocks_cleanup(self):
        server = _server(sync_deprecate_prefixes_and_ranges=True)
        _run_job(server, [_pd("2001:db8:2:100::")])
        summary = _run_job(server, [_pd(subnet_id=99)])
        self.assertEqual(summary["prefix_errors"], 1)
        self.assertFalse(Prefix.objects.filter(prefix=PD_NETWORK).exists())
        kept = Prefix.objects.get(prefix="2001:db8:2:100::/56")
        self.assertEqual((kept.status, _sources(kept)), ("active", {("owner", "lease-prefix")}))


@override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
class LegacyPrefixLeaseRepairTest(TestCase):
    """IP addresses that the lease source made of delegated prefixes before #294."""

    def test_a_successful_prefix_claim_retires_the_legacy_ip_through_the_normal_cleanup(self):
        server = _server()
        ip = _legacy_pd_ip(server)
        summary = _run_job(server, [_pd()])

        self.assertEqual((summary["errors"], summary["prefix_errors"], summary["unclassified"]), (0, 0, 0))
        self.assertEqual(_sources(Prefix.objects.get(prefix=PD_NETWORK)), {("owner", "lease-prefix")})
        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())

        again = _run_job(server, [_pd()])
        self.assertEqual((again["created"], again["updated"], again["errors"], again["prefix_errors"]), (0, 0, 0, 0))
        self.assertEqual(Prefix.objects.filter(prefix=PD_NETWORK).count(), 1)

    def test_the_repair_follows_the_configured_stale_ip_cleanup(self):
        for mode, exists, status in (("none", True, "dhcp"), ("deprecate", True, "deprecated")):
            with self.subTest(mode), override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup=mode)):
                NbIP.objects.all().delete()
                Prefix.objects.all().delete()
                server = _server(f"mode-{mode}")
                ip = _legacy_pd_ip(server)
                _run_job(server, [_pd()])
                _run_job(server, [_pd()])
                ip = NbIP.objects.filter(pk=ip.pk).first()
                self.assertEqual(ip is not None, exists)
                self.assertEqual(ip.status, status)
                self.assertTrue(Prefix.objects.filter(prefix=PD_NETWORK).exists())

    def test_the_repair_keeps_another_owner_of_the_legacy_ip(self):
        server, other = _server(), _server("other")
        ip = _legacy_pd_ip(server)
        IPAMOwnershipLink.objects.create(
            server=other,
            family=6,
            source="reservation",
            ip_address=ip,
            facts={"hostname": "", "prefix_length": 64},
            confirmation=next_confirmation_number(),
        )
        _run_job(server, [_pd()])
        ip.refresh_from_db()
        self.assertEqual(_sources(ip), {("other", "reservation")})
        self.assertEqual(ip.status, "reserved")

    def test_a_failed_disabled_or_partial_replacement_keeps_the_legacy_ip(self):
        cases = (
            ("conflict", {}, [_pd()]),
            ("disabled", {"sync_prefixes_enabled": False}, [_pd()]),
            ("partial", {}, [_pd(), lease_record("2001:db8::99", subnet_id=1, state=99)]),
        )
        for name, fields, leases in cases:
            with self.subTest(name):
                NbIP.objects.all().delete()
                Prefix.objects.all().delete()
                if name == "conflict":
                    Prefix.objects.create(prefix=PD_NETWORK, description="Operator prefix")
                server = _server(name, **fields)
                ip = _legacy_pd_ip(server)
                _run_job(server, leases)
                ip.refresh_from_db()
                link = IPAMOwnershipLink.objects.get(ip_address=ip)
                self.assertEqual((link.source, link.allocation_kind, link.stale_mark), ("lease", "", None))
                self.assertEqual(ip.status, "dhcp")

    def test_an_unclassified_link_absent_from_kea_is_kept_and_reported_until_an_operator_releases_it(self):
        server = _server()
        ip = _legacy_pd_ip(server)
        for _ in range(2):
            summary = _run_job(server, [])
            self.assertEqual((summary["unclassified"], summary["errors"]), (1, 0))
            self.assertEqual(_sources(ip), {("owner", "lease")})

        ip.description = "Released by the operator"
        ip.save()
        summary = _run_job(server, [])
        self.assertEqual(summary["unclassified"], 0)
        self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=ip).exists())
        self.assertTrue(NbIP.objects.filter(pk=ip.pk).exists())

    def test_a_fresh_address_lease_classifies_an_unclassified_link(self):
        server = _server()
        ip = NbIP.objects.create(address=f"{ADDRESS6}/64", status="dhcp", description="[kea-sync: lease]")
        IPAMOwnershipLink.objects.create(
            server=server,
            family=6,
            source="lease",
            ip_address=ip,
            facts={"hostname": "", "prefix_length": 64},
            confirmation=next_confirmation_number(),
        )
        _run_job(server, [_address()])
        self.assertEqual(IPAMOwnershipLink.objects.get(ip_address=ip).allocation_kind, "address")
        _run_job(server, [])
        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())

    def test_an_inactive_address_lease_classifies_an_unclassified_link_on_a_complete_run(self):
        server = _server()
        ip = NbIP.objects.create(address=f"{ADDRESS6}/64", status="dhcp", description="[kea-sync: lease]")
        IPAMOwnershipLink.objects.create(
            server=server,
            family=6,
            source="lease",
            ip_address=ip,
            facts={"hostname": "", "prefix_length": 64},
            confirmation=next_confirmation_number(),
        )
        # Kea state 3 is released: the address is known, and it is not current.
        summary = _run_job(server, [_address(state=3)])
        self.assertEqual((summary["unclassified"], summary["errors"]), (0, 0))
        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())

    def test_another_owner_claiming_the_address_keeps_an_already_stale_unclassified_link(self):
        server, other = _server(), _server("other")
        ip = _legacy_pd_ip(server, prefix_length=64)
        IPAMOwnershipLink.objects.filter(ip_address=ip).update(stale_mark=next_confirmation_number())
        with stub_kea(_catalogue_responses_for_subnets(6, [{"id": 1, "subnet": "2001:db8:1::/56"}])):
            result = claim(other, 6, [typed_lease(lease_record(PD, subnet_id=1))], force=False)

        self.assertEqual(result.addresses[PD].outcome, "updated")
        self.assertEqual(_sources(ip), {("owner", "lease"), ("other", "lease")})
        self.assertEqual(IPAMOwnershipLink.objects.get(ip_address=ip, server=server).allocation_kind, "")

    def test_the_repair_finds_the_legacy_ip_in_the_vrf_that_the_server_used_before(self):
        old_vrf, new_vrf = VRF.objects.create(name="old"), VRF.objects.create(name="new")
        server = _server(sync_vrf=new_vrf)
        ip = _legacy_pd_ip(server, vrf=old_vrf)
        _run_job(server, [_pd()])
        self.assertEqual(Prefix.objects.get(prefix=PD_NETWORK).vrf, new_vrf)
        self.assertFalse(NbIP.objects.filter(pk=ip.pk).exists())


@override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
class LivePrefixReceiptTest(TestCase):
    def test_an_old_narrower_job_receipt_does_not_attest_to_live_prefix_coverage(self):
        server = _server(sync_deprecate_prefixes_and_ranges=True)
        old_sources = [[6, "lease"], [6, "pool"], [6, "reservation"], [6, "subnet"]]
        # Server.save keeps the stored receipts, so the test writes the receipt that an earlier release published.
        Server.objects.filter(pk=server.pk).update(
            ipam_initial_observations={"job": {"sources": old_sources, "completed_at": timezone.now().isoformat()}},
            ipam_first_complete_at=timezone.now(),
        )
        adopted = Prefix.objects.create(prefix="2001:db8:5::/64", description="[kea-sync: subnet]")
        with stub_kea(_catalogue_responses_for_subnets(6, [SUBNET6, {"id": 5, "subnet": "2001:db8:5::/64"}])):
            reconcile(server, 6, [SubnetPhase(read_catalogue(server, 6))])
        self.assertTrue(IPAMOwnershipLink.objects.get(prefix=adopted).adopted)

        first = _run_job(server, [])
        adopted.refresh_from_db()
        self.assertEqual((first["waiting"], adopted.status), (1, "active"))
        server.refresh_from_db()
        self.assertIn([6, "lease-prefix"], server.ipam_initial_observations["job"]["sources"])

        second = _run_job(server, [])
        adopted.refresh_from_db()
        self.assertEqual((second["waiting"], adopted.status), (0, "deprecated"))

    def test_a_job_whose_live_prefix_phase_failed_records_no_receipt(self):
        server = _server()
        _run_job(server, [_pd(subnet_id=99)])
        server.refresh_from_db()
        self.assertNotIn("job", server.ipam_initial_observations)


class LivePrefixPhaseContractTest(TestCase):
    def test_the_live_prefix_phase_reads_the_observation_of_the_address_lease_phase(self):
        from netbox_kea.ipam_reconciliation import LeaseObservation

        server = _server()
        first = LeaseObservation(None, next_confirmation_number(), None, RuntimeError("not read"))
        second = LeaseObservation(None, next_confirmation_number(), None, RuntimeError("not read"))
        for phases in (
            [LeasePrefixPhase(first, None)],
            [LeasePhase(first, {}), LeasePrefixPhase(second, None)],
        ):
            with self.subTest(len(phases)), self.assertRaises(ValueError):
                reconcile(server, 6, phases)
        with self.assertRaises(ValueError):
            reconcile(server, 4, [LeasePhase(first, {}), LeasePrefixPhase(first, None)])

    def test_a_lease_observation_holds_a_snapshot_or_its_read_failure(self):
        from netbox_kea.ipam_reconciliation import LeaseObservation

        with self.assertRaises(ValueError):
            LeaseObservation(None, next_confirmation_number(), None)

    def test_a_claim_takes_address_or_delegated_prefix_leases_but_not_both(self):
        server = _server()
        with (
            stub_kea(_catalogue_responses_for_subnets(6, [SUBNET6])) as kea,
            self.assertRaises(ValueError),
        ):
            claim(server, 6, [typed_lease(_address()), typed_lease(_pd())], force=False)
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(NbIP.objects.exists())
        self.assertLessEqual(len(kea.commands()), 2)

    def test_a_delegated_prefix_claim_refuses_an_unknown_subnet_before_any_write(self):
        server = _server()
        with stub_kea(_catalogue_responses_for_subnets(6, [SUBNET6])), self.assertRaises(ValueError):
            claim(server, 6, [typed_lease(_pd()), typed_lease(_pd("2001:db8:2:100::", subnet_id=7))], force=False)
        self.assertFalse(Prefix.objects.exists())


@override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
class LivePrefixConcurrencyTest(ConcurrencyHarness):
    """Live delegated-prefix ownership against concurrent writers, each on its own connection."""

    def _job_phases(self, server):
        catalogue = read_catalogue(server, 6).catalogue
        leases = read_leases(server, 6, None)
        return [LeasePhase(leases, {1: 64}), LeasePrefixPhase(leases, catalogue)]

    def test_live_and_imported_delegated_prefix_claims_create_one_shared_prefix(self):
        from netbox_kea.ipam_reconciliation import DelegatedPrefixPhase
        from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot

        live, imported = _server("live-owner"), _server("import-owner")
        observation = _reservation_snapshot(
            {"subnet6": []}, 6, [{"subnet-id": 0, "duid": "01:02:03", "prefixes": [PD_NETWORK]}]
        )
        holder = self._hold("LOCK TABLE ipam_prefix IN SHARE MODE", [])
        with _patch_kea(leases6=[_pd()]):
            self._start("live", lambda: reconcile(live, 6, self._job_phases(live)))
            self._start(
                "import",
                lambda: reconcile(
                    imported, 6, [DelegatedPrefixPhase(observation.snapshot.records, observation.cutoff, True)]
                ),
            )
            self._wait_for_lock_waits(2)
            holder.commit()
            self._join()

        prefix = Prefix.objects.get(prefix=PD_NETWORK)
        self.assertEqual(_sources(prefix), {("live-owner", "lease-prefix"), ("import-owner", "delegated-prefix")})
        self.assertEqual(self._report("live").prefix_errors + self._report("import").prefix_errors, 0)

    def test_an_operator_who_moves_the_legacy_address_during_the_repair_keeps_the_moved_row(self):
        server = _server()
        ip = _legacy_pd_ip(server)
        moved = "2001:db8:9::1/64"
        # The operator's edit holds the row lock; the repair must wait for it, then see the new address.
        holder = self._hold("UPDATE ipam_ipaddress SET address = %s WHERE id = %s", [moved, ip.pk])
        with _patch_kea(leases6=[_pd()]):
            self._start("run", lambda: reconcile(server, 6, self._job_phases(server)))
            self._wait_for_lock_waits(1, finished="run")
            holder.commit()
            self._join()

        self._report("run")
        row = NbIP.objects.filter(pk=ip.pk).first()
        self.assertIsNotNone(row)
        self.assertEqual(str(row.address), moved)
        self.assertEqual(IPAMOwnershipLink.objects.get(ip_address=row).allocation_kind, "")

    def test_an_address_claim_that_confirms_the_link_while_the_repair_waits_keeps_its_address_kind(self):
        server = _server()
        ip = _legacy_pd_ip(server)
        link = IPAMOwnershipLink.objects.get(ip_address=ip)
        # The repair takes the identity lock of the base address after its Prefix claim; an address claim holds it.
        holder = self._hold("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_CLASS, _int4(f"ip-address None {PD}")])
        with _patch_kea(leases6=[_pd()]):
            self._start("run", lambda: reconcile(server, 6, self._job_phases(server)))
            self._wait_for_lock_waits(1)
            holder.cursor.execute(
                "UPDATE netbox_kea_ipamownershiplink SET allocation_kind = 'address', confirmation = nextval(%s)"
                " WHERE id = %s",
                [CONFIRMATION_SEQUENCE, link.pk],
            )
            holder.commit()
            self._join()

        report = self._report("run")
        self.assertEqual((report.errors, report.prefix_errors, report.removed), (0, 0, 0))
        self.assertEqual(IPAMOwnershipLink.objects.get(pk=link.pk).allocation_kind, "address")
        self.assertTrue(NbIP.objects.filter(pk=ip.pk).exists())
        self.assertTrue(Prefix.objects.filter(prefix=PD_NETWORK).exists())


class AllocationKindMigrationTest(_MigrationTestCase):
    """Migration 0023 classifies only what it can prove: a DHCPv4 lease link is an address link."""

    def test_dhcpv4_lease_links_become_address_links_and_dhcpv6_lease_links_stay_unclassified(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        server = _make_db_server(name="released", ca_url="https://released.example.com")
        rows = {
            (4, "lease"): NbIP.objects.create(address="198.18.0.5/24"),
            (6, "lease"): NbIP.objects.create(address=f"{PD}/64"),
            (4, "reservation"): NbIP.objects.create(address="198.18.0.6/24"),
        }
        executor = MigrationExecutor(connection)
        current = executor.loader.graph.leaf_nodes("netbox_kea")

        def restore_migration_state():
            restore = MigrationExecutor(connection)
            restore.loader.build_graph()
            restore.migrate(current)

        self.addCleanup(restore_migration_state)
        executor.migrate([("netbox_kea", "0022_dhcp_import_mapping_history")])
        with connection.cursor() as cursor:
            for (family, source), ip in rows.items():
                cursor.execute(
                    "INSERT INTO netbox_kea_ipamownershiplink"
                    " (server_id, family, source, ip_address_id, facts, confirmation, adopted)"
                    " VALUES (%s, %s, %s, %s, NULL, nextval(%s), false)",
                    [server.pk, family, source, ip.pk, CONFIRMATION_SEQUENCE],
                )
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        forward = MigrationExecutor(connection)
        forward.loader.build_graph()
        forward.migrate(current)

        kinds = {
            (link.family, link.source): link.allocation_kind for link in IPAMOwnershipLink.objects.filter(server=server)
        }
        self.assertEqual(kinds, {(4, "lease"): "address", (6, "lease"): "", (4, "reservation"): ""})
