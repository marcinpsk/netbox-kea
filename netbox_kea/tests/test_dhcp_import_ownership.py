# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Ownership and failure boundaries through the real DHCP import adapter."""

from __future__ import annotations

import ast
import ipaddress
import unittest
from dataclasses import replace
from pathlib import Path

from django.apps import apps
from django.db import connection
from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase, override_settings
from ipam.models import VRF, IPAddress, IPRange, Prefix
from netaddr import IPNetwork

from netbox_kea.integrations import dhcp_plugin
from netbox_kea.ipam_reconciliation import (
    DelegatedPrefixPhase,
    PoolClaim,
    SubnetClaim,
    claim,
    reconcile,
)
from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config
from netbox_kea.models import IPAMOwnershipLink, next_confirmation_number
from netbox_kea.pools import parse_pool
from netbox_kea.views.dhcp_plugin_sync import _summary_problems, run_dhcp_plugin_import

from .kea_stub import stub_kea
from .test_integration_dhcp_plugin import _reservation_snapshot
from .test_views_dhcp_plugin import _sync_responses
from .utils import _make_db_server, plugins_config


@override_settings(PLUGINS_CONFIG=plugins_config())
class ImportOwnershipTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not apps.is_installed("netbox_dhcp"):
            raise unittest.SkipTest("netbox_dhcp not installed")
        super().setUpClass()

    def setUp(self):
        self.server = _make_db_server(name="import-owner", dhcp4=False, dhcp6=True)
        self.config = {"subnet6": [{"id": 1, "subnet": "2001:db8:1::/64"}]}
        self.identity = {"subnet-id": 1, "duid": "01:02:03:04", "hostname": "delegated.example"}
        self.network = "2001:db8:100::/56"

    def test_complete_import_only_server_records_initial_completion(self):
        self.server.sync_enabled = False
        self.server.sync_dhcp_plugin_enabled = True
        self.server.save()
        with stub_kea(_sync_responses({6: self.config}, {6: []})):
            results = run_dhcp_plugin_import(self.server)
        self.assertEqual(results[0][1].errors, 0)
        self.server.refresh_from_db()
        self.assertIsNotNone(self.server.ipam_first_complete_at)
        self.assertIn("import", self.server.ipam_initial_observations)

    def test_handled_curated_global_prefix_does_not_block_complete_import(self):
        self.server.sync_enabled = False
        self.server.sync_dhcp_plugin_enabled = True
        self.server.save()
        Prefix.objects.create(prefix=self.network, description="Operator delegation")
        hosts = [{"subnet-id": 0, "duid": "01:02:03:04", "prefixes": [self.network]}]
        with stub_kea(_sync_responses({6: self.config}, {6: hosts})):
            results = run_dhcp_plugin_import(self.server)
        self.assertEqual(results[0][1].errors, 0)
        self.server.refresh_from_db()
        self.assertIsNotNone(self.server.ipam_first_complete_at)

    def test_missing_enabled_family_does_not_record_import_completion(self):
        self.server.dhcp4 = True
        self.server.sync_enabled = False
        self.server.sync_dhcp_plugin_enabled = True
        self.server.save()
        responses = _sync_responses({6: self.config}, {6: []})
        responses["subnet4-list"] = {"result": 1, "text": "unavailable"}
        with stub_kea(responses):
            run_dhcp_plugin_import(self.server)
        self.server.refresh_from_db()
        self.assertIsNone(self.server.ipam_first_complete_at)
        self.assertEqual(self.server.ipam_initial_observations, {})

    def test_successful_config_response_without_family_block_does_not_complete_import(self):
        self.server.dhcp4 = True
        self.server.sync_enabled = False
        self.server.sync_dhcp_plugin_enabled = True
        self.server.save()
        for arguments in ({}, {"Dhcp4": None}):
            with self.subTest(arguments=arguments):
                responses = _sync_responses({6: self.config}, {6: []})
                complete_config = responses["config-get"]

                def config_get(body, missing=arguments, healthy=complete_config):
                    if body["service"] == ["dhcp4"]:
                        return {"result": 0, "arguments": missing}
                    return healthy(body)

                responses["config-get"] = config_get
                with stub_kea(responses):
                    results = run_dhcp_plugin_import(self.server)
                self.assertEqual([family for family, _summary in results], [6])
                self.assertEqual(results[0][1].errors, 0)
                self.assertTrue(
                    IPAMOwnershipLink.objects.filter(server=self.server, family=6, source="subnet").exists()
                )
                self.server.refresh_from_db()
                self.assertIsNone(self.server.ipam_first_complete_at)
                self.assertEqual(self.server.ipam_initial_observations, {})
        with stub_kea(_sync_responses({4: {"subnet4": []}, 6: self.config}, {4: [], 6: []})):
            run_dhcp_plugin_import(self.server)
        self.server.refresh_from_db()
        self.assertIsNotNone(self.server.ipam_first_complete_at)
        self.assertEqual(set(self.server.ipam_initial_observations), {"import"})

    def test_job_before_import_cannot_release_an_unknown_import_owner(self):
        from netbox_kea.jobs import KeaIpamSyncJob
        from netbox_kea.tests.test_jobs import _lease_page, _make_job

        self.server.sync_dhcp_plugin_enabled = True
        self.server.sync_deprecate_prefixes_and_ranges = True
        self.server.save()
        prefix = Prefix.objects.create(prefix="2001:db8:1::/64", description="[kea-sync: subnet]")

        def run_job(config):
            responses = _sync_responses({6: config}, {6: []})
            responses["lease6-get-page"] = _lease_page([])
            with stub_kea(responses):
                KeaIpamSyncJob(_make_job()).run(server_pk=self.server.pk)

        run_job(self.config)
        run_job({"subnet6": []})
        self.server.refresh_from_db()
        prefix.refresh_from_db()
        self.assertIsNone(self.server.ipam_first_complete_at)
        self.assertEqual(prefix.status, "active")
        self.assertTrue(IPAMOwnershipLink.objects.filter(prefix=prefix).exists())
        with stub_kea(_sync_responses({6: {"subnet6": []}}, {6: []})):
            run_dhcp_plugin_import(self.server)
        self.server.refresh_from_db()
        self.assertIsNotNone(self.server.ipam_first_complete_at)
        run_job({"subnet6": []})
        prefix.refresh_from_db()
        self.assertEqual(prefix.status, "deprecated")

    def test_complementary_partial_imports_never_combine_into_completion(self):
        self.server.dhcp4 = True
        self.server.sync_enabled = False
        self.server.sync_dhcp_plugin_enabled = True
        self.server.save()
        configs = {4: {"subnet4": []}, 6: {"subnet6": []}}
        for failed in (4, 6):
            responses = _sync_responses(configs, {4: [], 6: []})
            responses[f"subnet{failed}-list"] = {"result": 1, "text": "unavailable"}
            with stub_kea(responses):
                run_dhcp_plugin_import(self.server)
            self.server.refresh_from_db()
            self.assertIsNone(self.server.ipam_first_complete_at)
            self.assertEqual(self.server.ipam_initial_observations, {})
        with stub_kea(_sync_responses(configs, {4: [], 6: []})):
            run_dhcp_plugin_import(self.server)
        self.server.refresh_from_db()
        self.assertIsNotNone(self.server.ipam_first_complete_at)

    def test_failed_ownership_row_blocks_whole_import_receipt(self):
        self.server.sync_enabled = False
        self.server.sync_dhcp_plugin_enabled = True
        self.server.save()
        for _ in range(2):
            Prefix.objects.create(prefix="2001:db8:1::/64", description="[kea-sync: subnet]")
        with stub_kea(_sync_responses({6: self.config}, {6: []})):
            results = run_dhcp_plugin_import(self.server)
        self.assertGreater(results[0][1].errors, 0)
        self.server.refresh_from_db()
        self.assertIsNone(self.server.ipam_first_complete_at)
        self.assertEqual(self.server.ipam_initial_observations, {})

    def test_import_only_owner_protects_addresses_prefixes_and_ranges(self):
        from netbox_kea.jobs import KeaIpamSyncJob
        from netbox_kea.tests.test_jobs import _lease_page, _make_job

        owner = _make_db_server(name="periodic-owner", dhcp6=False, sync_deprecate_prefixes_and_ranges=True)
        unknown = _make_db_server(name="import-only", dhcp6=False, sync_enabled=False, sync_dhcp_plugin_enabled=True)
        address = IPAddress.objects.create(address="10.0.0.25/24", description="[kea-sync: lease]")
        prefix = Prefix.objects.create(prefix="10.0.0.0/24", description="[kea-sync: subnet]")
        ip_range = IPRange.objects.create(
            start_address=IPNetwork("10.0.0.20/24"),
            end_address=IPNetwork("10.0.0.30/24"),
            description="[kea-sync: pool]",
        )
        config = {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.20-10.0.0.30"}]}]}

        def run_job(configuration, leases):
            responses = _sync_responses({4: configuration}, {4: []})
            responses["lease4-get-page"] = _lease_page(leases)
            job = _make_job()
            with stub_kea(responses):
                KeaIpamSyncJob(job).run(server_pk=owner.pk)
            return job.data["summary"][0]

        run_job(config, [{"ip-address": "10.0.0.25", "subnet-id": 1, "valid-lft": 3600, "state": 0}])
        waiting = run_job({"subnet4": []}, [])
        self.assertEqual(waiting["waiting"], 3)
        for obj in (address, prefix, ip_range):
            obj.refresh_from_db()
            self.assertNotEqual(obj.status, "deprecated")
        self.assertEqual(IPAMOwnershipLink.objects.filter(server=owner).count(), 3)
        with stub_kea(_sync_responses({4: {"subnet4": []}}, {4: []})):
            run_dhcp_plugin_import(unknown)
        run_job({"subnet4": []}, [])
        self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())
        for obj in (prefix, ip_range):
            obj.refresh_from_db()
            self.assertEqual(obj.status, "deprecated")

    def import_hosts(self, hosts, *, complete=True, server=None):
        observation = _reservation_snapshot(self.config, 6, hosts)
        if not complete:
            observation = replace(observation, snapshot=replace(observation.snapshot, complete=False))
        return dhcp_plugin.import_server_config(server or self.server, parse_dhcp_config(self.config, 6), observation)

    def delegated(self):
        return Prefix.objects.get(prefix=self.network)

    def test_shared_subnet_and_delegated_prefix_keeps_marker_and_repeat_import_is_unchanged(self):
        self.network = self.config["subnet6"][0]["subnet"]
        hosts = [{**self.identity, "prefixes": [self.network]}]
        first = self.import_hosts(hosts)
        prefix = self.delegated()
        self.assertEqual(first.errors, 0, first.warnings)
        self.assertEqual(prefix.description, "[kea-sync: subnet]")
        self.assertEqual(
            set(IPAMOwnershipLink.objects.filter(prefix=prefix).values_list("source", flat=True)),
            {"subnet", "delegated-prefix"},
        )
        before = Prefix.objects.values().get(pk=prefix.pk)
        second = self.import_hosts(hosts)
        self.assertEqual(second.errors, 0, second.warnings)
        self.assertEqual(Prefix.objects.values().get(pk=prefix.pk), before)

    def test_delegated_writer_ignores_subnet_links_without_live_facts(self):
        for state in ("stale", "factless"):
            with self.subTest(state=state):
                self.network = "2001:db8:100::/56" if state == "stale" else "2001:db8:200::/56"
                claim(self.server, 6, [SubnetClaim(ipaddress.ip_network(self.network))], force=False)
                link = IPAMOwnershipLink.objects.get(prefix=self.delegated(), source="subnet")
                if state == "stale":
                    link.stale_mark = next_confirmation_number()
                else:
                    link.facts = None
                link.save()
                summary = self.import_hosts([{**self.identity, "prefixes": [self.network]}])
                self.assertEqual(summary.errors, 0, summary.warnings)
                self.assertEqual(self.delegated().description, "[kea-sync: delegated prefix]")

    def test_delegated_cleanup_preserves_live_subnet_marker(self):
        self.network = self.config["subnet6"][0]["subnet"]
        self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        summary = self.import_hosts([self.identity])
        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(self.delegated().description, "[kea-sync: subnet]")
        self.assertEqual(
            list(IPAMOwnershipLink.objects.filter(prefix=self.delegated()).values_list("source", flat=True)), ["subnet"]
        )

    def test_curated_delegated_prefix_is_attached_unchanged_and_warns(self):
        prefix = Prefix.objects.create(prefix=self.network, description="Operator delegation")
        before = Prefix.objects.values().get(pk=prefix.pk)
        summary = self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(Prefix.objects.values().get(pk=prefix.pk), before)
        self.assertFalse(IPAMOwnershipLink.objects.filter(prefix=prefix).exists())
        self.assertTrue(
            apps.get_model("netbox_dhcp", "HostReservation").objects.get().ipv6_prefixes.filter(pk=prefix.pk).exists()
        )
        self.assertIn(
            f"delegated prefix {self.network}: IPAM ownership conflict, Prefix left unchanged", summary.warnings
        )

    def test_duplicate_subnet_prefix_reports_error_and_imports_healthy_sibling(self):
        network = self.config["subnet6"][0]["subnet"]
        Prefix.objects.create(prefix=network)
        Prefix.objects.create(prefix=network)
        self.config["subnet6"].append({"id": 2, "subnet": "2001:db8:2::/64"})
        summary = self.import_hosts([])
        self.assertEqual((summary.errors, summary.subnets_created), (1, 1))
        self.assertEqual(str(apps.get_model("netbox_dhcp", "Subnet").objects.get().prefix.prefix), "2001:db8:2::/64")
        self.assertFalse(IPAMOwnershipLink.objects.filter(prefix__prefix=network).exists())

    def test_snapshot_disagreement_creates_no_address_and_reports_one_object(self):
        self.config["subnet6"].append({"id": 2, "subnet": "2001:db8:1::/80"})
        hosts = [
            {**self.identity, "ip-addresses": ["2001:db8:1::10"]},
            {**self.identity, "subnet-id": 2, "duid": "01:02:03:05", "ip-addresses": ["2001:db8:1::10"]},
        ]
        summary = self.import_hosts(hosts)
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.filter(source="reservation").exists())
        self.assertEqual(summary.owner_disagreements, 1)
        self.assertEqual(summary.foreign_addresses_skipped, 0)
        self.assertIn("1 IPAM owner disagreement(s)", " ".join(_summary_problems(summary)))

    def test_snapshot_disagreement_preserves_existing_facts_and_new_owner_has_no_facts(self):
        address = "2001:db8:1::10"
        self.import_hosts([{**self.identity, "ip-addresses": [address]}])
        ip = IPAddress.objects.get(address__net_host=address)
        original = IPAddress.objects.values().get(pk=ip.pk)
        own = IPAMOwnershipLink.objects.get(ip_address=ip)
        facts = own.facts
        self.config["subnet6"].append({"id": 2, "subnet": "2001:db8:1::/80"})
        hosts = [
            {**self.identity, "ip-addresses": [address]},
            {**self.identity, "subnet-id": 2, "duid": "01:02:03:05", "ip-addresses": [address]},
        ]
        summary = self.import_hosts(hosts)
        own.refresh_from_db()
        self.assertEqual(own.facts, facts)
        self.assertEqual(IPAddress.objects.values().get(pk=ip.pk), original)
        second = _make_db_server(name="second-import-owner", dhcp4=False, dhcp6=True)
        other = self.import_hosts(hosts, server=second)
        self.assertIsNone(IPAMOwnershipLink.objects.get(server=second, ip_address=ip).facts)
        self.assertEqual((summary.owner_disagreements, other.owner_disagreements), (1, 1))
        self.assertEqual(IPAddress.objects.values().get(pk=ip.pk), original)

    def test_complete_delegated_phase_deprecates_only_after_dropped_reference(self):
        self.server.sync_deprecate_prefixes_and_ranges = True
        self.server.save()
        first = self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        prefix = self.delegated()
        link = IPAMOwnershipLink.objects.get(prefix=prefix, source="delegated-prefix")
        self.assertEqual(link.server, self.server)
        self.assertEqual(first.errors, 0)
        second = self.import_hosts([self.identity])
        prefix.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(second.errors, 0, second.warnings)
        self.assertEqual(prefix.status, "deprecated")
        self.assertIsNotNone(link.stale_mark)
        self.assertFalse(apps.get_model("netbox_dhcp", "HostReservation").objects.get().ipv6_prefixes.exists())

    def test_delegated_cleanup_without_opt_in_keeps_prefix_and_drops_link(self):
        self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        prefix = self.delegated()
        self.import_hosts([self.identity])
        prefix.refresh_from_db()
        self.assertEqual(prefix.status, "active")
        self.assertFalse(IPAMOwnershipLink.objects.filter(prefix=prefix, source="delegated-prefix").exists())

    def test_incomplete_snapshot_preserves_dropped_prefix_link(self):
        self.server.sync_deprecate_prefixes_and_ranges = True
        self.server.save()
        self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        prefix = self.delegated()
        before = IPAMOwnershipLink.objects.values().get(prefix=prefix)
        self.import_hosts([self.identity], complete=False)
        self.assertEqual(IPAMOwnershipLink.objects.values().get(prefix=prefix), before)
        prefix.refresh_from_db()
        self.assertEqual(prefix.status, "active")

    def test_failed_reservation_keeps_stale_delegated_link_and_healthy_sibling_imports(self):
        self.server.sync_deprecate_prefixes_and_ranges = True
        self.server.save()
        common = ":".join(["aa"] * 83)
        failed = {**self.identity, "duid": common + ":01"}
        self.import_hosts([{**failed, "prefixes": [self.network]}])
        before = IPAMOwnershipLink.objects.values().get(prefix=self.delegated())
        summary = self.import_hosts([{**failed, "duid": common + ":02"}, self.identity])
        self.assertEqual(summary.errors, 1, summary.warnings)
        self.assertEqual(summary.reservations_created, 1)
        self.assertEqual(IPAMOwnershipLink.objects.values().get(prefix=self.delegated()), before)
        self.assertEqual(self.delegated().status, "active")

    def test_skipped_reservation_preserves_stale_delegated_link(self):
        self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        before = IPAMOwnershipLink.objects.values().get(prefix=self.delegated())
        conf = {"subnet6": [{"id": 9, "subnet": "2001:db8:9::/64"}]}
        observation = _reservation_snapshot(conf, 6, [{**self.identity, "subnet-id": 9}])
        summary = dhcp_plugin.ImportSummary()
        dhcp_plugin.import_reservation_snapshot(self.server, None, observation, None, summary)
        self.assertEqual(summary.reservations_skipped, 1)
        self.assertEqual(IPAMOwnershipLink.objects.values().get(prefix=self.delegated()), before)

    def test_foreign_dhcp_reference_prevents_delegated_deprecation(self):
        self.server.sync_deprecate_prefixes_and_ranges = True
        self.server.save()
        self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        prefix = self.delegated()
        HostReservation = apps.get_model("netbox_dhcp", "HostReservation")
        imported = HostReservation.objects.get()
        other = HostReservation.objects.create(name="manual-reservation", subnet=imported.subnet, duid="01:03:04")
        other.ipv6_prefixes.add(prefix)
        self.import_hosts([self.identity])
        prefix.refresh_from_db()
        self.assertEqual(prefix.status, "active")
        self.assertFalse(IPAMOwnershipLink.objects.filter(prefix=prefix, source="delegated-prefix").exists())
        self.assertTrue(other.ipv6_prefixes.filter(pk=prefix.pk).exists())

    def test_failed_delegated_claim_preserves_attachment_and_imports_healthy_sibling(self):
        self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        original = self.delegated()
        before = IPAMOwnershipLink.objects.values().get(prefix=original)
        HostReservation = apps.get_model("netbox_dhcp", "HostReservation")
        reservation = HostReservation.objects.get()
        Prefix.objects.create(prefix=self.network, vrf=original.vrf)
        healthy_prefix = "2001:db8:200::/56"
        healthy = {
            **self.identity,
            "duid": "01:02:03:05",
            "hostname": "healthy.example",
            "prefixes": [healthy_prefix],
        }

        summary = self.import_hosts([{**self.identity, "prefixes": [self.network]}, healthy])

        self.assertEqual(summary.errors, 1, summary.warnings)
        self.assertEqual(list(reservation.ipv6_prefixes.values_list("pk", flat=True)), [original.pk])
        self.assertTrue(
            any("existing reported Prefix attachments were retained" in warning for warning in summary.warnings)
        )
        self.assertEqual(IPAMOwnershipLink.objects.values().get(prefix=original), before)
        sibling = HostReservation.objects.get(hostname="healthy.example")
        self.assertEqual([str(prefix.prefix) for prefix in sibling.ipv6_prefixes.all()], [healthy_prefix])
        self.assertTrue(
            IPAMOwnershipLink.objects.filter(prefix__prefix=healthy_prefix, source="delegated-prefix").exists()
        )

    def test_failed_attachment_rolls_back_new_prefix_and_stale_cleanup(self):
        self.server.sync_deprecate_prefixes_and_ranges = True
        self.server.save()
        self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        old = self.delegated()
        before = IPAMOwnershipLink.objects.values().get(prefix=old)
        HostReservation = apps.get_model("netbox_dhcp", "HostReservation")
        obj = HostReservation.objects.get()
        through = HostReservation.ipv6_prefixes.through
        owner_field = next(
            field for field in through._meta.fields if field.is_relation and field.related_model is HostReservation
        )
        table = connection.ops.quote_name(through._meta.db_table)
        column = connection.ops.quote_name(owner_field.column)
        constraint = connection.ops.quote_name("reject_delegated_attachment")
        # An actual CHECK failure at the M2M INSERT tests the outer phase transaction.
        obj.ipv6_prefixes.clear()
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({column} <> %s)", [obj.pk])
        try:
            new_prefix = "2001:db8:200::/56"
            summary = self.import_hosts([{**self.identity, "prefixes": [new_prefix]}])
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        self.assertEqual(summary.errors, 1, summary.warnings)
        self.assertFalse(Prefix.objects.filter(prefix=new_prefix).exists())
        self.assertEqual(IPAMOwnershipLink.objects.values().get(prefix=old), before)
        old.refresh_from_db()
        self.assertEqual(old.status, "active")

    def test_reconfirmation_during_catalogue_read_survives_empty_delegated_import(self):
        self.import_hosts([{**self.identity, "prefixes": [self.network]}])
        observation = _reservation_snapshot(self.config, 6, [{**self.identity, "prefixes": [self.network]}])
        prefix = self.delegated()
        responses = _sync_responses({6: self.config}, {6: []})
        original_read = responses["subnet6-list"]
        confirmations = []

        def confirm(body):
            report = reconcile(
                self.server, 6, [DelegatedPrefixPhase(observation.snapshot.records, next_confirmation_number(), False)]
            )
            self.assertEqual(report.errors, 0)
            confirmations.append(IPAMOwnershipLink.objects.get(prefix=prefix).confirmation)
            return original_read

        responses["subnet6-list"] = confirm
        with stub_kea(responses):
            results = run_dhcp_plugin_import(self.server)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1].errors, 0)
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(IPAMOwnershipLink.objects.get(prefix=prefix).confirmation, confirmations[0])

    def test_address_pool_prefix_and_global_attachment_stay_in_server_vrf(self):
        self.config["subnet6"][0]["pools"] = [{"pool": "2001:db8:1::10-2001:db8:1::20"}]
        for name in ("first", "second"):
            server = _make_db_server(name=name, sync_vrf=VRF.objects.create(name=name))
            summary = self.import_hosts(
                [{**self.identity, "ip-addresses": ["2001:db8:1::15"], "prefixes": [self.network]}], server=server
            )
            self.assertEqual(summary.errors, 0, summary.warnings)
            objects = IPAMOwnershipLink.objects.filter(server=server).select_related("ip_address", "prefix", "ip_range")
            self.assertEqual(objects.count(), 4)
            self.assertEqual(
                {(link.ip_address or link.prefix or link.ip_range).vrf_id for link in objects}, {server.sync_vrf_id}
            )
        self.assertEqual((IPAddress.objects.count(), IPRange.objects.count(), Prefix.objects.count()), (2, 2, 4))

    def test_failed_required_mac_rolls_back_one_address_and_keeps_healthy_sibling(self):
        from dcim.models import MACAddress

        hardware = "aa:bb:cc:00:00:01"
        MACAddress.objects.create(mac_address=hardware)
        MACAddress.objects.create(mac_address=hardware)
        failed = {"subnet-id": 1, "hw-address": hardware, "ip-addresses": ["2001:db8:1::10"]}
        healthy = {**self.identity, "ip-addresses": ["2001:db8:1::11"]}
        summary = self.import_hosts([failed, healthy])
        self.assertEqual((summary.errors, summary.reservations_created), (1, 1))
        self.assertFalse(IPAddress.objects.filter(address__net_host="2001:db8:1::10").exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address__address__net_host="2001:db8:1::11").exists())

    def test_mac_duplicate_after_claim_does_not_repeat_lookup_during_attachment(self):
        from dcim.models import MACAddress

        hardware = "aa:bb:cc:00:00:01"
        claimed = []

        def duplicate_after_create(sender, instance, created, **kwargs):
            if created and not claimed:
                claimed.append(instance.pk)
                MACAddress.objects.create(mac_address=instance.mac_address)

        post_save.connect(duplicate_after_create, sender=MACAddress)
        try:
            summary = self.import_hosts(
                [
                    {"subnet-id": 1, "hw-address": hardware, "ip-addresses": ["2001:db8:1::10"]},
                    {**self.identity, "ip-addresses": ["2001:db8:1::11"]},
                ]
            )
        finally:
            post_save.disconnect(duplicate_after_create, sender=MACAddress)
        self.assertEqual(MACAddress.objects.filter(mac_address=hardware).count(), 2)
        self.assertEqual((summary.errors, summary.reservations_created), (0, 2), summary.warnings)
        reservation = apps.get_model("netbox_dhcp", "HostReservation").objects.get(hw_address_id=claimed[0])
        self.assertEqual(str(reservation.ipv6_addresses.get().address), "2001:db8:1::10/64")
        self.assertEqual(IPAMOwnershipLink.objects.filter(source="reservation").count(), 2)

    def test_shared_address_returns_each_hardware_and_hostname_pair(self):
        from dcim.models import MACAddress

        hosts = [
            {
                "subnet-id": 1,
                "hw-address": "AA:BB:CC:00:00:01",
                "hostname": "first.example",
                "ip-addresses": ["2001:db8:1::10"],
            },
            {
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:00:02",
                "hostname": "second.example",
                "ip-addresses": ["2001:db8:1::10"],
            },
            {
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:00:01",
                "hostname": "third.example",
                "ip-addresses": ["2001:db8:1::11"],
            },
        ]
        observation = _reservation_snapshot(self.config, 6, hosts)
        result = claim(self.server, 6, observation.snapshot.records, force=False)
        first = result.addresses["2001:db8:1::10"]
        self.assertEqual(
            set(first.resolved_macs), {("aa:bb:cc:00:00:01", "first.example"), ("aa:bb:cc:00:00:02", "second.example")}
        )
        for (hardware, hostname), mac in first.resolved_macs.items():
            self.assertEqual(mac.pk, MACAddress.objects.get(mac_address=hardware).pk)
            self.assertIn(hostname, mac.description)
        third = result.addresses["2001:db8:1::11"]
        self.assertEqual(set(third.resolved_macs), {("aa:bb:cc:00:00:01", "third.example")})
        self.assertIn("third.example", next(iter(third.resolved_macs.values())).description)

    def test_multiaddress_mac_failure_is_not_hidden_by_an_earlier_resolved_mac(self):
        from dcim.models import MACAddress

        hardware = "aa:bb:cc:00:00:01"
        claimed = []

        def duplicate_after_create(sender, instance, created, **kwargs):
            if created and not claimed:
                claimed.append(instance.pk)
                MACAddress.objects.create(mac_address=instance.mac_address)

        post_save.connect(duplicate_after_create, sender=MACAddress)
        try:
            summary = self.import_hosts(
                [
                    {"subnet-id": 1, "hw-address": hardware, "ip-addresses": ["2001:db8:1::10", "2001:db8:1::11"]},
                    {**self.identity, "ip-addresses": ["2001:db8:1::12"]},
                ]
            )
        finally:
            post_save.disconnect(duplicate_after_create, sender=MACAddress)
        self.assertEqual((summary.errors, summary.reservations_created), (1, 1), summary.warnings)
        self.assertFalse(
            apps.get_model("netbox_dhcp", "HostReservation").objects.filter(hw_address_id=claimed[0]).exists()
        )
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address__address__net_host="2001:db8:1::10").exists())
        self.assertFalse(IPAddress.objects.filter(address__net_host="2001:db8:1::11").exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address__address__net_host="2001:db8:1::12").exists())

    def test_curated_prefix_and_range_are_attached_without_changes_and_reported(self):
        prefix = Prefix.objects.create(prefix="2001:db8:1::/64", description="Operator prefix")
        ip_range = IPRange.objects.create(
            start_address=IPNetwork("2001:db8:1::10/64"),
            end_address=IPNetwork("2001:db8:1::20/64"),
            description="Operator range",
        )
        prefix_before = Prefix.objects.values().get(pk=prefix.pk)
        range_before = IPRange.objects.values().get(pk=ip_range.pk)
        self.config["subnet6"][0]["pools"] = [{"pool": "2001:db8:1::10-2001:db8:1::20"}]

        summary = self.import_hosts([])

        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(Prefix.objects.values().get(pk=prefix.pk), prefix_before)
        self.assertEqual(IPRange.objects.values().get(pk=ip_range.pk), range_before)
        self.assertFalse(IPAMOwnershipLink.objects.exists())
        self.assertEqual(apps.get_model("netbox_dhcp", "Subnet").objects.get().prefix_id, prefix.pk)
        self.assertEqual(apps.get_model("netbox_dhcp", "Pool").objects.get().ip_range_id, ip_range.pk)
        self.assertTrue(any("Prefix left unchanged" in warning for warning in summary.warnings))
        self.assertTrue(any("IP Range left unchanged" in warning for warning in summary.warnings))

    def test_ipv6_pool_larger_than_netbox_range_limit_is_skipped(self):
        self.config["subnet6"][0]["pools"] = [{"pool": "2001:db8:1::/64"}]

        summary = self.import_hosts([])

        self.assertEqual((summary.errors, summary.subnets_created, summary.pools_created), (0, 1, 0))
        self.assertFalse(IPRange.objects.exists())
        self.assertTrue(any("unusable range, skipped" in warning for warning in summary.warnings))


@override_settings(PLUGINS_CONFIG=plugins_config())
class TypedNetworkClaimTest(TestCase):
    def test_oversized_pool_is_skipped_while_supported_pool_is_claimed(self):
        server = _make_db_server()
        network = ipaddress.ip_network("2001:db8::/64")
        oversized = parse_pool("2001:db8::/64", network)
        supported = parse_pool("2001:db8::10-2001:db8::20", network)

        result = claim(server, 6, [PoolClaim(oversized, network), PoolClaim(supported, network)], force=False)

        self.assertEqual(set(result.ranges), {"2001:db8::10 - 2001:db8::20"})
        self.assertEqual(result.ranges["2001:db8::10 - 2001:db8::20"].outcome, "created")
        ip_range = IPRange.objects.get()
        self.assertEqual(str(ip_range.start_address), "2001:db8::10/64")
        self.assertEqual(str(ip_range.end_address), "2001:db8::20/64")
        self.assertEqual(IPAMOwnershipLink.objects.get().ip_range_id, ip_range.pk)

    def test_pool_phase_refuses_catalogue_without_configuration_before_writes(self):
        from netbox_kea.ipam_reconciliation import PoolPhase, read_catalogue

        from .kea_stub import _catalogue_responses_for_subnets

        server = _make_db_server()
        with stub_kea(
            _catalogue_responses_for_subnets(
                4, [{"id": 1, "subnet": "198.18.0.0/24", "pools": [{"pool": "198.18.0.10-198.18.0.20"}]}]
            )
        ):
            observation = read_catalogue(server, 4)
        catalogue = observation.catalogue
        self.assertIsNotNone(catalogue)
        invalid = replace(catalogue, subnets=(replace(catalogue.subnets[0], configuration=None),))
        with self.assertRaisesMessage(ValueError, "complete catalogue must include every Subnet configuration"):
            reconcile(server, 4, [PoolPhase(replace(observation, catalogue=invalid))])
        self.assertFalse(IPRange.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

        report = reconcile(server, 4, [PoolPhase(observation)])
        self.assertTrue(report.complete)
        self.assertEqual(report.created, 1)
        self.assertEqual(IPAMOwnershipLink.objects.get().ip_range_id, IPRange.objects.get().pk)

    def test_failed_delegated_claim_preserves_stale_links_and_claims_other_prefixes(self):
        server = _make_db_server(sync_deprecate_prefixes_and_ranges=True)
        stale = "2001:db8:100::/56"
        duplicate = "2001:db8:200::/56"
        valid = "2001:db8:300::/56"
        initial = _reservation_snapshot({"subnet6": []}, 6, [{"subnet-id": 0, "duid": "01:02:03", "prefixes": [stale]}])
        control = reconcile(server, 6, [DelegatedPrefixPhase(initial.snapshot.records, initial.cutoff, True)])
        self.assertTrue(control.complete)
        self.assertEqual(control.prefixes[stale].outcome, "created")
        before = IPAMOwnershipLink.objects.values().get(prefix__prefix=stale)
        Prefix.objects.create(prefix=duplicate)
        Prefix.objects.create(prefix=duplicate)
        duplicates_before = list(Prefix.objects.filter(prefix=duplicate).order_by("pk").values())
        observation = _reservation_snapshot(
            {"subnet6": []}, 6, [{"subnet-id": 0, "duid": "01:02:03", "prefixes": [duplicate, valid]}]
        )

        report = reconcile(server, 6, [DelegatedPrefixPhase(observation.snapshot.records, observation.cutoff, True)])

        self.assertEqual(report.errors, 1)
        self.assertEqual(report.incomplete, {"delegated-prefix"})
        self.assertEqual(report.prefixes[duplicate].outcome, "error")
        self.assertEqual(report.prefixes[valid].outcome, "created")
        self.assertEqual(list(Prefix.objects.filter(prefix=duplicate).order_by("pk").values()), duplicates_before)
        self.assertFalse(IPAMOwnershipLink.objects.filter(prefix__prefix=duplicate).exists())
        self.assertEqual(IPAMOwnershipLink.objects.values().get(prefix__prefix=stale), before)
        self.assertEqual(Prefix.objects.get(prefix=stale).status, "active")

    def test_pool_outside_claimed_subnet_fails_before_writing(self):
        server = _make_db_server()
        network = ipaddress.ip_network("198.18.0.0/24")
        pool = parse_pool("198.18.1.10-198.18.1.20", ipaddress.ip_network("198.18.1.0/24"))
        with self.assertRaisesMessage(ValueError, "Pool must be contained"):
            claim(server, 4, [PoolClaim(pool, network)], force=False)
        self.assertFalse(IPRange.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_shared_pool_with_different_subnet_lengths_reports_disagreement(self):
        server = _make_db_server()
        network = ipaddress.ip_network("198.18.0.0/24")
        pool = parse_pool("198.18.0.10-198.18.0.20", network)
        result = claim(
            server, 4, [PoolClaim(pool, network), PoolClaim(pool, ipaddress.ip_network("198.18.0.0/25"))], force=False
        )
        self.assertEqual(result.ranges["198.18.0.10 - 198.18.0.20"].outcome, "disagreement")
        self.assertFalse(IPRange.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_wrong_network_family_fails_before_writing(self):
        server = _make_db_server()
        with self.assertRaisesMessage(ValueError, "network does not match"):
            claim(server, 4, [SubnetClaim(ipaddress.ip_network("2001:db8:1::/64"))], force=False)
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_reservation_followed_by_network_fails_before_writing(self):
        server = _make_db_server()
        observation = _reservation_snapshot(
            {"subnet6": [{"id": 1, "subnet": "2001:db8:1::/64"}]},
            6,
            [{"subnet-id": 1, "duid": "01:02:03", "ip-addresses": ["2001:db8:1::10"]}],
        )
        with self.assertRaisesMessage(ValueError, "homogeneous"):
            claim(
                server,
                6,
                [observation.snapshot.records[0], SubnetClaim(ipaddress.ip_network("2001:db8:1::/64"))],
                force=False,
            )
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_wrong_delegated_phase_family_fails_before_writing(self):
        server = _make_db_server()
        observation = _reservation_snapshot(
            {"subnet6": []}, 6, [{"subnet-id": 0, "duid": "01:02:03", "prefixes": ["2001:db8:100::/56"]}]
        )
        with self.assertRaisesMessage(ValueError, "Reservation does not match"):
            reconcile(server, 4, [DelegatedPrefixPhase(observation.snapshot.records, observation.cutoff, True)])
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_force_claim_preserves_curated_network_notes(self):
        server = _make_db_server()
        network = ipaddress.ip_network("198.18.0.0/24")
        prefix = Prefix.objects.create(prefix=str(network), description="Operator note")
        pool = parse_pool("198.18.0.10-198.18.0.20", network)
        ip_range = IPRange.objects.create(
            start_address=IPNetwork("198.18.0.10/24"), end_address=IPNetwork("198.18.0.20/24"), description="Pool note"
        )
        first = claim(server, 4, [SubnetClaim(network)], force=True)
        second = claim(server, 4, [PoolClaim(pool, network)], force=True)
        prefix.refresh_from_db()
        ip_range.refresh_from_db()
        self.assertEqual(prefix.description, "[kea-sync: subnet] Operator note")
        self.assertEqual(ip_range.description, "[kea-sync: pool] Pool note")
        self.assertEqual(first.prefixes[str(network)].prefix.pk, prefix.pk)
        self.assertEqual(second.ranges["198.18.0.10 - 198.18.0.20"].ip_range.pk, ip_range.pk)

    def test_mixed_sources_fail_before_writing_any_network(self):
        server = _make_db_server()
        network = ipaddress.ip_network("198.18.0.0/24")
        with self.assertRaisesMessage(ValueError, "homogeneous"):
            claim(
                server,
                4,
                [SubnetClaim(network), PoolClaim(parse_pool("198.18.0.10-198.18.0.20", network), network)],
                force=False,
            )
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPRange.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())


class AdapterOwnershipBoundaryTest(SimpleTestCase):
    def test_adapter_imports_no_private_sync_helpers(self):
        source = Path(dhcp_plugin.__file__).read_text()
        imported = [
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.split(".")[-1] == "sync"
            for alias in node.names
        ]
        self.assertLessEqual(set(imported), {"sync_mac_address"})
