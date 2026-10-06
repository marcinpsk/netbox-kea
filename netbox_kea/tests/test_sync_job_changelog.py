# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Real sync job executions produce native NetBox change records."""

import uuid
from collections import defaultdict
from unittest import skipUnless

import django_rq
from core.models import Job, ObjectChange
from dcim.models import MACAddress
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from ipam.models import VRF, IPAddress, IPRange, Prefix
from netbox import context as tracking_context

from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.models import SyncConfig
from netbox_kea.tests.kea_stub import _catalogue_responses_for_subnets, complete_lease, queued
from netbox_kea.tests.test_jobs import _lease_page, _patch_kea
from netbox_kea.tests.utils import DISPATCHED_EVENTS, _make_db_server, plugins_config

_SUBNETS = [{"id": 1, "subnet": "198.18.0.0/24"}]
_LEASE = complete_lease(
    {
        "ip-address": "198.18.0.42",
        "hostname": "phone.example.invalid",
        "subnet-id": 1,
        "valid-lft": 3600,
        "state": 0,
    }
)
_EVENTS_RECORDER = "netbox_kea.tests.utils.record_dispatched_events"


@override_settings(PLUGINS_CONFIG=plugins_config())
class SyncJobChangeRecordTest(TestCase):
    def setUp(self):
        self.server = _make_db_server(dhcp6=False, sync_reservations_enabled=False)

    def _run(self, *, leases=(), subnets=None, user=None, interval=None):
        job = Job.objects.create(
            name=KeaIpamSyncJob.name,
            job_id=uuid.uuid4(),
            user=user,
            interval=interval,
            scheduled=timezone.now() if interval else None,
            status="scheduled" if interval else "pending",
        )
        with _patch_kea(
            leases4=list(leases),
            responses=_catalogue_responses_for_subnets(4, _SUBNETS if subnets is None else subnets),
        ):
            KeaIpamSyncJob.handle(job)
        job.refresh_from_db()
        return job

    def test_scheduled_execution_records_a_system_attributed_ip_create(self):
        config = SyncConfig.get()
        config.interval_minutes = 13
        config.save()
        with self.captureOnCommitCallbacks(execute=True):
            job = self._run(leases=[_LEASE], interval=5)
        self.assertEqual(job.status, "completed")
        ip = IPAddress.objects.get(address__net_host="198.18.0.42")
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(ip),
            changed_object_id=ip.pk,
            action="create",
        )
        self.assertEqual(change.user.username, "netbox-kea-sync")
        self.assertEqual(change.user_name, "netbox-kea-sync")
        self.assertEqual(change.postchange_data["address"], "198.18.0.42/24")
        self.assertIsNone(job.user_id)
        self.assertIsNotNone(change.request_id)
        self.assertFalse(change.user.is_active)
        self.assertFalse(change.user.is_superuser)
        self.assertFalse(change.user.has_usable_password())
        self.assertFalse(change.user.groups.exists())
        self.assertFalse(change.user.user_permissions.exists())
        self.assertFalse(change.user.object_permissions.exists())
        successor = Job.objects.exclude(pk=job.pk).get(name=KeaIpamSyncJob.name)
        self.assertEqual(successor.status, "scheduled")
        self.assertEqual(successor.interval, 13)
        self.assertIsNone(successor.user_id)
        queue = django_rq.get_queue(getattr(successor, "queue_name", "default") or "default")
        queued_successor = queue.fetch_job(str(successor.job_id))
        self.assertIsNotNone(queued_successor)
        self.addCleanup(queued_successor.delete)

    @skipUnless(hasattr(Job, "log_entries"), "This NetBox release has no persistent native job logs.")
    def test_the_native_job_log_retains_sync_messages(self):
        job = self._run(leases=[_LEASE])
        self.assertEqual(job.status, "completed")
        messages = [entry["message"] for entry in job.log_entries]
        self.assertIn("Starting Kea IPAM sync for 1 server(s).", messages)
        self.assertTrue(any(message.startswith("Server test-kea: created=") for message in messages))

    def test_changed_lease_records_previous_fields_and_an_unchanged_run_adds_no_changes(self):
        first = self._run(leases=[_LEASE])
        self.assertEqual(first.status, "completed")
        ip = IPAddress.objects.get(address__net_host="198.18.0.42")
        object_type = ContentType.objects.get_for_model(ip)
        created = ObjectChange.objects.get(changed_object_type=object_type, changed_object_id=ip.pk, action="create")
        IPAddress.objects.filter(pk=ip.pk).update(
            address="198.18.0.42/32",
            status="reserved",
            dns_name="old.example.invalid",
            description="[kea-sync: reservation] operator note",
        )
        changed = self._run(leases=[_LEASE])
        self.assertEqual(changed.status, "completed")
        update = ObjectChange.objects.get(changed_object_type=object_type, changed_object_id=ip.pk, action="update")
        for field, before, after in (
            ("address", "198.18.0.42/32", "198.18.0.42/24"),
            ("status", "reserved", "dhcp"),
            ("dns_name", "old.example.invalid", "phone.example.invalid"),
            ("description", "[kea-sync: reservation] operator note", "[kea-sync: lease] operator note"),
        ):
            with self.subTest(field=field):
                self.assertEqual(update.prechange_data[field], before)
                self.assertEqual(update.postchange_data[field], after)
        self.assertNotEqual(update.request_id, created.request_id)
        self.assertEqual(update.user_id, created.user_id)
        ip_changes = ObjectChange.objects.filter(changed_object_type=object_type, changed_object_id=ip.pk)
        changes_before = list(ip_changes.values_list("pk", flat=True))
        unchanged = self._run(leases=[_LEASE])
        self.assertEqual(unchanged.status, "completed")
        self.assertEqual(list(ip_changes.values_list("pk", flat=True)), changes_before)

    def test_one_execution_groups_changes_across_all_servers(self):
        _make_db_server(name="other-owner", ca_url="https://other.example.invalid", dhcp6=False)
        responses = _catalogue_responses_for_subnets(4, _SUBNETS)
        responses["lease4-get-page"] = queued(
            _lease_page([_LEASE]), _lease_page([{**_LEASE, "ip-address": "198.18.0.43"}])
        )
        job = Job.objects.create(name=KeaIpamSyncJob.name, job_id=uuid.uuid4())
        with _patch_kea(responses=responses):
            KeaIpamSyncJob.handle(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(len(job.data["summary"]), 2)
        changes = ObjectChange.objects.filter(changed_object_type=ContentType.objects.get_for_model(IPAddress))
        self.assertEqual(changes.count(), 2)
        self.assertEqual(set(changes.values_list("action", flat=True)), {"create"})
        self.assertEqual(len(set(ObjectChange.objects.values_list("request_id", flat=True))), 1)

    def test_prefix_and_pool_changes_record_creation_reactivation_and_deprecation(self):
        type(self.server).objects.filter(pk=self.server.pk).update(sync_deprecate_prefixes_and_ranges=True)
        config = SyncConfig.get()
        config.sync_prefixes_enabled = config.sync_ip_ranges_enabled = True
        config.save()
        subnets = [{"id": 1, "subnet": "198.18.0.0/24", "pools": [{"pool": "198.18.0.10-198.18.0.20"}]}]
        created = self._run(subnets=subnets)
        self.assertEqual(created.status, "completed")
        prefix = Prefix.objects.get(prefix="198.18.0.0/24")
        pool = IPRange.objects.get(start_address="198.18.0.10/24", end_address="198.18.0.20/24")
        for obj in (prefix, pool):
            change = ObjectChange.objects.get(
                changed_object_type=ContentType.objects.get_for_model(obj), changed_object_id=obj.pk, action="create"
            )
            self.assertEqual(change.postchange_data["status"], "active")
        Prefix.objects.filter(pk=prefix.pk).update(status="deprecated")
        IPRange.objects.filter(pk=pool.pk).update(status="deprecated")
        for reported, before, after in ((subnets, "deprecated", "active"), ([], "active", "deprecated")):
            changed = self._run(subnets=reported)
            self.assertEqual(changed.status, "completed")
            for obj in (prefix, pool):
                with self.subTest(model=type(obj).__name__, before=before):
                    change = ObjectChange.objects.filter(
                        changed_object_type=ContentType.objects.get_for_model(obj),
                        changed_object_id=obj.pk,
                        action="update",
                    ).latest("pk")
                    self.assertEqual(change.prechange_data["status"], before)
                    self.assertEqual(change.postchange_data["status"], after)

    def test_lease_hostname_change_records_the_existing_mac_description(self):
        mac = MACAddress.objects.create(
            mac_address="02:00:00:00:00:42", description="dhcp_hostname: old.example.invalid"
        )
        job = self._run(leases=[{**_LEASE, "hw-address": "02:00:00:00:00:42"}])
        self.assertEqual(job.status, "completed")
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(mac), changed_object_id=mac.pk, action="update"
        )
        self.assertEqual(change.prechange_data["description"], "dhcp_hostname: old.example.invalid")
        self.assertEqual(change.postchange_data["description"], "dhcp_hostname: phone.example.invalid")
        self.assertEqual(len(set(ObjectChange.objects.values_list("request_id", flat=True))), 1)

    def test_a_new_mac_records_one_create_that_carries_its_hostname(self):
        job = self._run(leases=[{**_LEASE, "hw-address": "02:00:00:00:00:42"}])
        self.assertEqual(job.status, "completed")
        mac = MACAddress.objects.get(mac_address="02:00:00:00:00:42")
        changes = list(
            ObjectChange.objects.filter(
                changed_object_type=ContentType.objects.get_for_model(mac), changed_object_id=mac.pk
            ).order_by("pk")
        )
        # One event-producing write per MAC sync: a failed write then queues no event (#302).
        self.assertEqual([change.action for change in changes], ["create"])
        self.assertEqual(changes[0].postchange_data["description"], "dhcp_hostname: phone.example.invalid")

    def test_legacy_adoption_keeps_the_vrf_move_and_lease_update_as_separate_changes(self):
        vrf = VRF.objects.create(name="sync-vrf")
        type(self.server).objects.filter(pk=self.server.pk).update(sync_vrf=vrf)
        ip = IPAddress.objects.create(
            address="198.18.0.42/32", status="reserved", description="Synced from Kea DHCP reservation"
        )
        job = self._run(leases=[_LEASE])
        self.assertEqual(job.status, "completed")
        changes = list(
            ObjectChange.objects.filter(
                changed_object_type=ContentType.objects.get_for_model(ip), changed_object_id=ip.pk, action="update"
            ).order_by("pk")
        )
        self.assertEqual(len(changes), 2)
        move, update = changes
        self.assertIsNone(move.prechange_data["vrf"])
        self.assertEqual(move.postchange_data["vrf"], vrf.pk)
        self.assertEqual(update.prechange_data["vrf"], vrf.pk)
        self.assertEqual(update.prechange_data["address"], "198.18.0.42/32")
        self.assertEqual(update.postchange_data["address"], "198.18.0.42/24")
        self.assertEqual(update.prechange_data["status"], "reserved")
        self.assertEqual(update.postchange_data["status"], "dhcp")
        self.assertEqual(move.request_id, update.request_id)

    @override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="deprecate"))
    def test_stale_ip_deprecation_records_the_previous_status(self):
        self.assertEqual(self._run(leases=[_LEASE]).status, "completed")
        ip = IPAddress.objects.get(address__net_host="198.18.0.42")
        self.assertEqual(self._run().status, "completed")
        ip.refresh_from_db()
        self.assertEqual(ip.status, "deprecated")
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(ip), changed_object_id=ip.pk, action="update"
        )
        self.assertEqual(change.prechange_data["status"], "dhcp")
        self.assertEqual(change.postchange_data["status"], "deprecated")

    @override_settings(EVENTS_PIPELINE=[_EVENTS_RECORDER])
    def test_failed_execution_keeps_committed_changes_and_restores_context_without_dispatch(self):
        _make_db_server(name="z-other-owner", ca_url="https://other.example.invalid", dhcp6=False)
        first_actor = get_user_model().objects.create_user("first-operator")
        next_actor = get_user_model().objects.create_user("next-operator")
        prior_request = RequestFactory().get("/caller/")
        prior_request.user = first_actor
        prior_request.id = uuid.uuid4()
        prior_queue = {"caller": object()}
        contexts = [
            (tracking_context.current_request, prior_request),
            (tracking_context.events_queue, prior_queue),
        ]
        if (cache := getattr(tracking_context, "query_cache", None)) is not None:
            contexts.append((cache, defaultdict(dict)))
        tokens = [(variable, variable.set(value)) for variable, value in contexts]
        DISPATCHED_EVENTS.clear()
        self.addCleanup(DISPATCHED_EVENTS.clear)
        try:
            responses = _catalogue_responses_for_subnets(4, _SUBNETS)
            responses["lease4-get-page"] = queued(
                _lease_page([_LEASE]), {"result": 0, "arguments": {"leases": "invalid"}}
            )
            failed = Job.objects.create(name=KeaIpamSyncJob.name, job_id=uuid.uuid4(), user=first_actor)
            with _patch_kea(responses=responses):
                KeaIpamSyncJob.handle(failed)
            failed.refresh_from_db()
            self.assertEqual(failed.status, "failed")
            ip = IPAddress.objects.get(address__net_host="198.18.0.42")
            committed = ObjectChange.objects.get(
                changed_object_type=ContentType.objects.get_for_model(ip), changed_object_id=ip.pk, action="create"
            )
            self.assertEqual(committed.user_id, first_actor.pk)
            self.assertNotEqual(committed.request_id, prior_request.id)
            self.assertEqual(DISPATCHED_EVENTS, [])
            for variable, expected in contexts:
                self.assertIs(variable.get(), expected)

            later = self._run(leases=[{**_LEASE, "ip-address": "198.18.0.43"}], user=next_actor)
            self.assertEqual(later.status, "completed")
            next_ip = IPAddress.objects.get(address__net_host="198.18.0.43")
            change = ObjectChange.objects.get(
                changed_object_type=ContentType.objects.get_for_model(next_ip),
                changed_object_id=next_ip.pk,
                action="create",
            )
            self.assertEqual(change.user_id, next_actor.pk)
            self.assertNotEqual(change.request_id, committed.request_id)
            self.assertTrue(DISPATCHED_EVENTS)
            for event in DISPATCHED_EVENTS:
                if "request" in event:
                    self.assertEqual(event["user"].pk, next_actor.pk)
                    self.assertEqual(event["request"].id, change.request_id)
                else:
                    self.assertEqual(event["username"], "next-operator")
                    self.assertEqual(str(event["request_id"]), str(change.request_id))
            for variable, expected in contexts:
                self.assertIs(variable.get(), expected)
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    def test_retry_after_a_rolled_back_cleanup_records_the_successful_delete(self):
        seeded = self._run(leases=[_LEASE])
        self.assertEqual(seeded.status, "completed")
        ip = IPAddress.objects.get(address__net_host="198.18.0.42")
        ip_pk = ip.pk
        object_type = ContentType.objects.get_for_model(ip)
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE FUNCTION reject_sync_ip_delete() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'retry this deletion' USING ERRCODE = '40001'; END $$"
            )
            cursor.execute(
                "CREATE TRIGGER reject_sync_ip_delete BEFORE DELETE ON ipam_ipaddress "
                "FOR EACH ROW EXECUTE FUNCTION reject_sync_ip_delete()"
            )
        try:
            failed = self._run()
            self.assertEqual(failed.status, "failed")
            self.assertTrue(IPAddress.objects.filter(pk=ip_pk).exists())
            self.assertFalse(
                ObjectChange.objects.filter(
                    changed_object_type=object_type, changed_object_id=ip_pk, action="delete"
                ).exists()
            )
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER reject_sync_ip_delete ON ipam_ipaddress")
                cursor.execute("DROP FUNCTION reject_sync_ip_delete()")

        retried = self._run()
        self.assertEqual(retried.status, "completed")
        self.assertFalse(IPAddress.objects.filter(pk=ip_pk).exists())
        change = ObjectChange.objects.get(changed_object_type=object_type, changed_object_id=ip_pk, action="delete")
        self.assertEqual(change.prechange_data["address"], "198.18.0.42/24")
        self.assertEqual(change.prechange_data["status"], "dhcp")
        self.assertEqual(change.prechange_data["description"], "[kea-sync: lease]")
        self.assertIsNone(change.postchange_data)

    def test_an_active_reserved_account_fails_before_sync_without_changing_the_account(self):
        actor = get_user_model().objects.create_user("netbox-kea-sync")
        before = get_user_model().objects.filter(pk=actor.pk).values().get()
        job = Job.objects.create(name=KeaIpamSyncJob.name, job_id=uuid.uuid4())
        with _patch_kea(leases4=[_LEASE], responses=_catalogue_responses_for_subnets(4, _SUBNETS)) as kea:
            KeaIpamSyncJob.handle(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(kea.requests, [])
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(ObjectChange.objects.exists())
        self.assertEqual(get_user_model().objects.filter(pk=actor.pk).values().get(), before)

    def test_reserved_account_privileges_and_case_variants_are_rejected(self):
        reasons = ["superuser", "password", "group", "django-permission", "object-permission", "case"]
        if hasattr(get_user_model(), "is_staff"):
            reasons.append("staff")
        for reason in reasons:
            with self.subTest(reason=reason):
                actor = get_user_model().objects.create_user(
                    "NETBOX-KEA-SYNC" if reason == "case" else "netbox-kea-sync", is_active=False
                )
                if reason == "staff":
                    actor.is_staff = True
                    actor.save()
                elif reason == "superuser":
                    actor.is_superuser = True
                    actor.save()
                elif reason == "password":
                    actor.set_password("placeholder-password")
                    actor.save()
                elif reason == "group":
                    actor.groups.add(actor.groups.model.objects.create(name="sync-grant"))
                elif reason == "django-permission":
                    actor.user_permissions.add(
                        Permission.objects.get(
                            content_type=ContentType.objects.get_for_model(IPAddress), codename="change_ipaddress"
                        )
                    )
                elif reason == "object-permission":
                    permission = actor.object_permissions.model.objects.create(name="sync-grant", actions=["change"])
                    permission.object_types.add(permission.object_types.model.objects.get_for_model(IPAddress))
                    actor.object_permissions.add(permission)
                before = get_user_model().objects.filter(pk=actor.pk).values().get()
                job = Job.objects.create(name=KeaIpamSyncJob.name, job_id=uuid.uuid4())
                with _patch_kea(leases4=[_LEASE], responses=_catalogue_responses_for_subnets(4, _SUBNETS)) as kea:
                    KeaIpamSyncJob.handle(job)
                job.refresh_from_db()
                self.assertEqual(job.status, "failed")
                self.assertEqual(kea.requests, [])
                self.assertFalse(IPAddress.objects.exists())
                self.assertFalse(ObjectChange.objects.exists())
                self.assertEqual(get_user_model().objects.filter(pk=actor.pk).values().get(), before)
                self.assertEqual(get_user_model().objects.count(), 1)
                actor.delete()


@override_settings(PLUGINS_CONFIG=plugins_config())
class ManualSyncJobChangeRecordTest(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("sync-operator")
        self.client.force_login(self.user)
        self.server = _make_db_server(dhcp6=False, sync_reservations_enabled=False)

    def _enqueue(self):
        response = self.client.post(
            reverse("plugins:netbox_kea:server_sync_now", args=[self.server.pk]),
            {"browser_payload": "private form data"},
            HTTP_X_BROWSER_METADATA="private request header",
        )
        self.assertEqual(response.status_code, 302)
        job = Job.objects.get(name=KeaIpamSyncJob.name)
        queue = django_rq.get_queue(getattr(job, "queue_name", "default") or "default")
        queued = queue.fetch_job(str(job.job_id))
        self.assertIsNotNone(queued)
        self.assertEqual(queued.args, ())
        self.assertEqual(set(queued.kwargs), {"job", "server_pk"})
        self.assertEqual(queued.kwargs["server_pk"], self.server.pk)
        self.addCleanup(queued.delete)
        return job, queued

    def test_sync_now_records_the_initiating_user_after_redis_serialization(self):
        job, queued = self._enqueue()
        self.assertEqual(job.user_id, self.user.pk)
        self.assertEqual(queued.kwargs["job"].user.pk, self.user.pk)
        with _patch_kea(leases4=[_LEASE], responses=_catalogue_responses_for_subnets(4, _SUBNETS)):
            queued.func(*queued.args, **queued.kwargs)

        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        ip = IPAddress.objects.get(address__net_host="198.18.0.42")
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(ip), changed_object_id=ip.pk, action="create"
        )
        self.assertEqual(change.user_id, self.user.pk)
        self.assertEqual(change.user_name, "sync-operator")

    def test_deleted_initiator_uses_system_attribution_without_restoring_the_user(self):
        job, queued = self._enqueue()
        queued_job = queued.kwargs["job"]
        user_pk = self.user.pk
        self.assertEqual(queued_job.user.pk, user_pk)
        self.user.delete()
        job.refresh_from_db()
        self.assertIsNone(job.user_id)
        self.assertEqual(queued_job.user_id, user_pk)

        with _patch_kea(leases4=[_LEASE], responses=_catalogue_responses_for_subnets(4, _SUBNETS)):
            queued.func(*queued.args, **queued.kwargs)

        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertIsNone(job.user_id)
        self.assertFalse(get_user_model().objects.filter(pk=user_pk).exists())
        ip = IPAddress.objects.get(address__net_host="198.18.0.42")
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(ip), changed_object_id=ip.pk, action="create"
        )
        self.assertEqual(change.user.username, "netbox-kea-sync")
