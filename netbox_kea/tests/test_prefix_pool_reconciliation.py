# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The sync job owns Prefixes and IP Ranges through the real Kea transport and ORM."""

import uuid

from core.models import Job
from django.test import TestCase, override_settings
from ipam.models import VRF, IPRange, Prefix

from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.models import IPAMOwnershipLink, next_confirmation_number
from netbox_kea.tests.kea_stub import _catalogue_responses_for_subnets, stub_kea
from netbox_kea.tests.utils import _make_db_server, plugins_config

SUBNET = {"id": 1, "subnet": "198.18.0.0/24", "pools": [{"pool": "198.18.0.10 - 198.18.0.20"}]}


def run_job(server, subnets=(SUBNET,)):
    job = Job.objects.create(name="Kea IPAM Sync", job_id=uuid.uuid4(), data={})
    with stub_kea(_catalogue_responses_for_subnets(4, list(subnets))):
        KeaIpamSyncJob(job).run(server_pk=server.pk)
    return job.data["summary"][0]


@override_settings(PLUGINS_CONFIG=plugins_config())
class PrefixPoolJobTest(TestCase):
    def test_job_links_prefix_and_pool_in_server_vrf(self):
        vrf = VRF.objects.create(name="sync")
        server = _make_db_server(dhcp6=False, sync_leases_enabled=False, sync_reservations_enabled=False, sync_vrf=vrf)
        summary = run_job(server)
        prefix, pool = Prefix.objects.get(vrf=vrf), IPRange.objects.get(vrf=vrf)
        self.assertEqual((str(prefix.prefix), prefix.status), ("198.18.0.0/24", "active"))
        self.assertEqual((str(pool.start_address), str(pool.end_address)), ("198.18.0.10/24", "198.18.0.20/24"))
        self.assertEqual(
            set(IPAMOwnershipLink.objects.filter(server=server).values_list("source", "prefix_id", "ip_range_id")),
            {("subnet", prefix.pk, None), ("pool", None, pool.pk)},
        )
        self.assertEqual((summary["created"], summary["errors"]), (2, 0))

    def test_removed_subnet_and_pool_keep_objects_and_drop_links_by_default(self):
        server = _make_db_server(dhcp6=False, sync_leases_enabled=False, sync_reservations_enabled=False)
        run_job(server)
        prefix, pool = Prefix.objects.get(), IPRange.objects.get()
        run_job(server, ())
        prefix.refresh_from_db()
        pool.refresh_from_db()
        self.assertEqual((prefix.status, pool.status), ("active", "active"))
        self.assertFalse(IPAMOwnershipLink.objects.filter(server=server).exists())

    def test_opt_in_deprecates_and_restores_prefix_and_pool_with_their_links(self):
        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        run_job(server)
        prefix, pool = Prefix.objects.get(), IPRange.objects.get()
        run_job(server, ())
        prefix.refresh_from_db()
        pool.refresh_from_db()
        self.assertEqual((prefix.status, pool.status), ("deprecated", "deprecated"))
        self.assertEqual(IPAMOwnershipLink.objects.filter(server=server, stale_mark__isnull=False).count(), 2)
        run_job(server)
        prefix.refresh_from_db()
        pool.refresh_from_db()
        self.assertEqual((prefix.status, pool.status), ("active", "active"))
        self.assertEqual(IPAMOwnershipLink.objects.filter(server=server, stale_mark__isnull=True).count(), 2)

    def test_conflicting_pool_masks_confirm_stale_link_without_restoring_its_status(self):
        from netbox_kea.ipam_reconciliation import PoolPhase, read_catalogue, reconcile

        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        run_job(server)
        run_job(server, ())
        pool = IPRange.objects.get()
        link = IPAMOwnershipLink.objects.get(server=server, ip_range=pool)
        mark, facts = link.stale_mark, link.facts
        overlapping = [SUBNET, {**SUBNET, "id": 2, "subnet": "198.18.0.0/25"}]
        with stub_kea(_catalogue_responses_for_subnets(4, overlapping)):
            report = reconcile(server, 4, [PoolPhase(read_catalogue(server, 4))])
        pool.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(report.disagreements, {"198.18.0.10 - 198.18.0.20"})
        self.assertEqual((pool.status, link.stale_mark, link.facts), ("deprecated", mark, facts))
        self.assertGreater(link.confirmation, mark)

    def test_last_dropping_server_decides_for_shared_prefix_and_pool(self):
        for first_flag, last_flag in ((False, True), (True, False)):
            with self.subTest(first=first_flag, last=last_flag):
                Prefix.objects.all().delete()
                IPRange.objects.all().delete()
                first = _make_db_server(
                    name=f"first-{first_flag}",
                    dhcp6=False,
                    sync_leases_enabled=False,
                    sync_reservations_enabled=False,
                    sync_deprecate_prefixes_and_ranges=first_flag,
                )
                last = _make_db_server(
                    name=f"last-{last_flag}",
                    dhcp6=False,
                    sync_leases_enabled=False,
                    sync_reservations_enabled=False,
                    sync_deprecate_prefixes_and_ranges=last_flag,
                )
                run_job(first)
                run_job(last, [{**SUBNET, "id": 42}])
                run_job(first, ())
                self.assertEqual((Prefix.objects.get().status, IPRange.objects.get().status), ("active", "active"))
                self.assertEqual(set(IPAMOwnershipLink.objects.values_list("server_id", flat=True)), {last.pk})
                run_job(last, ())
                expected = "deprecated" if last_flag else "active"
                self.assertEqual((Prefix.objects.get().status, IPRange.objects.get().status), (expected, expected))
                self.assertEqual(IPAMOwnershipLink.objects.count(), 2 if last_flag else 0)

    def test_operator_release_of_prefix_and_range_never_deprecates_them(self):
        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        run_job(server)
        Prefix.objects.update(description="Operator prefix note")
        IPRange.objects.update(description="Operator pool note")
        summary = run_job(server, ())
        self.assertEqual((Prefix.objects.get().status, IPRange.objects.get().status), ("active", "active"))
        self.assertEqual(Prefix.objects.get().description, "Operator prefix note")
        self.assertEqual(IPRange.objects.get().description, "Operator pool note")
        self.assertFalse(IPAMOwnershipLink.objects.exists())

        self.assertEqual(summary["conflicts"], 2)
        self.assertEqual(summary["conflict_sample"], ["198.18.0.0/24", "198.18.0.10 - 198.18.0.20"])

    def test_operator_release_of_deprecated_prefix_and_pool_drops_marked_links(self):
        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        run_job(server)
        run_job(server, ())
        self.assertEqual(IPAMOwnershipLink.objects.filter(stale_mark__isnull=False).count(), 2)
        self.assertEqual((Prefix.objects.get().status, IPRange.objects.get().status), ("deprecated", "deprecated"))
        Prefix.objects.update(description="Operator prefix note", status="reserved")
        IPRange.objects.update(description="Operator pool note", status="reserved")
        prefix = Prefix.objects.values().get()
        pool = IPRange.objects.values().get()

        summary = run_job(server, ())

        self.assertEqual(Prefix.objects.values().get(), prefix)
        self.assertEqual(IPRange.objects.values().get(), pool)
        self.assertFalse(IPAMOwnershipLink.objects.exists())
        self.assertEqual(summary["conflicts"], 2)
        self.assertEqual(summary["conflict_sample"], ["198.18.0.0/24", "198.18.0.10 - 198.18.0.20"])

    def test_blank_descriptions_release_prefix_and_pool_even_while_reported(self):
        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        run_job(server)
        Prefix.objects.update(description="")
        IPRange.objects.update(description="")
        summary = run_job(server)
        self.assertEqual(summary["conflicts"], 2)
        self.assertEqual((Prefix.objects.get().description, IPRange.objects.get().description), ("", ""))
        self.assertEqual((Prefix.objects.get().status, IPRange.objects.get().status), ("active", "active"))
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_failed_pool_row_keeps_stale_pool_links_while_complete_subnet_phase_cleans_up(self):
        from django.db import connection

        from netbox_kea.ipam_reconciliation import PoolPhase, SubnetPhase, read_catalogue, reconcile

        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        old = {"id": 2, "subnet": "198.18.1.0/24", "pools": [{"pool": "198.18.1.10 - 198.18.1.20"}]}
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE ipam_iprange ADD CONSTRAINT reject_test_pool "
                "CHECK (host(start_address) <> '198.18.0.10') NOT VALID"
            )
        run_job(server, [old])
        stale_pool = IPRange.objects.get()
        with stub_kea(_catalogue_responses_for_subnets(4, [SUBNET])):
            observation = read_catalogue(server, 4)
            report = reconcile(server, 4, [SubnetPhase(observation), PoolPhase(observation)])
        self.assertEqual((report.prefix_errors, report.errors, report.incomplete), (1, 0, {"pool"}))
        self.assertEqual(Prefix.objects.get(prefix="198.18.1.0/24").status, "deprecated")
        stale_pool.refresh_from_db()
        self.assertEqual(stale_pool.status, "active")
        self.assertIsNone(IPAMOwnershipLink.objects.get(ip_range=stale_pool).stale_mark)

    def test_another_server_supersedes_unconfirmed_stale_prefix_and_pool_links(self):
        first = _make_db_server(
            name="first",
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        other = _make_db_server(name="other", dhcp6=False, sync_leases_enabled=False, sync_reservations_enabled=False)
        run_job(first)
        run_job(first, ())
        run_job(other)
        self.assertEqual(set(IPAMOwnershipLink.objects.values_list("server_id", flat=True)), {other.pk})
        self.assertEqual((Prefix.objects.get().status, IPRange.objects.get().status), ("active", "active"))
        self.assertFalse(IPAMOwnershipLink.objects.filter(stale_mark__isnull=False).exists())

    def test_subnet_disagreement_keeps_confirmed_stale_link_when_another_owner_reports(self):
        from netbox_kea.ipam_reconciliation import SubnetPhase, read_catalogue, reconcile

        first = _make_db_server(
            name="first",
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        other = _make_db_server(name="other", dhcp6=False, sync_leases_enabled=False, sync_reservations_enabled=False)
        run_job(first)
        run_job(first, ())
        prefix = Prefix.objects.get()
        stale = IPAMOwnershipLink.objects.get(server=first, prefix=prefix)
        mark = stale.stale_mark
        # A Prefix's CIDR fixes its mask. Seed inconsistent persisted owner facts to test the defensive invariant.
        opposing = IPAMOwnershipLink.objects.create(
            server=other,
            family=4,
            source="delegated-prefix",
            prefix=prefix,
            facts={"prefix_length": 25},
            confirmation=next_confirmation_number(),
        )
        with stub_kea(_catalogue_responses_for_subnets(4, [SUBNET])):
            report = reconcile(first, 4, [SubnetPhase(read_catalogue(first, 4))])
        stale.refresh_from_db()
        prefix.refresh_from_db()
        self.assertEqual(report.disagreements, {"198.18.0.0/24"})
        self.assertEqual((prefix.status, stale.stale_mark), ("deprecated", mark))
        self.assertGreater(stale.confirmation, mark)
        opposing.delete()
        run_job(other)
        stale.refresh_from_db()
        prefix.refresh_from_db()
        self.assertEqual(stale.stale_mark, mark)
        self.assertEqual(prefix.status, "active")
        self.assertEqual(
            set(IPAMOwnershipLink.objects.filter(prefix=prefix).values_list("server_id", flat=True)),
            {first.pk, other.pk},
        )

    def test_cleanup_reports_marker_update_from_remaining_delegated_prefix_owner(self):
        from netbox_kea.ipam_reconciliation import SubnetPhase, read_catalogue, reconcile

        server = _make_db_server(dhcp6=False, sync_leases_enabled=False, sync_reservations_enabled=False)
        other = _make_db_server(name="delegated-owner")
        run_job(server)
        prefix = Prefix.objects.get()
        IPAMOwnershipLink.objects.create(
            server=other,
            family=4,
            source="delegated-prefix",
            prefix=prefix,
            facts={"prefix_length": 24},
            confirmation=next_confirmation_number(),
        )
        with stub_kea(_catalogue_responses_for_subnets(4, [])):
            report = reconcile(server, 4, [SubnetPhase(read_catalogue(server, 4))])
        prefix.refresh_from_db()
        self.assertEqual(prefix.description, "[kea-sync: delegated prefix]")
        self.assertEqual((report.updated, report.removed), (1, 0))

    def test_complete_subnet_phase_alone_deprecates_its_last_prefix(self):
        from netbox_kea.ipam_reconciliation import SubnetPhase, read_catalogue, reconcile

        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        run_job(server)
        with stub_kea(_catalogue_responses_for_subnets(4, [])):
            report = reconcile(server, 4, [SubnetPhase(read_catalogue(server, 4))])
        self.assertEqual((report.complete, report.deprecated, report.removed), (True, 1, 0))
        self.assertEqual(Prefix.objects.get().status, "deprecated")
        self.assertEqual(IPRange.objects.get().status, "active")

    def test_confirmation_during_catalogue_request_survives_stale_cleanup(self):
        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        run_job(server)
        responses = _catalogue_responses_for_subnets(4, [])
        original = responses["config-get"]
        confirmations = []

        def confirm_during_snapshot(body):
            for link in IPAMOwnershipLink.objects.filter(server=server):
                link.confirmation = next_confirmation_number()
                link.save(update_fields=["confirmation"])
                confirmations.append(link.confirmation)
            return original

        responses["config-get"] = confirm_during_snapshot
        with stub_kea(responses):
            KeaIpamSyncJob(Job.objects.create(name="Kea IPAM Sync", job_id=uuid.uuid4(), data={})).run(
                server_pk=server.pk
            )
        self.assertEqual(len(confirmations), 2)
        self.assertEqual(IPAMOwnershipLink.objects.filter(server=server, stale_mark__isnull=True).count(), 2)
        self.assertEqual((Prefix.objects.get().status, IPRange.objects.get().status), ("active", "active"))

    def test_two_servers_in_two_vrfs_keep_separate_prefix_and_pool_ownership(self):
        for name in ("first", "second"):
            server = _make_db_server(
                name=name,
                dhcp6=False,
                sync_leases_enabled=False,
                sync_reservations_enabled=False,
                sync_vrf=VRF.objects.create(name=name),
            )
            run_job(server)
            links = IPAMOwnershipLink.objects.filter(server=server).select_related("prefix", "ip_range")
            self.assertEqual({(link.prefix or link.ip_range).vrf_id for link in links}, {server.sync_vrf_id})
        self.assertEqual((Prefix.objects.count(), IPRange.objects.count()), (2, 2))


@override_settings(PLUGINS_CONFIG=plugins_config())
class DeprecationSettingFormTest(TestCase):
    def test_model_form_saves_opt_in_and_opt_out(self):
        from netbox_kea.forms import ServerForm

        server = _make_db_server(dhcp6=False)
        self.assertFalse(server.sync_deprecate_prefixes_and_ranges)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                form = ServerForm(
                    instance=server,
                    data={
                        "name": server.name,
                        "ca_url": server.ca_url,
                        "dhcp4": True,
                        "sync_deprecate_prefixes_and_ranges": enabled,
                    },
                )
                with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "3.0.0"}}}):
                    self.assertTrue(form.is_valid(), form.errors)
                    form.save()
                server.refresh_from_db()
                self.assertEqual(server.sync_deprecate_prefixes_and_ranges, enabled)

    def test_csv_import_round_trips_opt_in_opt_out_and_omitted_default(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from netbox_kea.models import Server

        user = get_user_model().objects.create_superuser(username="importer", password="example-password")
        self.client.force_login(user)
        for name, value in (("enabled", "true"), ("disabled", "false"), ("default", "")):
            with self.subTest(value=value):
                csv = (
                    "name,ca_url,dhcp4,dhcp6,sync_deprecate_prefixes_and_ranges\n"
                    f"{name},https://kea.example.com,true,false,{value}\n"
                )
                with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "3.0.0"}}}):
                    response = self.client.post(
                        reverse("plugins:netbox_kea:server_bulk_import"),
                        {"data": csv, "format": "csv", "csv_delimiter": ","},
                    )
                self.assertEqual(response.status_code, 302, response.content)
                self.assertEqual(Server.objects.get(name=name).sync_deprecate_prefixes_and_ranges, value == "true")


@override_settings(PLUGINS_CONFIG=plugins_config())
class DhcpReferencesTest(TestCase):
    def test_referenced_prefix_and_range_are_not_deprecated(self):
        from django.apps import apps

        from netbox_kea.integrations import dhcp_plugin
        from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config
        from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot

        if not apps.is_installed("netbox_dhcp"):
            self.skipTest("netbox_dhcp is not installed")
        server = _make_db_server(
            dhcp6=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_deprecate_prefixes_and_ranges=True,
        )
        run_job(server)
        config = {"subnet4": [SUBNET]}
        imported = dhcp_plugin.import_server_config(
            server, parse_dhcp_config(config, 4), _reservation_snapshot(config, 4)
        )
        self.assertEqual(imported.errors, 0, imported.warnings)
        prefix, pool = Prefix.objects.get(), IPRange.objects.get()
        self.assertEqual(apps.get_model("netbox_dhcp", "Subnet").objects.get().prefix_id, prefix.pk)
        self.assertEqual(apps.get_model("netbox_dhcp", "Pool").objects.get().ip_range_id, pool.pk)
        run_job(server, ())
        prefix.refresh_from_db()
        pool.refresh_from_db()
        self.assertEqual((prefix.status, pool.status), ("active", "active"))
        self.assertFalse(IPAMOwnershipLink.objects.filter(server=server).exists())
