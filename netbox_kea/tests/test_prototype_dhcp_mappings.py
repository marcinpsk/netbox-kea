# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Throwaway real-DB probes for DHCP Import Mapping branch recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from core import signals as core_signals
from core.models import ObjectChange
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import connection, connections
from django.db.models.signals import pre_delete
from django.test import TransactionTestCase
from netbox.context_managers import event_tracking
from netbox_branching.utilities import activate_branch
from utilities.exceptions import AbortRequest

from netbox_kea.integrations.dhcp_plugin import import_server_config
from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config
from netbox_kea.models import KeaDhcpLink
from netbox_kea.tests.test_branching import _change_request, _provisioned_branch
from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot
from netbox_kea.tests.utils import _make_db_server


class PrototypeMappingLifecycleTest(TransactionTestCase):
    """Execute real lifecycle operations; this class is not production test coverage."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("prototype-mapping-admin")

    def _imported(self, kind, family, suffix):
        server = _make_db_server(name=f"prototype-{suffix}", ca_url="https://kea.example.invalid")
        config = {f"subnet{family}": []}
        observation = None
        if kind == "subnet":
            config[f"subnet{family}"] = [{"id": 7, "subnet": "198.18.0.0/24" if family == 4 else "2001:db8::/64"}]
        else:
            observation = _reservation_snapshot(
                config, family, [{"subnet-id": 0, "hw-address": "02:00:00:00:00:07", "hostname": "prototype"}]
            )
        intent = parse_dhcp_config(config, family)
        with event_tracking(_change_request(self.user)):
            summary = import_server_config(server, intent, observation)
        self.assertEqual(summary.errors, 0, summary.warnings)
        link = KeaDhcpLink.objects.get(server=server, family=family)
        return server, intent, observation, link.sys4_object, link

    def _state(self, branch, target, link, phase):
        row = {
            "phase": phase,
            "strategy": branch.merge_strategy,
            "main_target": type(target).objects.using("default").filter(pk=target.pk).exists(),
            "main_mapping": KeaDhcpLink.objects.using("default").filter(pk=link.pk).exists(),
            "branch_changes": list(
                branch.get_changes().values_list("changed_object_type__model", "changed_object_id", "action")
            ),
        }
        print("PROTOTYPE_STATE " + json.dumps(row, default=str))

    def _delete(self, branch, target, queryset=False):
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            if queryset:
                type(target).objects.filter(pk=target.pk).delete()
            else:
                type(target).objects.get(pk=target.pk).delete()

    def _lifecycle(self, kind, family, queryset):
        server, intent, observation, target, link = self._imported(kind, family, f"{kind}-{family}-{queryset}")
        branch = _provisioned_branch(self, f"prototype-{kind}-{family}-{queryset}")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target, queryset)
        with activate_branch(branch):
            self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self._state(branch, target, link, "branch delete")
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        core_signals.clear_signal_history(sender=type(self))
        branch.merge(user=self.user)
        branch.refresh_from_db()
        self._state(branch, target, link, "merge")
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        core_signals.clear_signal_history(sender=type(self))
        branch.revert(user=self.user)
        branch.refresh_from_db()
        self._state(branch, target, link, "revert")
        restored = KeaDhcpLink.objects.get(pk=link.pk)
        self.assertEqual(restored.object_id, target.pk)
        self.assertEqual(restored.server_id, server.pk)
        self.assertEqual(restored.family, family)
        self.assertEqual(restored.kea_subnet_id, link.kea_subnet_id)
        self.assertEqual(restored.kea_identity, link.kea_identity)
        with event_tracking(_change_request(self.user)):
            again = import_server_config(server, intent, observation)
        self.assertEqual(again.errors, 0, again.warnings)
        self.assertEqual(again.subnets_created, 0)
        self.assertEqual(again.reservations_created, 0)
        self.assertEqual(KeaDhcpLink.objects.get(server=server, family=family).pk, link.pk)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).sys4_object.pk, target.pk)

    def test_squash_restores_imported_subnets(self):
        for family in (4, 6):
            for queryset in (False, True):
                with self.subTest(family=family, queryset=queryset):
                    self._lifecycle("subnet", family, queryset)

    def test_squash_restores_global_reservations(self):
        for family in (4, 6):
            for queryset in (False, True):
                with self.subTest(family=family, queryset=queryset):
                    self._lifecycle("reservation", family, queryset)

    def test_iterative_is_refused_before_merge(self):
        _, _, _, target, link = self._imported("subnet", 4, "iterative")
        branch = _provisioned_branch(self, "prototype-iterative")
        branch.merge_strategy = "iterative"
        branch.save(provision=False)
        self._delete(branch, target)
        self._state(branch, target, link, "iterative delete")
        core_signals.clear_signal_history(sender=type(self))
        with self.assertRaises(AbortRequest):
            branch.merge(user=self.user)
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertEqual(branch.merge_strategy, "iterative")

    def test_unguarded_iterative_revert_reaches_missing_target(self):
        from netbox_branching.signals import pre_merge, pre_revert

        from netbox_kea import branching

        _, _, _, target, link = self._imported("subnet", 4, "iterative-control")
        branch = _provisioned_branch(self, "prototype-iterative-control")
        branch.merge_strategy = "iterative"
        branch.save(provision=False)
        self._delete(branch, target)
        # Remove only the strategy refusal to observe native dependency ordering.
        pre_merge.disconnect(dispatch_uid="prototype.mapping.merge")
        pre_revert.disconnect(dispatch_uid="prototype.mapping.revert")
        try:
            core_signals.clear_signal_history(sender=type(self))
            branch.merge(user=self.user)
            branch.refresh_from_db()
            self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
            self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
            core_signals.clear_signal_history(sender=type(self))
            with self.assertRaises(ObjectDoesNotExist) as failed:
                branch.revert(user=self.user)
            self.assertIsInstance(failed.exception, type(target).DoesNotExist)
            print(f"PROTOTYPE_RESULT iterative restoration: {type(failed.exception).__name__}: {failed.exception}")
        finally:
            pre_merge.connect(branching.prototype_mapping_preaction, dispatch_uid="prototype.mapping.merge")
            pre_revert.connect(branching.prototype_mapping_preaction, dispatch_uid="prototype.mapping.revert")

    def test_main_delete_has_synchronous_applied_provenance(self):
        _, _, _, target, link = self._imported("subnet", 4, "provenance")
        branch = _provisioned_branch(self, "prototype-provenance")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        observations = []
        content_type = ContentType.objects.get_for_model(target)

        def observe_delete(sender, instance, using, **kwargs):
            if using == "default":
                records = ObjectChange.objects.filter(
                    changed_object_type=content_type,
                    changed_object_id=instance.pk,
                    application__branch=branch,
                    action="delete",
                )
                observations.append((connection.in_atomic_block, records.count()))

        pre_delete.connect(observe_delete, sender=type(target), weak=False)
        self.addCleanup(pre_delete.disconnect, observe_delete, sender=type(target))
        core_signals.clear_signal_history(sender=type(self))
        branch.merge(user=self.user)
        self.assertEqual(observations, [(True, 1)])
        changes = ObjectChange.objects.filter(
            changed_object_type=content_type, changed_object_id=target.pk, application__branch=branch, action="delete"
        )
        self.assertEqual(changes.count(), 1)
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_missing_branch_mapping_table_falls_through_to_main(self):
        _, _, _, _, link = self._imported("subnet", 4, "old-schema")
        branch = _provisioned_branch(self, "prototype-old-schema")
        with connection.cursor() as cursor:
            cursor.execute(f'DROP TABLE "{branch.schema_name}"."{KeaDhcpLink._meta.db_table}"')
        with activate_branch(branch):
            self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).server_id, link.server_id)
        print("PROTOTYPE_RESULT old-schema raw mapping query read main")

    def test_discard_keeps_main_target_and_mapping(self):
        _, _, _, target, link = self._imported("subnet", 4, "discard")
        branch = _provisioned_branch(self, "prototype-discard")
        self._delete(branch, target)
        branch.deprovision()
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())


class PrototypeMappingSafetyTest(PrototypeMappingLifecycleTest):
    """Prove the unsafe controls red before adding prototype refusal guards."""

    def test_main_import_after_preflight_must_refuse_merge(self):
        from netbox_branching.models import Branch
        from netbox_branching.signals import pre_merge

        server, intent, observation, target, link = self._imported("reservation", 4, "late-import")
        link.delete()
        branch = _provisioned_branch(self, "prototype-late-import")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        paused, resume = Event(), Event()

        def pause_merge(sender, branch, **kwargs):
            paused.set()
            if not resume.wait(30):
                raise TimeoutError("Prototype main import did not resume merge")

        def merge_in_thread():
            try:
                Branch.objects.get(pk=branch.pk).merge(user=self.user)
            finally:
                connections.close_all()

        pre_merge.connect(pause_merge, weak=False)
        self.addCleanup(pre_merge.disconnect, pause_merge)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(merge_in_thread)
            try:
                self.assertTrue(paused.wait(30))
                with event_tracking(_change_request(self.user)):
                    imported = import_server_config(server, intent, observation)
                self.assertEqual(imported.errors, 0, imported.warnings)
                attached = KeaDhcpLink.objects.get(server=server)
                self.assertEqual(attached.object_id, target.pk)
            finally:
                resume.set()
            with self.assertRaises(AbortRequest):
                future.result(timeout=30)
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=attached.pk).exists())

    def test_old_schema_delete_must_preserve_main_mapping(self):
        _, _, _, target, link = self._imported("subnet", 4, "old-delete")
        branch = _provisioned_branch(self, "prototype-old-delete")
        with connection.cursor() as cursor:
            cursor.execute(f'DROP TABLE "{branch.schema_name}"."{KeaDhcpLink._meta.db_table}"')
        with self.assertRaises(AbortRequest):
            self._delete(branch, target)
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
