# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Upgrade adoption through the public ownership and workflow interfaces."""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ipam.models import VRF, IPAddress
from rest_framework.test import APIClient

from netbox_kea.ipam_reconciliation import claim, upgrade_counts
from netbox_kea.models import IPAMOwnershipLink
from netbox_kea.tests.test_ipam_reconciliation import _kea, _lease, _reconcile, _run_job, _server
from netbox_kea.tests.utils import plugins_config


@override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
class UpgradeAdoptionTest(TestCase):
    def test_shared_vrf_moves_legacy_address_with_same_primary_key(self):
        vrf = VRF.objects.create(name="shared")
        server = _server("first", sync_vrf=vrf)
        _server("second", sync_vrf=vrf)
        existing = IPAddress.objects.create(address="10.0.0.5/32", description="Synced from Kea DHCP lease note")
        with _kea():
            result = claim(server, 4, [_lease()], force=False)
        self.assertEqual(result.primary.pk, existing.pk)
        existing.refresh_from_db()
        self.assertEqual(existing.vrf_id, vrf.pk)
        self.assertEqual(existing.description, "[kea-sync: lease] note")
        self.assertTrue(IPAMOwnershipLink.objects.get(ip_address=existing).adopted)

    def test_last_adopted_link_waits_for_unobserved_owner(self):
        server = _server("first")
        _server("second")
        existing = IPAddress.objects.create(address="10.0.0.5/32", description="[kea-sync: lease]")
        _reconcile(server, [_lease()])
        report = _reconcile(server)
        self.assertTrue(IPAddress.objects.filter(pk=existing.pk).exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address=existing).exists())
        self.assertEqual(report.waiting, 1)

    def test_target_collision_keeps_global_marker_unowned_and_reports_conflict(self):
        vrf = VRF.objects.create(name="shared")
        server = _server("first", sync_vrf=vrf)
        legacy = IPAddress.objects.create(address="10.0.0.5/32", description="[kea-sync: lease]")
        target = IPAddress.objects.create(address="10.0.0.5/24", vrf=vrf, description="[kea-sync: lease]")
        report = _reconcile(server, [_lease()])
        self.assertIn("10.0.0.5", report.conflicts)
        legacy.refresh_from_db()
        self.assertIsNone(legacy.vrf_id)
        self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=legacy).exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address=target).exists())

    def test_completion_timestamp_is_visible_but_cannot_be_written_through_rest(self):
        server = _server("first")
        user = get_user_model().objects.create(username="upgrade-operator", is_superuser=True)
        client = APIClient()
        client.force_authenticate(user)
        url = reverse("plugins-api:netbox_kea-api:server-detail", args=[server.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("ipam_first_complete_at", response.data)
        self.assertNotIn("ipam_initial_observations", response.data)
        with _kea(responses={"version-get": {"result": 0, "arguments": {"extended": "test"}}}):
            response = client.patch(url, {"ipam_first_complete_at": timezone.now().isoformat()}, format="json")
        self.assertEqual(response.status_code, 200)
        server.refresh_from_db()
        self.assertIsNone(server.ipam_first_complete_at)

    def test_complete_empty_job_records_completion_and_preserves_preupgrade_stale_row(self):
        server = _server("first")
        stale = IPAddress.objects.create(address="10.0.0.50/24", description="[kea-sync: lease]")
        summary = _run_job(server, [])
        server.refresh_from_db()
        self.assertIsNotNone(server.ipam_first_complete_at)
        self.assertTrue(IPAddress.objects.filter(pk=stale.pk).exists())
        self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=stale).exists())
        self.assertEqual(summary["unowned"], 1)

    def test_global_adoption_keeps_primary_key_and_blank_description_refuses_claim(self):
        server = _server("first")
        legacy = IPAddress.objects.create(address="10.0.0.5/32", description="[kea-sync: lease]")
        blank = IPAddress.objects.create(address="10.0.0.6/32", description="")
        with _kea():
            result = claim(server, 4, [_lease(), _lease("10.0.0.6")], force=False)
        self.assertEqual(result.addresses["10.0.0.5"].ip.pk, legacy.pk)
        self.assertTrue(IPAMOwnershipLink.objects.get(ip_address=legacy).adopted)
        self.assertEqual(result.addresses["10.0.0.6"].outcome, "conflict")
        self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=blank).exists())
        blank.refresh_from_db()
        self.assertEqual(blank.description, "")

    def test_mixed_vrfs_never_move_the_global_row_in_either_order(self):
        first = _server("first", sync_vrf=VRF.objects.create(name="first"))
        second = _server("second", sync_vrf=VRF.objects.create(name="second"))
        for address, order in (("10.0.0.5", (first, second)), ("10.0.0.6", (second, first))):
            legacy = IPAddress.objects.create(address=f"{address}/32", description="[kea-sync: lease]")
            for server in order:
                with _kea():
                    claim(server, 4, [_lease(address)], force=False)
            legacy.refresh_from_db()
            self.assertIsNone(legacy.vrf_id)
            self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address=legacy).exists())
            self.assertEqual(
                set(IPAddress.objects.filter(address__net_host=address).values_list("vrf_id", flat=True)),
                {None, first.sync_vrf_id, second.sync_vrf_id},
            )
            for server in order:
                with _kea():
                    result = claim(server, 4, [_lease(address)], force=False)
                self.assertEqual(
                    result.addresses[address].ip.pk,
                    IPAMOwnershipLink.objects.get(server=server, ip_address__address__net_host=address).ip_address_id,
                )

    def test_adoption_mark_survives_replacing_a_stale_owner(self):
        first = _server("first")
        second = _server("second")
        existing = IPAddress.objects.create(address="10.0.0.5/32", description="[kea-sync: lease]")
        _reconcile(first, [_lease()])
        link = IPAMOwnershipLink.objects.get(ip_address=existing)
        link.stale_mark = link.confirmation
        link.save()
        _reconcile(second, [_lease()])
        remaining = IPAMOwnershipLink.objects.get(ip_address=existing)
        self.assertEqual(remaining.server_id, second.pk)
        self.assertTrue(remaining.adopted)
        _reconcile(second)
        self.assertTrue(IPAddress.objects.filter(pk=existing.pk).exists())

    def test_adoption_barrier_keeps_last_link_in_every_ip_cleanup_mode(self):
        server = _server("first")
        _server("unobserved")
        for index, mode in enumerate(("none", "remove", "deprecate"), start=10):
            address = f"10.0.0.{index}"
            existing = IPAddress.objects.create(address=f"{address}/24", description="[kea-sync: lease]")
            with override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup=mode)):
                _reconcile(server, [_lease(address)])
                _reconcile(server)
            existing.refresh_from_db()
            self.assertEqual(existing.status, "dhcp")
            self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address=existing).exists())

    def test_existing_owner_prevents_global_row_move_after_vrf_change(self):
        first = _server("first")
        second = _server("second")
        legacy = IPAddress.objects.create(address="10.0.0.5/32", description="[kea-sync: lease]")
        _reconcile(first, [_lease()])
        vrf = VRF.objects.create(name="new-shared")
        for server in (first, second):
            server.sync_vrf = vrf
            server.save()
        with _kea():
            result = claim(second, 4, [_lease()], force=False)
        legacy.refresh_from_db()
        self.assertIsNone(legacy.vrf_id)
        self.assertNotEqual(result.primary.pk, legacy.pk)
        self.assertEqual(result.primary.vrf_id, vrf.pk)

    def test_newly_enabled_source_holds_barrier_until_a_complete_run_observes_it(self):
        first = _server("first")
        second = _server("second", sync_reservations_enabled=False)
        _run_job(first, [])
        _run_job(second, [])
        first.refresh_from_db()
        second.refresh_from_db()
        first_complete = second.ipam_first_complete_at
        existing = IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
        _reconcile(first, [_lease()])
        second.sync_reservations_enabled = True
        second.save()
        _reconcile(first)
        self.assertTrue(IPAddress.objects.filter(pk=existing.pk).exists())
        _run_job(second, [])
        _reconcile(first)
        self.assertFalse(IPAddress.objects.filter(pk=existing.pk).exists())
        second.refresh_from_db()
        self.assertEqual(second.ipam_first_complete_at, first_complete)

    def test_counting_more_adopted_objects_does_not_repeat_owner_policy_queries(self):
        server = _server("first")
        _server("unobserved")
        IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
        _reconcile(server, [_lease()])
        with CaptureQueriesContext(connection) as first_queries:
            first = upgrade_counts()
        self.assertEqual(first.waiting, 1)
        for index in range(10, 19):
            IPAddress.objects.create(address=f"10.0.0.{index}/24", description="[kea-sync: lease]")
        _reconcile(server, [_lease(), *[_lease(f"10.0.0.{index}") for index in range(10, 19)]])
        with CaptureQueriesContext(connection) as many_queries:
            many = upgrade_counts()
        self.assertEqual(many.waiting, 10)
        self.assertEqual(len(many_queries), len(first_queries))

    def test_completion_timestamp_appears_in_graphql_and_server_detail(self):
        import json

        server = _server("first")
        completed = timezone.now().replace(microsecond=0)
        server.ipam_first_complete_at = completed
        server.save()
        user = get_user_model().objects.create(username="upgrade-reader", is_superuser=True)
        self.client.force_login(user)
        response = self.client.post(
            reverse("graphql"),
            data=json.dumps({"query": f"query {{ server(id: {server.pk}) {{ ipam_first_complete_at }} }}"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("errors", payload)
        self.assertEqual(payload["data"]["server"]["ipam_first_complete_at"], completed.isoformat())
        response = self.client.post(
            reverse("graphql"),
            data=json.dumps({"query": f"query {{ server(id: {server.pk}) {{ ipam_initial_observations }} }}"}),
            content_type="application/json",
        )
        self.assertIn("errors", response.json())
        response = self.client.get(reverse("plugins:netbox_kea:server", args=[server.pk]))
        self.assertContains(response, "IPAM initial observation completed")
        self.assertEqual(response.context["object"].ipam_first_complete_at, completed)
        self.assertNotContains(response, "ipam_initial_observations")

    def test_complementary_partial_jobs_do_not_complete_the_initial_observation(self):
        from contextlib import suppress

        from core.exceptions import JobFailed

        from netbox_kea.jobs import KeaIpamSyncJob
        from netbox_kea.tests.test_jobs import _make_job, _patch_kea

        server = _server("dual-family")
        server.dhcp6 = True
        server.save()
        lease6 = {"ip-address": "2001:db8::5", "subnet-id": 1, "valid-lft": 3600, "state": 0}
        for failed_family, successful_address in ((6, "10.0.0.5"), (4, "2001:db8::5")):
            job = _make_job()
            with (
                _patch_kea(
                    leases4=[_lease()],
                    leases6=[lease6],
                    responses={f"lease{failed_family}-get-page": {"result": 1, "text": "read unavailable"}},
                ),
                suppress(JobFailed),
            ):
                KeaIpamSyncJob(job).run(server_pk=server.pk)
            self.assertGreater(job.data["summary"][0]["errors"], 0)
            self.assertTrue(
                IPAMOwnershipLink.objects.filter(
                    server=server, ip_address__address__net_host=successful_address
                ).exists()
            )
            server.refresh_from_db()
            self.assertIsNone(server.ipam_first_complete_at)
            self.assertEqual(server.ipam_initial_observations, {})
        with _patch_kea(leases4=[_lease()], leases6=[lease6]):
            KeaIpamSyncJob(_make_job()).run(server_pk=server.pk)
        server.refresh_from_db()
        self.assertIsNotNone(server.ipam_first_complete_at)
        self.assertEqual(set(server.ipam_initial_observations), {"job"})
        self.assertIn([4, "lease"], server.ipam_initial_observations["job"]["sources"])
        self.assertIn([6, "lease"], server.ipam_initial_observations["job"]["sources"])

    def test_unobserved_other_family_does_not_hold_the_address_barrier(self):
        from netbox_kea.tests.utils import _make_db_server

        owner = _server("observed")
        _run_job(owner, [])
        unknown = _make_db_server(name="other-family", dhcp4=False, dhcp6=True)
        address = IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
        _reconcile(owner, [_lease()])
        report = _reconcile(owner)
        self.assertEqual(report.removed, 1)
        self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())
        unknown.refresh_from_db()
        self.assertIsNone(unknown.ipam_first_complete_at)
        self.assertEqual(unknown.ipam_initial_observations, {})
        unknown.dhcp4 = True
        unknown.save()
        relevant = IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
        _reconcile(owner, [_lease()])
        self.assertEqual(_reconcile(owner).waiting, 1)
        self.assertTrue(IPAddress.objects.filter(pk=relevant.pk).exists())

    def test_unobserved_disabled_address_sources_do_not_hold_the_barrier(self):
        owner = _server("observed")
        _run_job(owner, [])
        unknown = _server("network-only", sync_leases_enabled=False, sync_reservations_enabled=False)
        address = IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
        _reconcile(owner, [_lease()])
        report = _reconcile(owner)
        self.assertEqual(report.removed, 1)
        self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())
        unknown.refresh_from_db()
        self.assertIsNone(unknown.ipam_first_complete_at)
        self.assertEqual(unknown.ipam_initial_observations, {})
        unknown.sync_reservations_enabled = True
        unknown.save()
        relevant = IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
        _reconcile(owner, [_lease()])
        self.assertEqual(_reconcile(owner).waiting, 1)
        self.assertTrue(IPAddress.objects.filter(pk=relevant.pk).exists())

    def test_absent_dhcp_plugin_does_not_hold_the_import_barrier(self):
        from django.apps import apps

        owner = _server("observed")
        _run_job(owner, [])
        unknown = _server("import-only", sync_enabled=False, sync_dhcp_plugin_enabled=True)
        available_apps = [config.name for config in apps.get_app_configs() if config.label != "netbox_dhcp"]
        installed = apps.is_installed("netbox_dhcp")
        apps.set_available_apps(available_apps)
        try:
            self.assertFalse(apps.is_installed("netbox_dhcp"))
            address = IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
            _reconcile(owner, [_lease()])
            report = _reconcile(owner)
            self.assertEqual(report.removed, 1)
            self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())
            unknown.refresh_from_db()
            self.assertEqual(unknown.ipam_initial_observations, {})
        finally:
            apps.unset_available_apps()
        if installed:
            self.assertTrue(apps.is_installed("netbox_dhcp"))
            relevant = IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
            _reconcile(owner, [_lease()])
            self.assertEqual(_reconcile(owner).waiting, 1)
            self.assertTrue(IPAddress.objects.filter(pk=relevant.pk).exists())

    def test_malformed_persisted_receipts_fail_closed_without_mutating_ownership(self):
        server = _server("owner")
        address = IPAddress.objects.create(address="10.0.0.5/24", description="[kea-sync: lease]")
        _reconcile(server, [_lease()])
        before_object = IPAddress.objects.values().get(pk=address.pk)
        before_links = list(IPAMOwnershipLink.objects.filter(ip_address=address).values())
        completed = "2026-10-02T00:00:00+00:00"
        malformed = (
            [],
            {"unknown": {}},
            {"job": []},
            {"job": {"sources": []}},
            {"job": {"sources": "lease", "completed_at": completed}},
            {"job": {"sources": [[5, "lease"]], "completed_at": completed}},
            {"job": {"sources": [[4, "unknown"]], "completed_at": completed}},
            {"job": {"sources": [[True, "lease"]], "completed_at": completed}},
            {"job": {"sources": [[4]], "completed_at": completed}},
            {"job": {"sources": [[4, "lease"]], "completed_at": "2026-10-02T00:00:00"}},
        )
        for value in malformed:
            with self.subTest(receipts=value):
                server.ipam_initial_observations = value
                server.save(update_fields=["ipam_initial_observations"])
                with self.assertRaises(ValueError):
                    upgrade_counts()
                self.assertEqual(IPAddress.objects.values().get(pk=address.pk), before_object)
                self.assertEqual(list(IPAMOwnershipLink.objects.filter(ip_address=address).values()), before_links)
                server.refresh_from_db()
                self.assertIsNone(server.ipam_first_complete_at)
                self.assertEqual(server.ipam_initial_observations, value)
        server.ipam_initial_observations = {}
        server.save(update_fields=["ipam_initial_observations"])
        self.assertEqual(upgrade_counts().waiting, 1)
        _run_job(server, [_lease()])
        server.refresh_from_db()
        self.assertIsNotNone(server.ipam_first_complete_at)
        self.assertEqual(upgrade_counts().waiting, 0)
        self.assertTrue(IPAddress.objects.filter(pk=address.pk).exists())
        self.assertTrue(IPAMOwnershipLink.objects.filter(ip_address=address, adopted=True).exists())
