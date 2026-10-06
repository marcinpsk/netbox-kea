# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Real importer and native branch recovery preserve DHCP source associations."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from threading import Event, get_ident

import pytest

from netbox_kea import branching

if not branching.installed():
    if os.environ.get("NETBOX_KEA_REQUIRE_BRANCHING") == "1":
        raise RuntimeError("NETBOX_KEA_REQUIRE_BRANCHING=1, but netbox_branching is not an installed app")
    pytest.skip("netbox-branching is not installed", allow_module_level=True)

from asgiref.sync import async_to_sync
from core.models import Job, ObjectChange
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.core.serializers.json import DjangoJSONEncoder
from django.core.signals import request_finished
from django.db import IntegrityError, OperationalError, connection, connections, transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ipam.models import VRF
from netbox.context import current_request
from netbox.context_managers import event_tracking
from netbox_branching.constants import COOKIE_NAME
from netbox_branching.models import Branch
from netbox_branching.signals import pre_merge, squash_dependency_graph_built
from netbox_branching.utilities import activate_branch
from utilities.exceptions import AbortRequest

from netbox_kea.integrations.dhcp_plugin import import_server_config
from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config
from netbox_kea.models import KeaDhcpLink
from netbox_kea.tests.kea_stub import _res_page, stub_kea
from netbox_kea.tests.test_branching import (
    _KEA_OBJECTS,
    _change_request,
    _device_with_interface,
    _family,
    _provisioned_branch,
    _recorded_kea,
)
from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot
from netbox_kea.tests.test_ipam_reconciliation import _lease, _reconcile, _row
from netbox_kea.tests.utils import DISPATCHED_EVENTS, _make_db_server


class DhcpMappingRecoveryTest(TransactionTestCase):
    def setUp(self):
        from django.apps import apps

        if not apps.is_installed("netbox_dhcp"):
            self.skipTest("netbox_dhcp is not installed")
        self.user = get_user_model().objects.create_superuser("mapping-recovery-admin")

    def test_guarded_contenttype_cleanup_deletes_an_obsolete_model(self):
        stale = ContentType.objects.create(app_label="netbox_kea", model="obsolete_dhcp_mapping")
        current = ContentType.objects.get_for_model(KeaDhcpLink)

        call_command("remove_stale_contenttypes", interactive=False, stdout=StringIO())

        self.assertFalse(ContentType.objects.filter(pk=stale.pk).exists())
        self.assertTrue(ContentType.objects.filter(pk=current.pk).exists())

    def test_guarded_target_deletion_accepts_a_model_attribute(self):
        for kind in ("subnet", "reservation"):
            for family in (4, 6):
                for in_branch in (False, True):
                    with self.subTest(kind=kind, family=family, in_branch=in_branch):
                        suffix = f"model-attribute-{kind}-{family}-{in_branch}"
                        _, _, _, target, link = self._imported(kind, family, suffix)
                        branch = _provisioned_branch(self, suffix) if in_branch else None
                        with activate_branch(branch) if branch else nullcontext():
                            with event_tracking(_change_request(self.user)):
                                local = type(target).objects.get(pk=target.pk)
                                local.model = "target metadata"
                                local.delete()
                            self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
                            self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def _imported(self, kind, family, suffix):
        server = _make_db_server(name=f"mapping-{suffix}", ca_url="https://kea.example.invalid")
        config = {f"subnet{family}": []}
        observation = None
        if kind == "subnet":
            config[f"subnet{family}"] = [{"id": 7, "subnet": "198.18.0.0/24" if family == 4 else "2001:db8::/64"}]
        else:
            observation = _reservation_snapshot(
                config, family, [{"subnet-id": 0, "hw-address": "02:00:00:00:00:07", "hostname": "mapping"}]
            )
        intent = parse_dhcp_config(config, family)
        with event_tracking(_change_request(self.user)):
            summary = import_server_config(server, intent, observation)
        self.assertEqual(summary.errors, 0, summary.warnings)
        link = KeaDhcpLink.objects.get(server=server, family=family)
        return server, intent, observation, link.sys4_object, link

    def _delete(self, branch, target, queryset=False):
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            if queryset:
                type(target).objects.filter(pk=target.pk).delete()
            else:
                type(target).objects.get(pk=target.pk).delete()
        # The real HTTP lifecycle emits this signal after the view has completed.
        request_finished.send(sender=type(self))

    def _lifecycle(self, kind, family, queryset):
        server, intent, observation, target, link = self._imported(kind, family, f"{kind}-{family}-{queryset}")
        branch = _provisioned_branch(self, f"mapping-{kind}-{family}-{queryset}")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target, queryset)
        with activate_branch(branch):
            self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
            self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        branch.revert(user=self.user)
        branch.refresh_from_db()
        restored = KeaDhcpLink.objects.get(pk=link.pk)
        self.assertEqual(restored.created, link.created)
        self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
        self.assertEqual(
            (restored.object_id, restored.server_id, restored.family, restored.kea_subnet_id, restored.kea_identity),
            (target.pk, server.pk, family, link.kea_subnet_id, link.kea_identity),
        )
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

    def test_squash_deletes_subnet_before_its_protected_prefix(self):
        from ipam.models import Prefix
        from netbox_dhcp.models import DHCPServer, Subnet

        from netbox_kea.models import IPAMOwnershipLink

        for mapped in (False, True):
            with self.subTest(mapped=mapped):
                if mapped:
                    _, _, _, target, mapping = self._imported("subnet", 4, "protected-prefix")
                    IPAMOwnershipLink.objects.filter(prefix_id=target.prefix_id).delete()
                else:
                    prefix = Prefix.objects.create(prefix="198.18.2.0/24")
                    parent = DHCPServer.objects.create(name="ordinary-native-parent")
                    target = Subnet.objects.create(
                        name="ordinary-native-subnet", subnet_id=17, prefix=prefix, dhcp_server=parent
                    )
                    mapping = None
                prefix = target.prefix
                branch = _provisioned_branch(self, f"native-protected-prefix-{mapped}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                self._delete(branch, target)
                self._delete(branch, prefix)
                self.assertTrue(Subnet.objects.filter(pk=target.pk).exists())
                self.assertTrue(Prefix.objects.filter(pk=prefix.pk).exists())
                branch.merge(user=self.user)
                branch.refresh_from_db()
                self.assertEqual(branch.status, "merged")
                self.assertFalse(Subnet.objects.filter(pk=target.pk).exists())
                self.assertFalse(Prefix.objects.filter(pk=prefix.pk).exists())
                if mapping is not None:
                    self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
                branch.revert(user=self.user)
                self.assertTrue(Subnet.objects.filter(pk=target.pk, prefix_id=prefix.pk).exists())
                self.assertTrue(Prefix.objects.filter(pk=prefix.pk).exists())
                if mapping is not None:
                    self.assertTrue(KeaDhcpLink.objects.filter(pk=mapping.pk, object_id=target.pk).exists())

    def test_revert_preserves_previously_mapped_target_after_mapping_deletion(self):
        for delete_server in (False, True):
            with self.subTest(delete_server=delete_server):
                suffix = f"removed-mapping-protection-{delete_server}"
                server, _, _, target, link = self._imported("subnet", 4, suffix)
                branch = _provisioned_branch(self, suffix)
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    changed = type(target).objects.get(pk=target.pk)
                    changed.description = "branch description"
                    changed.save()
                branch.merge(user=self.user)
                branch.refresh_from_db()
                if delete_server:
                    server.delete()
                else:
                    KeaDhcpLink.objects.filter(pk=link.pk).delete()
                self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
                changed = type(target).objects.get(pk=target.pk)
                changed.description = "newer main description"
                changed.save()
                before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
                with self.assertRaises(AbortRequest):
                    branch.revert(user=self.user)
                branch.refresh_from_db()
                self.assertEqual(branch.status, "merged")
                self.assertEqual(branch.applied_changes.count(), applied)
                self.assertEqual(ObjectChange.objects.count(), before)
                self.assertEqual(type(target).objects.get(pk=target.pk).description, "newer main description")
                with activate_branch(branch):
                    self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk, object_id=target.pk).exists())
                self.assertIsNone(current_request.get())

    def test_squash_preserves_unplanned_protected_subnet(self):
        from django.db.models.deletion import ProtectedError
        from ipam.models import Prefix
        from netbox_dhcp.models import DHCPServer, Subnet

        prefix = Prefix.objects.create(prefix="198.18.3.0/24")
        branch = _provisioned_branch(self, "native-unplanned-protected-prefix")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, prefix)
        parent = DHCPServer.objects.create(name="unplanned-native-parent")
        target = Subnet.objects.create(name="unplanned-native-subnet", subnet_id=18, prefix=prefix, dhcp_server=parent)
        before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
        with self.assertRaises(ProtectedError):
            branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertTrue(Prefix.objects.filter(pk=prefix.pk).exists())
        self.assertTrue(Subnet.objects.filter(pk=target.pk, prefix_id=prefix.pk).exists())
        self.assertIsNone(current_request.get())

    def test_squash_moves_subnet_before_deleting_its_protected_prefix(self):
        from ipam.models import Prefix
        from netbox_dhcp.models import DHCPServer, Subnet

        prefix = Prefix.objects.create(prefix="198.18.4.0/24")
        replacement = Prefix.objects.create(prefix="198.18.5.0/24")
        parent = DHCPServer.objects.create(name="native-moved-prefix-parent")
        target = Subnet.objects.create(
            name="native-moved-prefix-subnet", subnet_id=19, prefix=prefix, dhcp_server=parent
        )
        branch = _provisioned_branch(self, "native-moved-protected-prefix")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = Subnet.objects.get(pk=target.pk)
            changed.prefix = replacement
            changed.save()
        self._delete(branch, prefix)
        self.assertTrue(Subnet.objects.filter(pk=target.pk, prefix_id=prefix.pk).exists())
        branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertFalse(Prefix.objects.filter(pk=prefix.pk).exists())
        self.assertTrue(Subnet.objects.filter(pk=target.pk, prefix_id=replacement.pk).exists())
        branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertTrue(Subnet.objects.filter(pk=target.pk, prefix_id=prefix.pk).exists())
        self.assertTrue(Prefix.objects.filter(pk=prefix.pk).exists())
        self.assertIsNone(current_request.get())

    def test_revert_moves_subnet_before_deleting_branch_created_prefix(self):
        from ipam.models import Prefix
        from netbox_dhcp.models import DHCPServer, Subnet

        prefix = Prefix.objects.create(prefix="198.18.6.0/24")
        parent = DHCPServer.objects.create(name="native-created-prefix-parent")
        target = Subnet.objects.create(
            name="native-created-prefix-subnet", subnet_id=20, prefix=prefix, dhcp_server=parent
        )
        branch = _provisioned_branch(self, "native-created-protected-prefix")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            replacement = Prefix.objects.create(prefix="198.18.7.0/24")
            changed = Subnet.objects.get(pk=target.pk)
            changed.prefix = replacement
            changed.save()
        self.assertFalse(Prefix.objects.filter(pk=replacement.pk).exists())
        branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertTrue(Subnet.objects.filter(pk=target.pk, prefix_id=replacement.pk).exists())
        branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertTrue(Subnet.objects.filter(pk=target.pk, prefix_id=prefix.pk).exists())
        self.assertFalse(Prefix.objects.filter(pk=replacement.pk).exists())
        self.assertIsNone(current_request.get())

    def test_scalar_update_preserves_main_protected_prefix_reference(self):
        from django.db.models.deletion import ProtectedError
        from ipam.models import Prefix
        from netbox_dhcp.models import DHCPServer, Subnet

        prefix = Prefix.objects.create(prefix="198.18.8.0/24")
        original = Prefix.objects.create(prefix="198.18.9.0/24")
        parent = DHCPServer.objects.create(name="native-unchanged-prefix-parent")
        target = Subnet.objects.create(
            name="native-unchanged-prefix-subnet", subnet_id=21, prefix=original, dhcp_server=parent
        )
        branch = _provisioned_branch(self, "native-unchanged-protected-prefix")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = Subnet.objects.get(pk=target.pk)
            changed.description = "branch scalar update"
            changed.save()
        self._delete(branch, prefix)
        target.prefix = prefix
        target.save()
        before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
        with self.assertRaises(ProtectedError):
            branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertTrue(Subnet.objects.filter(pk=target.pk, prefix_id=prefix.pk).exists())
        self.assertEqual(Subnet.objects.get(pk=target.pk).description, "")
        self.assertTrue(Prefix.objects.filter(pk=prefix.pk).exists())
        self.assertIsNone(current_request.get())

    def test_reentrant_import_during_revert_preserves_newer_mapped_update(self):
        server, _, _, target, link = self._imported("subnet", 4, "reentrant-revert-update")
        target.valid_lifetime = 100
        target.save()
        branch = _provisioned_branch(self, "mapping-reentrant-revert-update")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.valid_lifetime = 200
            changed.save()
        branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 200)
        changed_intent = parse_dhcp_config(
            {"subnet4": [{"id": 7, "subnet": "198.18.0.0/24", "valid-lifetime": 600}]}, 4
        )
        before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
        observed = []

        def importing(sender, operation, **kwargs):
            if operation == "revert":
                with event_tracking(_change_request(self.user)):
                    summary = import_server_config(server, changed_intent)
                self.assertEqual(summary.errors, 0, summary.warnings)
                self.assertEqual(KeaDhcpLink.objects.get(server=server, family=4).pk, link.pk)
                observed.append(type(target).objects.get(pk=target.pk).valid_lifetime)

        squash_dependency_graph_built.connect(importing, weak=False)
        try:
            with self.assertRaisesMessage(AbortRequest, "has changed"):
                branch.revert(user=self.user)
        finally:
            squash_dependency_graph_built.disconnect(importing)
        self.assertEqual(observed, [600])
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 200)
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertIsNone(current_request.get())
        branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 100)
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk, object_id=target.pk).exists())
        self.assertIsNone(current_request.get())

    def test_reentrant_mapping_deletion_during_revert_refuses_before_target_update(self):
        for delete_server in (False, True):
            with self.subTest(delete_server=delete_server):
                suffix = f"reentrant-revert-association-{delete_server}"
                server, _, _, target, link = self._imported("subnet", 4, suffix)
                target.valid_lifetime = 100
                target.save()
                branch = _provisioned_branch(self, suffix)
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    changed = type(target).objects.get(pk=target.pk)
                    changed.valid_lifetime = 200
                    changed.save()
                branch.merge(user=self.user)
                branch.refresh_from_db()
                before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
                observed, refusals = [], []

                def deleting(
                    sender,
                    operation,
                    delete_server=delete_server,
                    server=server,
                    link=link,
                    observed=observed,
                    refusals=refusals,
                    **kwargs,
                ):
                    if operation == "revert":
                        observed.append("deletion requested")
                        try:
                            with event_tracking(_change_request(self.user)):
                                if delete_server:
                                    type(server).objects.filter(pk=server.pk).delete()
                                else:
                                    KeaDhcpLink.objects.filter(pk=link.pk).delete()
                        except AbortRequest as error:
                            refusals.append(str(error))
                            raise

                squash_dependency_graph_built.connect(deleting, weak=False)
                try:
                    with self.assertRaises(AbortRequest):
                        branch.revert(user=self.user)
                finally:
                    squash_dependency_graph_built.disconnect(deleting)
                self.assertEqual(observed, ["deletion requested"])
                self.assertEqual(refusals, ["A main DHCP mapping has no reversible branch history. Nothing changed."])
                branch.refresh_from_db()
                self.assertEqual(branch.status, "merged")
                self.assertEqual(branch.applied_changes.count(), applied)
                self.assertEqual(ObjectChange.objects.count(), before)
                self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 200)
                self.assertTrue(type(server).objects.filter(pk=server.pk).exists())
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk, object_id=target.pk).exists())
                self.assertIsNone(current_request.get())

    def test_iterative_preserves_successive_mapped_target_updates(self):
        _, _, _, target, link = self._imported("subnet", 4, "iterative-mapped-updates")
        target.valid_lifetime = 100
        target.save()
        branch = _provisioned_branch(self, "mapping-iterative-updates")
        branch.merge_strategy = "iterative"
        branch.save(provision=False)
        for lifetime in (200, 300):
            with activate_branch(branch), event_tracking(_change_request(self.user)):
                changed = type(target).objects.get(pk=target.pk)
                changed.valid_lifetime = lifetime
                changed.save()
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 100)
        branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 300)
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk, object_id=target.pk).exists())
        branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 100)
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk, object_id=target.pk).exists())
        self.assertIsNone(current_request.get())

    def _tag_round_trip(self, suffix, strategy="iterative"):
        from extras.models import Tag

        _, _, _, target, mapping = self._imported("subnet", 4, suffix)
        first, intermediate, unrelated = (
            Tag.objects.create(name=f"mapping-{suffix}-{name}", slug=f"mapping-{suffix}-{name}")
            for name in ("a", "b", "unrelated")
        )
        target.tags.add(first)
        branch = _provisioned_branch(self, f"mapping-{suffix}")
        branch.merge_strategy = strategy
        branch.save(provision=False)
        for endpoint in (intermediate, first):
            with activate_branch(branch), event_tracking(_change_request(self.user)):
                local = type(target).objects.get(pk=target.pk)
                local.snapshot()
                local.tags.set([Tag.objects.get(pk=endpoint.pk)])
            request_finished.send(sender=type(self))
        history = branch.get_changes().filter(
            changed_object_type=ContentType.objects.get_for_model(target), changed_object_id=target.pk
        )
        self.assertEqual(history.count(), 2)
        self.assertEqual(len(set(history.values_list("request_id", flat=True))), 2)
        collapsed = branching.collapse_changes(branch.get_changes())
        change = next(change for change in collapsed.values() if change.model_class is type(target))
        self.assertEqual(change.final_action, "update")
        self.assertEqual(change.prechange_data["tags"], [first.name])
        self.assertEqual(change.postchange_data["tags"], [first.name])
        return target, mapping, first, intermediate, unrelated, branch

    def test_iterative_refuses_a_missing_intermediate_tag_before_merge_or_revert(self):
        from extras.models import Tag

        for action in ("merge", "revert"):
            with self.subTest(action=action):
                target, mapping, first, intermediate, _unrelated, branch = self._tag_round_trip(
                    f"intermediate-{action}"
                )
                if action == "revert":
                    branch.merge(user=self.user)
                with event_tracking(_change_request(self.user)):
                    Tag.objects.get(pk=intermediate.pk).delete()
                request_finished.send(sender=type(self))
                before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
                original_status = "ready" if action == "merge" else "merged"
                history = {
                    change.pk: (deepcopy(change.prechange_data), deepcopy(change.postchange_data))
                    for change in branch.get_changes()
                }
                mutations = []

                def capture_native_write(sender, instance, using, *, mutations=mutations, **kwargs):
                    if using == "default":
                        mutations.append((sender, instance.pk))

                for model in (type(target), Tag, KeaDhcpLink):
                    pre_save.connect(capture_native_write, sender=model, weak=False)
                try:
                    with self.assertRaisesMessage(AbortRequest, "Tag") as refusal:
                        getattr(branch, action)(user=self.user)
                finally:
                    for model in (type(target), Tag, KeaDhcpLink):
                        pre_save.disconnect(capture_native_write, sender=model)
                self.assertIn("Apply Tag changes separately", str(refusal.exception))
                self.assertIn("fresh branch", str(refusal.exception))
                self.assertFalse(mutations)
                branch.refresh_from_db()
                self.assertEqual(branch.status, original_status)
                self.assertEqual(branch.applied_changes.count(), applied)
                self.assertEqual(ObjectChange.objects.count(), before)
                self.assertEqual(
                    {change.pk: (change.prechange_data, change.postchange_data) for change in branch.get_changes()},
                    history,
                )
                self.assertFalse(Tag.objects.filter(name=intermediate.name).exists())
                self.assertEqual(
                    list(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)), [first.pk]
                )
                self.assertEqual(Tag.objects.get(pk=first.pk).created, first.created)
                self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
                self.assertEqual(KeaDhcpLink.objects.get(pk=mapping.pk).created, mapping.created)
                self.assertIsNone(current_request.get())

    def test_iterative_preserves_intermediate_tag_identity_and_native_undo(self):
        from extras.models import Tag

        target, mapping, first, intermediate, _unrelated, branch = self._tag_round_trip("intermediate-unchanged")
        observed = []

        def observe_native_save(sender, instance, using, **kwargs):
            if using == "default":
                observed.append(list(instance.tags.values_list("pk", flat=True)))

        pre_save.connect(observe_native_save, sender=type(target), weak=False)
        try:
            branch.merge(user=self.user)
            self.assertIn([intermediate.pk], observed)
            observed.clear()
            branch.revert(user=self.user)
            self.assertIn([intermediate.pk], observed)
        finally:
            pre_save.disconnect(observe_native_save, sender=type(target))
        self.assertEqual(list(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)), [first.pk])
        self.assertEqual(Tag.objects.get(pk=intermediate.pk).created, intermediate.created)
        self.assertEqual(Tag.objects.get(pk=first.pk).created, first.created)
        self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
        self.assertEqual(KeaDhcpLink.objects.get(pk=mapping.pk).created, mapping.created)
        self.assertIsNone(current_request.get())

    def test_mixed_tags_refuse_mapped_target_round_trip_replay(self):
        from extras.models import Tag

        for strategy in ("iterative", "squash"):
            with self.subTest(strategy=strategy):
                _target, _mapping, _first, _intermediate, unrelated, branch = self._tag_round_trip(
                    f"mixed-round-trip-{strategy}", strategy
                )
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = Tag.objects.get(pk=unrelated.pk)
                    local.snapshot()
                    local.color = "112233"
                    local.save()
                request_finished.send(sender=type(self))
                self._assert_tag_replay_refused(branch)

    def test_mixed_tags_cover_iterative_created_then_deleted_targets_and_squash_skips(self):
        from extras.models import Tag

        from netbox_kea.dhcp_mapping_lifecycle import observe_mapping

        for strategy in ("iterative", "squash"):
            with self.subTest(strategy=strategy):
                suffix = f"created-skip-{strategy}"
                server, _, _, target, mapping = self._imported("subnet", 4, suffix)
                tag = Tag.objects.create(name=f"mapping-{suffix}-tag", slug=f"mapping-{suffix}-tag")
                branch = _provisioned_branch(self, f"mapping-{suffix}")
                branch.merge_strategy = strategy
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.pk = None
                    local.name = f"mapping-{suffix}-temporary"
                    local.subnet_id = max(type(target).objects.values_list("subnet_id", flat=True), default=0) + 1
                    local.save()
                    local.tags.add(Tag.objects.get(pk=tag.pk))
                    created_pk = local.pk
                    values = {
                        field.attname: getattr(local, field.attname)
                        for field in local._meta.concrete_fields
                        if not field.primary_key and field.name not in {"created", "last_updated"}
                    }
                request_finished.send(sender=type(self))
                self._delete(branch, local)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    endpoint = Tag.objects.get(pk=tag.pk)
                    endpoint.snapshot()
                    endpoint.color = "112233"
                    endpoint.save()
                request_finished.send(sender=type(self))
                collapsed = branching.collapse_changes(branch.get_changes())
                change = next(change for change in collapsed.values() if change.model_class is type(target))
                self.assertEqual(change.final_action, "skip")
                # Main now owns this PK through an explicit source mapping. Squash does not replay its skipped history.
                with event_tracking(_change_request(self.user)):
                    current = type(target).objects.create(pk=created_pk, **values)
                    current.tags.add(tag)
                    mapping = observe_mapping(server, 4, current, subnet_id=current.subnet_id)
                request_finished.send(sender=type(self))
                if strategy == "iterative":
                    self._assert_tag_replay_refused(branch)
                    continue
                writes = []

                def record_target_save(sender, instance, using, *, writes=writes, **kwargs):
                    if using == "default":
                        writes.append(instance.pk)

                pre_save.connect(record_target_save, sender=type(target), weak=False)
                try:
                    branch.merge(user=self.user)
                    self.assertEqual(Tag.objects.get(pk=tag.pk).color, "112233")
                    branch.revert(user=self.user)
                finally:
                    pre_save.disconnect(record_target_save, sender=type(target))
                self.assertFalse(writes)
                self.assertEqual(Tag.objects.get(pk=tag.pk).color, tag.color)
                self.assertEqual(type(target).objects.get(pk=current.pk).created, current.created)
                self.assertEqual(KeaDhcpLink.objects.get(pk=mapping.pk).object_id, current.pk)
                self.assertIsNone(current_request.get())

    def test_iterative_refuses_incomplete_intermediate_tag_history(self):
        for corruption in ("missing", "malformed"):
            with self.subTest(corruption=corruption):
                target, _mapping, _first, _intermediate, _unrelated, branch = self._tag_round_trip(
                    f"raw-history-{corruption}"
                )
                change = (
                    branch.get_changes()
                    .filter(changed_object_type=ContentType.objects.get_for_model(target), changed_object_id=target.pk)
                    .earliest("time")
                )
                # Keep real chronological history except this deliberate incomplete endpoint payload.
                payload = deepcopy(change.postchange_data)
                if corruption == "missing":
                    payload.pop("tags")
                else:
                    payload["tags"] = [{"name": "invalid history"}]
                change.postchange_data = payload
                change.save(update_fields=["postchange_data"])
                before = ObjectChange.objects.count()
                writes = []

                def record_target_save(sender, instance, using, *, writes=writes, **kwargs):
                    if using == "default":
                        writes.append(instance.pk)

                pre_save.connect(record_target_save, sender=type(target), weak=False)
                try:
                    with self.assertRaisesMessage(AbortRequest, "Tag"):
                        branch.merge(user=self.user)
                finally:
                    pre_save.disconnect(record_target_save, sender=type(target))
                self.assertFalse(writes)
                branch.refresh_from_db()
                self.assertEqual(branch.status, "ready")
                self.assertFalse(branch.applied_changes.exists())
                self.assertEqual(ObjectChange.objects.count(), before)
                change.refresh_from_db()
                self.assertEqual(change.postchange_data, payload)
                self.assertIsNone(current_request.get())

    def test_reentrant_mapping_update_during_revert_preserves_newer_source(self):
        server, _, _, target, link = self._imported("subnet", 4, "reentrant-mapping-update")
        merged_source = _make_db_server(name="mapping-merged-source", ca_url="https://kea.example.invalid")
        newer_source = _make_db_server(name="mapping-newer-source", ca_url="https://kea.example.invalid")
        branch = _provisioned_branch(self, "mapping-reentrant-source-update")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = KeaDhcpLink.objects.get(pk=link.pk)
            changed.server = merged_source
            changed.save()
        branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).server_id, merged_source.pk)
        before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
        observed = []

        def changing(sender, operation, **kwargs):
            if operation == "revert":
                with event_tracking(_change_request(self.user)):
                    changed = KeaDhcpLink.objects.get(pk=link.pk)
                    changed.server = newer_source
                    changed.save()
                observed.append(KeaDhcpLink.objects.get(pk=link.pk).server_id)

        squash_dependency_graph_built.connect(changing, weak=False)
        try:
            with self.assertRaisesMessage(AbortRequest, "has changed"):
                branch.revert(user=self.user)
        finally:
            squash_dependency_graph_built.disconnect(changing)
        self.assertEqual(observed, [newer_source.pk])
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).server_id, merged_source.pk)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).object_id, target.pk)
        self.assertIsNone(current_request.get())
        branch.revert(user=self.user)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).server_id, server.pk)
        self.assertIsNone(current_request.get())

    def test_mapping_observation_during_revert_preserves_native_update(self):
        from netbox_kea.dhcp_mapping_lifecycle import observe_mapping

        server, _, _, target, link = self._imported("subnet", 4, "mapping-revert-observation")
        merged_source = _make_db_server(name="mapping-observed-source", ca_url="https://kea.example.invalid")
        branch = _provisioned_branch(self, "mapping-revert-observation")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = KeaDhcpLink.objects.get(pk=link.pk)
            changed.server = merged_source
            changed.save()
        branch.merge(user=self.user)
        branch.refresh_from_db()
        observed = []

        def observing(sender, operation, **kwargs):
            if operation == "revert":
                before = ObjectChange.objects.count()
                previous = KeaDhcpLink.objects.get(pk=link.pk).last_synced
                with event_tracking(_change_request(self.user)):
                    mapping = observe_mapping(merged_source, 4, target, subnet_id=7)
                self.assertEqual(mapping.pk, link.pk)
                self.assertGreater(mapping.last_synced, previous)
                self.assertEqual(ObjectChange.objects.count(), before)
                observed.append(mapping.server_id)

        squash_dependency_graph_built.connect(observing, weak=False)
        try:
            branch.revert(user=self.user)
        finally:
            squash_dependency_graph_built.disconnect(observing)
        self.assertEqual(observed, [merged_source.pk])
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).server_id, server.pk)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).object_id, target.pk)
        self.assertIsNone(current_request.get())

    def test_reentrant_mapping_source_update_refuses_target_only_revert(self):
        server, _, _, target, link = self._imported("subnet", 4, "reentrant-target-source-update")
        newer_source = _make_db_server(name="target-newer-source", ca_url="https://kea.example.invalid")
        target.valid_lifetime = 100
        target.save()
        branch = _provisioned_branch(self, "mapping-reentrant-target-source-update")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.valid_lifetime = 200
            changed.save()
        branch.merge(user=self.user)
        branch.refresh_from_db()
        before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
        observed = []

        def changing(sender, operation, **kwargs):
            if operation == "revert":
                with event_tracking(_change_request(self.user)):
                    changed = KeaDhcpLink.objects.get(pk=link.pk)
                    changed.server = newer_source
                    changed.save()
                observed.append(KeaDhcpLink.objects.get(pk=link.pk).server_id)
                self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 200)

        squash_dependency_graph_built.connect(changing, weak=False)
        try:
            with self.assertRaisesMessage(AbortRequest, "has changed"):
                branch.revert(user=self.user)
        finally:
            squash_dependency_graph_built.disconnect(changing)
        self.assertEqual(observed, [newer_source.pk])
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).server_id, server.pk)
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 200)
        self.assertIsNone(current_request.get())
        branch.revert(user=self.user)
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, 100)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).server_id, server.pk)
        self.assertIsNone(current_request.get())

    def test_squash_restores_global_reservations(self):
        for family in (4, 6):
            for queryset in (False, True):
                with self.subTest(family=family, queryset=queryset):
                    self._lifecycle("reservation", family, queryset)

    def test_discard_preserves_main_target_and_mapping(self):
        for kind in ("subnet", "reservation"):
            for family in (4, 6):
                with self.subTest(kind=kind, family=family):
                    suffix = f"discard-{kind}-{family}"
                    _, _, _, target, link = self._imported(kind, family, suffix)
                    branch = _provisioned_branch(self, f"mapping-{suffix}")
                    self._delete(branch, target)
                    with activate_branch(branch):
                        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
                        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
                    branch.deprovision()
                    self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
                    self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_old_branch_tab_explains_the_fresh_mapping_schema(self):
        server, _, _, target, link = self._imported("subnet", 4, "old-tab")
        branch = _provisioned_branch(self, "mapping-old-tab")
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS "' + branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')
        self.client.force_login(self.user)
        self.client.cookies[COOKIE_NAME] = branch.schema_id
        with stub_kea(_recorded_kea()):
            response = self.client.get(reverse("plugins:netbox_kea:server_dhcp_plugin", args=[server.pk]))
        self.assertContains(response, "Create a fresh branch")
        self.assertNotContains(response, "No DHCP versions enabled")
        self.assertTrue(type(target).objects.using("default").filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.using("default").filter(pk=link.pk).exists())
        with stub_kea(_recorded_kea()):
            response = self.client.get(reverse("plugins:netbox_kea:server_list"))
        self.assertEqual(response.status_code, 200)

    def test_fresh_branch_tab_checks_the_mapping_schema_once(self):
        from django.test.utils import CaptureQueriesContext

        from netbox_kea.dhcp_mapping_lifecycle import target_models

        server, _, _, _, _ = self._imported("subnet", 4, "fresh-tab")
        branch = _provisioned_branch(self, "mapping-fresh-tab")
        self.client.force_login(self.user)
        self.client.cookies[COOKIE_NAME] = branch.schema_id
        with stub_kea(_recorded_kea()), CaptureQueriesContext(connections["default"]) as queries:
            response = self.client.get(reverse("plugins:netbox_kea:server_dhcp_plugin", args=[server.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Create a fresh branch")
        catalog = [query for query in queries.captured_queries if "pg_attribute" in query["sql"]]
        # compute_drift, the guarded KeaDhcpLink query of each family, and the sources header: one round each.
        self.assertEqual(len(catalog), 4 * (1 + len(target_models())))

    def test_affected_iterative_merge_refuses_before_mutation(self):
        _, _, _, target, link = self._imported("subnet", 4, "iterative")
        branch = _provisioned_branch(self, "mapping-iterative")
        branch.merge_strategy = "iterative"
        branch.save(provision=False)
        self._delete(branch, target)
        with self.assertRaisesMessage(AbortRequest, "squash"):
            branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.merge_strategy, "iterative")
        self.assertEqual(branch.status, "ready")
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_old_branch_target_delete_refuses_before_collecting(self):
        _, _, _, target, link = self._imported("subnet", 4, "old-delete")
        branch = _provisioned_branch(self, "mapping-old-delete")
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "' + branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            with self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
                type(target).objects.filter(pk=target.pk).delete()
            self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_old_branch_mapping_query_refuses_main_fallback(self):
        _, _, _, _, link = self._imported("subnet", 6, "old-read")
        branch = _provisioned_branch(self, "mapping-old-read")
        with connection.cursor() as cursor:
            cursor.execute(
                'ALTER TABLE "' + branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '" DROP COLUMN last_updated'
            )
        with activate_branch(branch), self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
            KeaDhcpLink.objects.filter(pk=link.pk).exists()

    def test_old_branch_mapping_save_refuses_main_fallback(self):
        _, _, _, _, link = self._imported("subnet", 4, "old-save")
        branch = _provisioned_branch(self, "mapping-old-save")
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "' + branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')
        replacement = KeaDhcpLink(
            pk=link.pk,
            server_id=link.server_id,
            family=link.family,
            kea_subnet_id=99,
            object_type_id=link.object_type_id,
            object_id=link.object_id,
            created=link.created,
        )
        with activate_branch(branch), self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
            replacement.save()
        link.refresh_from_db(using="default")
        self.assertEqual(link.kea_subnet_id, 7)

    def test_mapping_iterator_checks_the_branch_when_consumed(self):
        _, _, _, _, link = self._imported("subnet", 4, "old-iterator")
        rows = KeaDhcpLink.objects.filter(pk=link.pk).iterator()
        branch = _provisioned_branch(self, "mapping-old-iterator")
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "' + branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')
        with activate_branch(branch), self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
            next(rows)

    def test_mapping_async_iterator_checks_the_branch_when_consumed(self):
        _, _, _, _, link = self._imported("subnet", 4, "old-async-iterator")
        rows = KeaDhcpLink.objects.filter(pk=link.pk).aiterator()
        branch = _provisioned_branch(self, "mapping-old-async-iterator")
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "' + branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')

        async def consume(iterator):
            return [row.pk async for row in iterator]

        with self.subTest(alias="active"):
            with activate_branch(branch), self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
                async_to_sync(consume)(rows)
        explicit = KeaDhcpLink.objects.using(branch.connection_name).filter(pk=link.pk).aiterator()
        with self.subTest(alias="explicit"), self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
            async_to_sync(consume)(explicit)
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_mapping_async_iterator_preserves_main_and_fresh_branch_reads(self):
        _, _, _, _, link = self._imported("subnet", 6, "fresh-async-iterator")
        branch = _provisioned_branch(self, "mapping-fresh-async-iterator")
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            local = KeaDhcpLink.objects.get(pk=link.pk)
            local.kea_subnet_id = 8
            local.save()

        async def consume(iterator):
            return [row.kea_subnet_id async for row in iterator]

        self.assertEqual(async_to_sync(consume)(KeaDhcpLink.objects.filter(pk=link.pk).aiterator()), [7])
        rows = KeaDhcpLink.objects.filter(pk=link.pk).aiterator()
        with activate_branch(branch):
            self.assertEqual(async_to_sync(consume)(rows), [8])
        explicit = KeaDhcpLink.objects.using(branch.connection_name).filter(pk=link.pk).aiterator()
        self.assertEqual(async_to_sync(consume)(explicit), [8])

    def test_native_endpoint_deletion_composes_with_mapped_target_cleanup(self):
        from netbox_dhcp.models import ClientClass

        for kind in ("client-class", "mac-address"):
            with self.subTest(kind=kind):
                _, _, _, target, link = self._imported(
                    "subnet" if kind == "client-class" else "reservation", 4, f"endpoint-cleanup-{kind}"
                )
                if kind == "client-class":
                    endpoint = ClientClass.objects.create(
                        name=f"mapping-cleanup-{kind}", dhcp_server=target.dhcp_server
                    )
                    target.client_classes.add(endpoint)
                else:
                    endpoint = target.hw_address
                model = type(endpoint)
                branch = _provisioned_branch(self, f"mapping-endpoint-cleanup-{kind}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                self._delete(branch, endpoint)
                if kind == "mac-address":
                    with activate_branch(branch), event_tracking(_change_request(self.user)):
                        local = type(target).objects.get(pk=target.pk)
                        local.snapshot()
                        local.description = "nullable endpoint cleanup"
                        local.save()
                    request_finished.send(sender=type(self))
                with activate_branch(branch):
                    local = type(target).objects.get(pk=target.pk)
                    self.assertFalse(local.client_classes.exists() if kind == "client-class" else local.hw_address_id)
                branch.merge(user=self.user)
                merged = type(target).objects.get(pk=target.pk)
                self.assertFalse(merged.client_classes.exists() if kind == "client-class" else merged.hw_address_id)
                self.assertFalse(model.objects.filter(pk=endpoint.pk).exists())
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
                branch.revert(user=self.user)
                restored = type(target).objects.get(pk=target.pk)
                if kind == "client-class":
                    self.assertEqual(list(restored.client_classes.values_list("pk", flat=True)), [endpoint.pk])
                else:
                    self.assertEqual(restored.hw_address_id, endpoint.pk)
                    self.assertEqual(restored.description, target.description)
                self.assertTrue(model.objects.filter(pk=endpoint.pk).exists())
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_native_endpoint_cleanup_composes_with_mapped_target_deletion(self):
        from netbox_dhcp.models import ClientClass

        _, _, _, target, link = self._imported("subnet", 6, "endpoint-target-delete")
        endpoint = ClientClass.objects.create(name="mapping-cleanup-target-delete", dhcp_server=target.dhcp_server)
        target.client_classes.add(endpoint)
        branch = _provisioned_branch(self, "mapping-endpoint-target-delete")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, endpoint)
        self._delete(branch, target)
        branch.merge(user=self.user)
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(ClientClass.objects.filter(pk=endpoint.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        branch.revert(user=self.user)
        restored = type(target).objects.get(pk=target.pk)
        self.assertEqual(list(restored.client_classes.values_list("pk", flat=True)), [endpoint.pk])
        self.assertTrue(ClientClass.objects.filter(pk=endpoint.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def _cleanup_target(self, kind, suffix):
        from extras.models import Tag
        from netbox_dhcp.models import ClientClass

        _, _, _, target, link = self._imported("reservation" if kind == "reservation" else "subnet", 4, suffix)
        endpoints = [
            Tag.objects.create(name=f"mapping-{suffix}-{name}", slug=f"mapping-{suffix}-{name}")
            if kind == "tags"
            else ClientClass.objects.create(name=f"mapping-{suffix}-{name}", dhcp_server=target.dhcp_server)
            for name in ("a", "b", "c")
        ]
        field = "tags" if kind == "tags" else "client_classes"
        getattr(target, field).set(endpoints[:2])
        branch = _provisioned_branch(self, f"mapping-{suffix}")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        # Capture the target's original scalar and relation state before endpoint deletion.
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            local = type(target).objects.get(pk=target.pk)
            local.snapshot()
            local.description = "starting cleanup"
            local.save()
        request_finished.send(sender=type(self))
        return target, link, endpoints, field, branch

    def test_multiple_native_endpoint_cleanup_preserves_original_history_and_request_ids(self):
        from netbox_kea.dhcp_mapping_lifecycle import _replay_operation

        for kind in ("classes", "tags"):
            with self.subTest(kind=kind):
                target, link, endpoints, field, branch = self._cleanup_target(kind, f"multiple-cleanup-{kind}")
                for endpoint in endpoints[:2]:
                    self._delete(branch, endpoint)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.snapshot()
                    local.description = "completed cleanup"
                    local.save()
                request_finished.send(sender=type(self))
                if kind == "tags":
                    self._assert_tag_replay_refused(branch)
                    continue
                history = {change.pk: deepcopy(change.prechange_data) for change in branch.get_changes()}
                captured = []

                def capture(
                    sender, instance, using, *, endpoints=endpoints, captured=captured, target=target, **kwargs
                ):
                    replay = _replay_operation.get()
                    if using == "default" and replay is not None:
                        facts = [
                            replay.endpoint_deletions.get((type(endpoint), endpoint.pk)) for endpoint in endpoints[:2]
                        ]
                        captured.append(
                            (
                                deepcopy(replay.object_states[(type(target), target.pk)]),
                                [fact.request_id for fact in facts if fact is not None],
                                current_request.get().id,
                            )
                        )

                pre_save.connect(capture, sender=type(target), weak=False)
                try:
                    branch.merge(user=self.user)
                finally:
                    pre_save.disconnect(capture, sender=type(target))
                merged = type(target).objects.get(pk=target.pk)
                self.assertFalse(getattr(merged, field).exists())
                self.assertEqual(merged.description, "completed cleanup")
                self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, link.created)
                self.assertTrue(captured)
                for original, deletion_ids, target_id in captured:
                    self.assertEqual(len(original[field]), 2)
                    self.assertEqual(len(set(deletion_ids)), 2)
                    self.assertNotIn(target_id, deletion_ids)
                self.assertEqual({change.pk: change.prechange_data for change in branch.get_changes()}, history)
                branch.revert(user=self.user)
                restored = type(target).objects.get(pk=target.pk)
                self.assertEqual(
                    set(getattr(restored, field).values_list("pk", flat=True)), {e.pk for e in endpoints[:2]}
                )
                self.assertEqual(restored.description, target.description)

    def test_named_endpoint_creation_refuses_mapped_target_update(self):
        from extras.models import Tag

        _, _, _, target, _link = self._imported("subnet", 6, "created-named-endpoint")
        branch = _provisioned_branch(self, "mapping-created-named-endpoint")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            tag = Tag.objects.create(name="mapping-created-endpoint", slug="mapping-created-endpoint")
            local = type(target).objects.get(pk=target.pk)
            local.snapshot()
            local.tags.add(tag)
        request_finished.send(sender=type(self))
        self._assert_tag_replay_refused(branch)

    def _renamed_tag_target(self, kind, family, suffix, *, assigned, strategy):
        from extras.models import Tag

        _, _, _, target, link = self._imported(kind, family, suffix)
        tag = Tag.objects.create(name=f"mapping-{suffix}-original", slug=f"mapping-{suffix}")
        if assigned:
            target.tags.add(tag)
        branch = _provisioned_branch(self, f"mapping-{suffix}")
        branch.merge_strategy = strategy
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            renamed = Tag.objects.get(pk=tag.pk)
            renamed.snapshot()
            renamed.name = f"mapping-{suffix}-renamed"
            renamed.save()
        request_finished.send(sender=type(self))
        return target, link, tag, renamed, branch

    def _assert_tag_replay_refused(self, branch):
        from extras.models import Tag

        from netbox_kea.dhcp_mapping_lifecycle import target_models

        models = (*target_models(), Tag, KeaDhcpLink)

        def main_state():
            return {
                model: [
                    (obj.pk, obj.serialize_object(), obj.created)
                    for obj in model.objects.using("default").order_by("pk")
                ]
                for model in models
            }

        state = main_state()
        history = {
            change.pk: (deepcopy(change.prechange_data), deepcopy(change.postchange_data))
            for change in branch.get_changes()
        }
        original_status = branch.status
        changes = ObjectChange.objects.count()
        applied = branch.applied_changes.count()
        mutations = []

        def capture_mutation(sender, instance, using, **kwargs):
            if using == "default":
                mutations.append((sender, instance.pk))

        def capture_status(execute, sql, params, many, context):
            if sql.lstrip().startswith("UPDATE") and branch._meta.db_table in sql and '"status"' in sql:
                mutations.append("branch status")
            return execute(sql, params, many, context)

        for model in models:
            pre_save.connect(capture_mutation, sender=model, weak=False)
            pre_delete.connect(capture_mutation, sender=model, weak=False)
        try:
            for commit in (True, False):
                with (
                    connection.execute_wrapper(capture_status),
                    self.assertRaisesMessage(AbortRequest, "Apply Tag changes separately") as refusal,
                ):
                    branch.merge(user=self.user, commit=commit)
                self.assertIn("fresh branch", str(refusal.exception))
                branch.refresh_from_db()
                self.assertEqual(branch.status, original_status)
                self.assertEqual(branch.applied_changes.count(), applied)
                self.assertEqual(ObjectChange.objects.count(), changes)
                self.assertEqual(main_state(), state)
                self.assertEqual(
                    {change.pk: (change.prechange_data, change.postchange_data) for change in branch.get_changes()},
                    history,
                )
                self.assertFalse(mutations, "Tag refusal occurred after a native mutation")
                self.assertIsNone(current_request.get())
        finally:
            for model in models:
                pre_save.disconnect(capture_mutation, sender=model)
                pre_delete.disconnect(capture_mutation, sender=model)

    def test_branch_tag_rename_then_assignment_refuses_mapped_target_replay(self):
        from extras.models import Tag

        for kind in ("subnet", "reservation"):
            for family in (4, 6):
                with self.subTest(kind=kind, family=family):
                    suffix = f"rename-assign-{kind}-{family}"
                    target, _link, original, _renamed, branch = self._renamed_tag_target(
                        kind, family, suffix, assigned=False, strategy="squash"
                    )
                    with activate_branch(branch), event_tracking(_change_request(self.user)):
                        local = type(target).objects.get(pk=target.pk)
                        local.snapshot()
                        local.tags.add(Tag.objects.get(pk=original.pk))
                    request_finished.send(sender=type(self))
                    self._assert_tag_replay_refused(branch)

    def test_branch_tag_rename_then_scalar_update_refuses_mapped_target_replay(self):

        for strategy in ("squash", "iterative"):
            for kind, family in (("subnet", 4), ("reservation", 6)):
                with self.subTest(strategy=strategy, kind=kind, family=family):
                    suffix = f"rename-scalar-{strategy}-{kind}-{family}"
                    target, _link, _original, _renamed, branch = self._renamed_tag_target(
                        kind, family, suffix, assigned=True, strategy=strategy
                    )
                    with activate_branch(branch), event_tracking(_change_request(self.user)):
                        local = type(target).objects.get(pk=target.pk)
                        local.snapshot()
                        local.description = "branch scalar after rename"
                        local.save()
                    request_finished.send(sender=type(self))
                    self._assert_tag_replay_refused(branch)

    def test_branch_tag_rename_without_target_change_preserves_native_identity(self):
        from extras.models import Tag

        target, link, original, renamed, branch = self._renamed_tag_target(
            "subnet", 4, "rename-only", assigned=True, strategy="squash"
        )
        branch.merge(user=self.user)
        self.assertEqual(
            list(type(target).objects.get(pk=target.pk).tags.values_list("name", flat=True)), [renamed.name]
        )
        self.assertEqual(Tag.objects.get(pk=original.pk).created, original.created)
        branch.revert(user=self.user)
        self.assertEqual(
            list(type(target).objects.get(pk=target.pk).tags.values_list("name", flat=True)), [original.name]
        )
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, link.created)

    def test_tag_rename_between_target_updates_refuses_mapped_target_replay(self):
        from extras.models import Tag

        _, _, _, target, _link = self._imported("subnet", 4, "target-rename-target")
        tag = Tag.objects.create(name="mapping-target-rename-original", slug="mapping-target-rename")
        target.tags.add(tag)
        branch = _provisioned_branch(self, "mapping-target-rename-target")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        for description, name in (
            ("first branch edit", None),
            (None, "mapping-target-rename-final"),
            ("last branch edit", None),
        ):
            with activate_branch(branch), event_tracking(_change_request(self.user)):
                local = (
                    type(target).objects.get(pk=target.pk) if description is not None else Tag.objects.get(pk=tag.pk)
                )
                local.snapshot()
                if description is not None:
                    local.description = description
                else:
                    local.name = name
                local.save()
            request_finished.send(sender=type(self))
        self._assert_tag_replay_refused(branch)

    def test_tag_renames_around_assignment_refuse_mapped_target_replay(self):
        from extras.models import Tag

        target, _link, original, _renamed, branch = self._renamed_tag_target(
            "reservation", 6, "rename-assignment-rename", assigned=False, strategy="squash"
        )
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            local = type(target).objects.get(pk=target.pk)
            local.snapshot()
            local.tags.add(Tag.objects.get(pk=original.pk))
        request_finished.send(sender=type(self))
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            endpoint = Tag.objects.get(pk=original.pk)
            endpoint.snapshot()
            endpoint.name = "mapping-rename-assignment-final"
            endpoint.save()
        request_finished.send(sender=type(self))
        self._assert_tag_replay_refused(branch)

    def test_existing_tag_assignment_preserves_native_target_identity(self):
        from extras.models import Tag

        for kind, family in (("subnet", 4), ("subnet", 6), ("reservation", 4), ("reservation", 6)):
            with self.subTest(kind=kind, family=family):
                suffix = f"existing-tag-{kind}-{family}"
                _, _, _, target, link = self._imported(kind, family, suffix)
                tag = Tag.objects.create(name=f"mapping-{suffix}", slug=f"mapping-{suffix}")
                branch = _provisioned_branch(self, f"mapping-{suffix}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.snapshot()
                    local.tags.add(tag)
                request_finished.send(sender=type(self))
                with event_tracking(_change_request(self.user)):
                    tag.color = "112233"
                    tag.save()
                request_finished.send(sender=type(self))
                branch.merge(user=self.user)
                self.assertEqual(
                    list(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)), [tag.pk]
                )
                branch.revert(user=self.user)
                self.assertFalse(type(target).objects.get(pk=target.pk).tags.exists())
                self.assertEqual(Tag.objects.get(pk=tag.pk).color, "112233")
                self.assertEqual(Tag.objects.get(pk=tag.pk).created, tag.created)
                self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
                self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, link.created)

    def test_mapping_only_replay_allows_separate_tag_changes(self):
        from extras.models import Tag

        _, _, _, target, link = self._imported("reservation", 6, "mapping-only-tags")
        tag = Tag.objects.create(name="mapping-only-tags-original", slug="mapping-only-tags")
        branch = _provisioned_branch(self, "mapping-only-tags")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            local = Tag.objects.get(pk=tag.pk)
            local.snapshot()
            local.name = "mapping-only-tags-renamed"
            local.save()
            KeaDhcpLink.objects.filter(pk=link.pk).delete()
        request_finished.send(sender=type(self))
        branch.merge(user=self.user)
        self.assertEqual(Tag.objects.get(pk=tag.pk).name, "mapping-only-tags-renamed")
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        branch.revert(user=self.user)
        self.assertEqual(Tag.objects.get(pk=tag.pk).name, tag.name)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, link.created)
        self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)

    def test_unmapped_target_and_tag_changes_preserve_native_replay(self):
        from extras.models import Tag

        for strategy in ("squash", "iterative"):
            with self.subTest(strategy=strategy):
                _, _, _, target, link = self._imported("subnet", 4, f"unmapped-tag-{strategy}")
                tag = Tag.objects.create(name=f"mapping-unmapped-{strategy}", slug=f"mapping-unmapped-{strategy}")
                target.tags.add(tag)
                with event_tracking(_change_request(self.user)):
                    link.delete()
                request_finished.send(sender=type(self))
                branch = _provisioned_branch(self, f"mapping-unmapped-tag-{strategy}")
                branch.merge_strategy = strategy
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = Tag.objects.get(pk=tag.pk)
                    local.snapshot()
                    local.name = f"mapping-unmapped-renamed-{strategy}"
                    local.save()
                request_finished.send(sender=type(self))
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.snapshot()
                    local.description = "unmapped native edit"
                    local.save()
                request_finished.send(sender=type(self))
                branch.merge(user=self.user)
                self.assertEqual(type(target).objects.get(pk=target.pk).description, "unmapped native edit")
                self.assertEqual(
                    list(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)), [tag.pk]
                )
                branch.revert(user=self.user)
                self.assertEqual(type(target).objects.get(pk=target.pk).description, target.description)
                self.assertEqual(Tag.objects.get(pk=tag.pk).name, tag.name)
                self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
                self.assertEqual(Tag.objects.get(pk=tag.pk).created, tag.created)
                self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
                self.assertIsNone(current_request.get())

    def test_unavailable_branch_tag_copy_refuses_mapped_replay(self):
        from extras.models import Tag

        for unavailable in ("table", "exemption"):
            with self.subTest(unavailable=unavailable):
                _, _, _, target, link = self._imported("subnet", 4, f"tag-copy-{unavailable}")
                tag = Tag.objects.create(name=f"mapping-copy-{unavailable}", slug=f"mapping-copy-{unavailable}")
                target.tags.add(tag)
                branch = _provisioned_branch(self, f"mapping-tag-copy-{unavailable}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.snapshot()
                    local.description = "branch with unchanged Tag"
                    local.save()
                request_finished.send(sender=type(self))
                config = deepcopy(settings.PLUGINS_CONFIG)
                if unavailable == "table":
                    with connection.cursor() as cursor:
                        cursor.execute('DROP TABLE "' + branch.schema_name + '"."' + Tag._meta.db_table + '" CASCADE')
                else:
                    config["netbox_branching"]["exempt_models"] = [
                        *config["netbox_branching"].get("exempt_models", []),
                        "extras.tag",
                    ]
                before = ObjectChange.objects.count()
                instruction = (
                    "Remove the affected Tag branching exemption"
                    if unavailable == "exemption"
                    else "Apply Tag changes separately"
                )
                with (
                    override_settings(PLUGINS_CONFIG=config),
                    self.assertRaisesMessage(AbortRequest, instruction) as refusal,
                ):
                    branch.merge(user=self.user)
                self.assertIn("fresh branch", str(refusal.exception))
                branch.refresh_from_db()
                self.assertEqual(branch.status, "ready")
                self.assertFalse(branch.applied_changes.exists())
                self.assertEqual(ObjectChange.objects.count(), before)
                self.assertEqual(type(target).objects.get(pk=target.pk).description, target.description)
                self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, link.created)
                self.assertEqual(Tag.objects.get(pk=tag.pk).created, tag.created)
                self.assertIsNone(current_request.get())

    def test_distinct_tag_renames_with_a_reused_name_refuse_mapped_target_replay(self):
        from extras.models import Tag

        for strategy in ("squash", "iterative"):
            with self.subTest(strategy=strategy):
                _, _, _, target, _link = self._imported("reservation", 4, f"reused-tag-name-{strategy}")
                first = Tag.objects.create(
                    name=f"mapping-tag-name-{strategy}-first", slug=f"mapping-name-{strategy}-first"
                )
                assigned = Tag.objects.create(
                    name=f"mapping-tag-name-{strategy}-assigned", slug=f"mapping-name-{strategy}-assigned"
                )
                target.tags.add(assigned)
                branch = _provisioned_branch(self, f"mapping-reused-tag-name-{strategy}")
                branch.merge_strategy = strategy
                branch.save(provision=False)
                for tag, name in ((first, f"mapping-tag-name-{strategy}-vacated"), (assigned, first.name)):
                    with activate_branch(branch), event_tracking(_change_request(self.user)):
                        local = Tag.objects.get(pk=tag.pk)
                        local.snapshot()
                        local.name = name
                        local.save()
                    request_finished.send(sender=type(self))
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.snapshot()
                    local.description = "branch reused-name metadata"
                    local.save()
                request_finished.send(sender=type(self))
                self._assert_tag_replay_refused(branch)

    def test_tag_changes_before_assignment_refuse_and_later_changes_remain_main(self):
        from extras.models import Tag

        for timing in ("before-assignment", "after-assignment"):
            with self.subTest(timing=timing):
                _, _, _, target, link = self._imported("subnet", 6, f"tag-assignment-{timing}")
                tag = Tag.objects.create(name=f"mapping-assignment-{timing}", slug=f"mapping-assignment-{timing}")
                branch = _provisioned_branch(self, f"mapping-assignment-{timing}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.snapshot()
                    local.description = "branch assignment"
                    local.save()
                    local.tags.add(Tag.objects.get(pk=tag.pk))
                request_finished.send(sender=type(self))
                observed = []

                def rename_main(sender, instance, using, *, tag=tag, observed=observed, timing=timing, **kwargs):
                    if using != "default" or kwargs.get("action") not in (None, "post_add") or observed:
                        return
                    with event_tracking(_change_request(self.user)):
                        endpoint = Tag.objects.get(pk=tag.pk)
                        endpoint.name = f"mapping-later-{timing}"
                        endpoint.save()
                    observed.append(endpoint.name)

                signal = post_save if timing == "before-assignment" else m2m_changed
                sender = type(target) if timing == "before-assignment" else type(target)._meta.get_field("tags").through
                signal.connect(rename_main, sender=sender, weak=False)
                before = ObjectChange.objects.count()
                try:
                    if timing == "before-assignment":
                        with self.assertRaisesMessage(AbortRequest, "changed"):
                            branch.merge(user=self.user)
                    else:
                        branch.merge(user=self.user)
                finally:
                    signal.disconnect(rename_main, sender=sender)
                self.assertEqual(observed, [f"mapping-later-{timing}"])
                branch.refresh_from_db()
                current = type(target).objects.get(pk=target.pk)
                if timing == "before-assignment":
                    self.assertEqual(branch.status, "ready")
                    self.assertEqual(ObjectChange.objects.count(), before)
                    self.assertFalse(branch.applied_changes.exists())
                    self.assertFalse(current.tags.exists())
                    self.assertEqual(current.description, target.description)
                    self.assertEqual(Tag.objects.get(pk=tag.pk).name, tag.name)
                else:
                    self.assertEqual(branch.status, "merged")
                    self.assertEqual(list(current.tags.values_list("pk", "name")), [(tag.pk, observed[0])])
                    self.assertEqual(current.description, "branch assignment")
                    applied = branch.applied_changes.count()
                    with self.assertRaisesMessage(AbortRequest, "changed"):
                        branch.revert(user=self.user)
                    self.assertEqual(branch.applied_changes.count(), applied)
                    self.assertEqual(Tag.objects.get(pk=tag.pk).name, observed[0])
                self.assertEqual(current.created, target.created)
                self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, link.created)
                self.assertIsNone(current_request.get())

    def test_unrelated_metadata_and_skipped_tag_changes_refuse_mapped_replay(self):
        from extras.models import Tag

        for strategy in ("squash", "iterative"):
            for change_kind in ("rename", "metadata", "skipped"):
                with self.subTest(strategy=strategy, change_kind=change_kind):
                    suffix = f"mixed-tags-{strategy}-{change_kind}"
                    _, _, _, target, _link = self._imported("reservation", 6, suffix)
                    tag = Tag.objects.create(name=f"mapping-{suffix}", slug=f"mapping-{suffix}")
                    branch = _provisioned_branch(self, f"mapping-{suffix}")
                    branch.merge_strategy = strategy
                    branch.save(provision=False)
                    with activate_branch(branch), event_tracking(_change_request(self.user)):
                        if change_kind == "skipped":
                            transient = Tag.objects.create(
                                name=f"mapping-{suffix}-temporary", slug=f"mapping-{suffix}-temporary"
                            )
                            transient.delete()
                        else:
                            local_tag = Tag.objects.get(pk=tag.pk)
                            local_tag.snapshot()
                            if change_kind == "rename":
                                local_tag.name = f"mapping-{suffix}-renamed"
                            else:
                                local_tag.color = "abcdef"
                            local_tag.save()
                        local = type(target).objects.get(pk=target.pk)
                        local.snapshot()
                        local.description = "mapped branch edit"
                        local.save()
                    request_finished.send(sender=type(self))
                    self._assert_tag_replay_refused(branch)

    def _mapping_only_branch(self, kind, family, suffix):
        _, _, _, target, link = self._imported(kind, family, suffix)
        unrelated = VRF.objects.create(name=f"mapping-{suffix}-unrelated")
        branch = _provisioned_branch(self, f"mapping-{suffix}")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, link, queryset=True)
        self._delete(branch, unrelated, queryset=True)
        branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertFalse(VRF.objects.filter(pk=unrelated.pk).exists())
        self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
        with activate_branch(branch):
            self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
        return target, link, unrelated, branch

    def _replace_unmapped_target(self, target):
        with event_tracking(_change_request(self.user)):
            type(target).objects.filter(pk=target.pk).delete()
            values = {
                field.attname: getattr(target, field.attname)
                for field in target._meta.concrete_fields
                if not field.primary_key and field.name not in {"created", "last_updated"}
            }
            replacement = type(target).objects.create(pk=target.pk, **values)
        self.assertNotEqual(replacement.created, target.created)
        return replacement

    def test_mapping_only_revert_refuses_replacement_target_generation(self):
        for phase in ("before", "after"):
            for kind, family in (("subnet", 4), ("subnet", 6), ("reservation", 4), ("reservation", 6)):
                with self.subTest(phase=phase, kind=kind, family=family):
                    target, link, unrelated, branch = self._mapping_only_branch(
                        kind, family, f"mapping-generation-{phase}-{kind}-{family}"
                    )
                    observed = []
                    if phase == "before":
                        replacement = self._replace_unmapped_target(target)
                        request_finished.send(sender=type(self))

                    def replace_during_replay(sender, operation, *, target=target, observed=observed, **kwargs):
                        if operation == "revert":
                            observed.append(self._replace_unmapped_target(target).created)

                    before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
                    if phase == "after":
                        squash_dependency_graph_built.connect(replace_during_replay, weak=False)
                    try:
                        with self.assertRaisesMessage(AbortRequest, "reused"):
                            branch.revert(user=self.user)
                    finally:
                        squash_dependency_graph_built.disconnect(replace_during_replay)
                    branch.refresh_from_db()
                    self.assertEqual(branch.status, "merged")
                    self.assertEqual(branch.applied_changes.count(), applied)
                    self.assertEqual(ObjectChange.objects.count(), before)
                    expected_created = replacement.created if phase == "before" else target.created
                    self.assertEqual(type(target).objects.get(pk=target.pk).created, expected_created)
                    if phase == "after":
                        self.assertEqual(len(observed), 1)
                        self.assertNotEqual(observed[0], target.created)
                    self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
                    self.assertFalse(VRF.objects.filter(pk=unrelated.pk).exists())
                    self.assertIsNone(current_request.get())

    def test_mapping_only_revert_preserves_original_generation_and_new_scalar_data(self):
        for kind, family in (("subnet", 4), ("subnet", 6), ("reservation", 4), ("reservation", 6)):
            with self.subTest(kind=kind, family=family):
                target, link, unrelated, branch = self._mapping_only_branch(
                    kind, family, f"mapping-unchanged-generation-{kind}-{family}"
                )
                with event_tracking(_change_request(self.user)):
                    current = type(target).objects.get(pk=target.pk)
                    current.snapshot()
                    current.description = "retained main metadata"
                    current.save()
                request_finished.send(sender=type(self))
                branch.revert(user=self.user)
                restored = KeaDhcpLink.objects.get(pk=link.pk)
                self.assertEqual(
                    (restored.object_id, restored.server_id, restored.family, restored.created),
                    (target.pk, link.server_id, family, link.created),
                )
                current = type(target).objects.get(pk=target.pk)
                self.assertEqual(current.created, target.created)
                self.assertEqual(current.description, "retained main metadata")
                self.assertTrue(VRF.objects.filter(pk=unrelated.pk).exists())
                self.assertIsNone(current_request.get())

    def test_revert_refuses_replacement_during_native_target_restoration(self):
        _, _, _, target, mapping = self._imported("subnet", 4, "restore-replacement")
        branch = _provisioned_branch(self, "mapping-restore-replacement")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        observations = []

        def replace_restored(sender, instance, using, created, **kwargs):
            if using != "default" or not created or observations:
                return
            self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
            observations.append(instance.created)
            replacement = self._replace_unmapped_target(instance)
            observations.append(replacement.created)

        post_save.connect(replace_restored, sender=type(target), weak=False)
        changes = ObjectChange.objects.count()
        applied = branch.applied_changes.count()
        try:
            with self.assertRaises(AbortRequest):
                branch.revert(user=self.user)
        finally:
            post_save.disconnect(replace_restored, sender=type(target))
        self.assertTrue(observations)
        if len(observations) == 2:
            self.assertNotEqual(*observations)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), changes)
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
        self.assertIsNone(current_request.get())

    def test_revert_refuses_a_different_creator_in_the_native_request(self):
        _, _, _, target, mapping = self._imported("subnet", 6, "restore-other-creator")
        branch = _provisioned_branch(self, "mapping-restore-other-creator")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        observations = []

        def create_before_native_save(sender, instance, using, **kwargs):
            if using != "default" or observations:
                return
            self.assertIsNotNone(current_request.get())
            self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
            observations.append(current_request.get().id)
            values = {
                field.attname: getattr(instance, field.attname)
                for field in instance._meta.concrete_fields
                if not field.primary_key and field.name not in {"created", "last_updated"}
            }
            replacement = type(target).objects.create(pk=target.pk, **values)
            self.assertIsNot(replacement, instance)
            observations.append(replacement.created)

        pre_save.connect(create_before_native_save, sender=type(target), weak=False)
        before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
        try:
            with self.assertRaises(AbortRequest):
                branch.revert(user=self.user)
        finally:
            pre_save.disconnect(create_before_native_save, sender=type(target))
        self.assertEqual(len(observations), 2)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
        self.assertIsNone(current_request.get())

    def test_mapping_restore_refuses_target_replacement_after_timestamp_reset(self):
        for kind, family in (("subnet", 4), ("subnet", 6), ("reservation", 4), ("reservation", 6)):
            with self.subTest(kind=kind, family=family):
                _, _, _, target, mapping = self._imported(kind, family, f"after-reset-{kind}-{family}")
                branch = _provisioned_branch(self, f"mapping-after-reset-{kind}-{family}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                self._delete(branch, target)
                branch.merge(user=self.user)
                observations = []

                def replace_before_mapping_save(
                    sender, instance, using, *, target=target, observed=observations, **kwargs
                ):
                    if using != "default" or observed:
                        return
                    restored = type(target).objects.get(pk=target.pk)
                    self.assertEqual(restored.created, target.created)
                    observed.append(self._replace_unmapped_target(restored).created)

                pre_save.connect(replace_before_mapping_save, sender=KeaDhcpLink, weak=False)
                before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
                try:
                    with self.assertRaisesMessage(AbortRequest, "reused"):
                        branch.revert(user=self.user)
                finally:
                    pre_save.disconnect(replace_before_mapping_save, sender=KeaDhcpLink)
                self.assertEqual(len(observations), 1)
                self.assertNotEqual(observations[0], target.created)
                branch.refresh_from_db()
                self.assertEqual(branch.status, "merged")
                self.assertEqual(branch.applied_changes.count(), applied)
                self.assertEqual(ObjectChange.objects.count(), before)
                self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
                self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
                self.assertIsNone(current_request.get())

    def test_mapping_create_merge_refuses_target_replacement_inside_native_save(self):
        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                _, _, _, target, original = self._imported("subnet", 4, f"mapping-create-{phase}")
                values = {
                    field.attname: getattr(original, field.attname)
                    for field in original._meta.concrete_fields
                    if not field.primary_key and field.name not in {"created", "last_updated", "last_synced"}
                }
                with event_tracking(_change_request(self.user)):
                    original.delete()
                request_finished.send(sender=type(self))
                branch = _provisioned_branch(self, f"mapping-create-{phase}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    mapping = KeaDhcpLink.objects.create(**values)
                request_finished.send(sender=type(self))
                observations = []

                def replace_during_mapping_save(
                    sender, instance, using, *, target=target, phase=phase, observed=observations, **kwargs
                ):
                    if using != "default" or observed:
                        return
                    self.assertEqual(KeaDhcpLink.objects.filter(pk=instance.pk).exists(), phase == "after")
                    current = type(target).objects.get(pk=target.pk)
                    observed.append(current.created)
                    observed.append(self._replace_unmapped_target(current).created)

                signal = pre_save if phase == "before" else post_save
                signal.connect(replace_during_mapping_save, sender=KeaDhcpLink, weak=False)
                before = ObjectChange.objects.count()
                try:
                    with self.assertRaisesMessage(AbortRequest, "reused" if phase == "before" else "history"):
                        branch.merge(user=self.user)
                finally:
                    signal.disconnect(replace_during_mapping_save, sender=KeaDhcpLink)
                self.assertEqual(len(observations), 2 if phase == "before" else 1)
                self.assertEqual(observations[0], target.created)
                if phase == "before":
                    self.assertNotEqual(observations[1], target.created)
                branch.refresh_from_db()
                self.assertEqual(branch.status, "ready")
                self.assertFalse(branch.applied_changes.exists())
                self.assertEqual(ObjectChange.objects.count(), before)
                self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
                self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
                self.assertIsNone(current_request.get())

    def test_mapping_create_merge_preserves_original_target_generation(self):
        _, _, _, target, original = self._imported("reservation", 6, "mapping-create-unchanged")
        values = {
            field.attname: getattr(original, field.attname)
            for field in original._meta.concrete_fields
            if not field.primary_key and field.name not in {"created", "last_updated", "last_synced"}
        }
        with event_tracking(_change_request(self.user)):
            original.delete()
        request_finished.send(sender=type(self))
        branch = _provisioned_branch(self, "mapping-create-unchanged")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            mapping = KeaDhcpLink.objects.create(**values)
        request_finished.send(sender=type(self))
        branch.merge(user=self.user)
        self.assertEqual(KeaDhcpLink.objects.get(pk=mapping.pk).object_id, target.pk)
        self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
        branch.revert(user=self.user)
        self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
        self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
        self.assertIsNone(current_request.get())

    def test_restoration_without_a_native_birth_receipt_refuses_and_retries_cleanly(self):
        from netbox_branching.models import AppliedChange

        from netbox_kea.dhcp_mapping_lifecycle import _replay_operation

        _, _, _, target, mapping = self._imported("reservation", 4, "missing-native-birth")
        branch = _provisioned_branch(self, "mapping-missing-native-birth")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        before, applied = ObjectChange.objects.count(), branch.applied_changes.count()
        post_save.disconnect(sender=AppliedChange, dispatch_uid="netbox_kea.mapping_applied_change")
        try:
            with self.assertRaisesMessage(AbortRequest, "creator history"):
                branch.revert(user=self.user)
        finally:
            post_save.connect(
                branching._mapping_applied_change,
                sender=AppliedChange,
                dispatch_uid="netbox_kea.mapping_applied_change",
            )
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
        self.assertIsNone(current_request.get())
        self.assertIsNone(_replay_operation.get())
        branch.revert(user=self.user)
        self.assertEqual(type(target).objects.get(pk=target.pk).created, target.created)
        self.assertEqual(KeaDhcpLink.objects.get(pk=mapping.pk).created, mapping.created)
        self.assertIsNone(current_request.get())
        self.assertIsNone(_replay_operation.get())

    def test_named_endpoint_deletion_refuses_mapped_target_deletion(self):
        target, _link, endpoints, _field, branch = self._cleanup_target("tags", "named-target-delete")
        self._delete(branch, endpoints[0])
        self._delete(branch, target)
        self._assert_tag_replay_refused(branch)

    def test_named_endpoint_creation_refuses_mapped_target_creation(self):
        from dcim.models import MACAddress
        from extras.models import Tag
        from netbox_dhcp.models import HostReservation

        server, _, _, original, _original_mapping = self._imported("reservation", 4, "named-target-create")
        mac = MACAddress.objects.create(mac_address="02:00:00:00:00:08")
        branch = _provisioned_branch(self, "mapping-named-target-create")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            target = HostReservation.objects.create(
                name="mapping-created-reservation", dhcp_server=original.dhcp_server, hw_address=mac
            )
            endpoint = Tag.objects.create(name="mapping-created-target-tag", slug="mapping-created-target-tag")
            target.snapshot()
            target.tags.add(endpoint)
            _link = KeaDhcpLink.objects.create(
                server=server,
                family=4,
                kea_identity="hw-address:02:00:00:00:00:08",
                object_type=ContentType.objects.get_for_model(target),
                object_id=target.pk,
            )
        request_finished.send(sender=type(self))
        self._assert_tag_replay_refused(branch)

    def test_named_endpoint_cleanup_refuses_even_complete_target_history(self):
        target, _link, endpoints, _field, branch = self._cleanup_target("tags", "native-named-history")
        self._delete(branch, endpoints[0])
        latest = (
            branch.get_changes()
            .filter(changed_object_type=ContentType.objects.get_for_model(target), changed_object_id=target.pk)
            .latest("time")
        )
        self.assertEqual(latest.postchange_data["tags"], [endpoints[1].name])
        self._assert_tag_replay_refused(branch)

    def test_named_endpoint_deletion_refuses_incomplete_target_relation_history(self):
        target, link, endpoints, _field, branch = self._cleanup_target("tags", "incomplete-named-history")
        self._delete(branch, endpoints[0])
        latest = (
            branch.get_changes()
            .filter(changed_object_type=ContentType.objects.get_for_model(target), changed_object_id=target.pk)
            .latest("time")
        )
        self.assertEqual(latest.postchange_data["tags"], [endpoints[1].name])
        # This counterfactual changes one persisted history field, preserving native schema and generation.
        latest.postchange_data["tags"] = [endpoint.name for endpoint in endpoints[:2]]
        latest.save(using=branch.connection_name, update_fields=["postchange_data"])
        before = ObjectChange.objects.count()
        observed = []

        def capture_mutation(sender, instance, using, **kwargs):
            if using == "default":
                observed.append((sender, instance.pk))

        for model in (type(target), type(endpoints[0]), KeaDhcpLink):
            pre_save.connect(capture_mutation, sender=model, weak=False)
            pre_delete.connect(capture_mutation, sender=model, weak=False)
        try:
            with self.assertRaisesMessage(AbortRequest, "Tag"):
                branch.merge(user=self.user)
        finally:
            for model in (type(target), type(endpoints[0]), KeaDhcpLink):
                pre_save.disconnect(capture_mutation, sender=model)
                pre_delete.disconnect(capture_mutation, sender=model)
        self.assertFalse(observed, "dependency refusal occurred after a native replay mutation")
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertFalse(branch.applied_changes.exists())
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertEqual(
            set(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)), {e.pk for e in endpoints[:2]}
        )
        self.assertTrue(type(endpoints[0]).objects.filter(pk=endpoints[0].pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_native_cleanup_does_not_allow_unrelated_relation_or_source_changes(self):
        from dcim.models import MACAddress

        for effect in (
            "remove",
            "add",
            "fk",
            "source",
            "scalar",
            "unselected-delete",
            "wrong-request-delete",
            "updated-receipt",
        ):
            with self.subTest(effect=effect):
                kind = "reservation" if effect == "fk" else "classes"
                target, link, endpoints, field, branch = self._cleanup_target(kind, f"cleanup-conflict-{effect}")
                replacement_server = _make_db_server(name=f"mapping-cleanup-replacement-{effect}")
                replacement_mac = MACAddress.objects.create(mac_address="02:00:00:00:00:08") if effect == "fk" else None
                self._delete(branch, endpoints[0])
                if effect == "wrong-request-delete":
                    self._delete(branch, endpoints[1])
                observed = []
                before = ObjectChange.objects.count()

                def change_main(
                    sender,
                    instance,
                    using,
                    *,
                    target=target,
                    effect=effect,
                    field=field,
                    endpoints=endpoints,
                    link=link,
                    replacement_server=replacement_server,
                    replacement_mac=replacement_mac,
                    observed=observed,
                    branch=branch,
                    **kwargs,
                ):
                    if using != "default" or instance.pk != endpoints[0].pk:
                        return
                    current = type(target).objects.get(pk=target.pk)
                    request = (
                        _change_request(self.user)
                        if effect in {"source", "wrong-request-delete"}
                        else current_request.get()
                    )
                    with event_tracking(request):
                        if effect in {"remove", "updated-receipt"}:
                            if effect == "updated-receipt":
                                from netbox_branching.utilities import record_applied_change

                                from netbox_kea.dhcp_mapping_lifecycle import _replay_operation

                                replay = _replay_operation.get()
                                key = (type(endpoints[0]), endpoints[0].pk)
                                fact = replay.endpoint_deletions[key]
                                receipt = branch.applied_changes.get(
                                    change__changed_object_type=ContentType.objects.get_for_model(endpoints[0]),
                                    change__changed_object_id=endpoints[0].pk,
                                    change__action="delete",
                                )
                                record_applied_change(receipt.change, branch)
                                self.assertIs(replay.endpoint_deletions[key], fact)
                            getattr(current, field).remove(endpoints[1])
                        elif effect in {"unselected-delete", "wrong-request-delete"}:
                            type(endpoints[1]).objects.get(pk=endpoints[1].pk).delete()
                        elif effect == "add":
                            getattr(current, field).add(endpoints[2])
                        elif effect == "fk":
                            current.hw_address = replacement_mac
                            current.save()
                        elif effect == "source":
                            mapping = KeaDhcpLink.objects.get(pk=link.pk)
                            mapping.server = replacement_server
                            mapping.save()
                        else:
                            current.description = "newer main scalar"
                            current.save()
                    observed.append(current.serialize_object())

                post_delete.connect(change_main, sender=type(endpoints[0]), weak=False)
                try:
                    with self.assertRaisesMessage(AbortRequest, "changed"):
                        branch.merge(user=self.user)
                finally:
                    post_delete.disconnect(change_main, sender=type(endpoints[0]))
                self.assertTrue(observed, "the ordinary callback did not reach its main write")
                branch.refresh_from_db()
                self.assertEqual(branch.status, "ready")
                self.assertFalse(branch.applied_changes.exists())
                self.assertEqual(ObjectChange.objects.count(), before)
                current = type(target).objects.get(pk=target.pk)
                self.assertEqual(
                    set(getattr(current, field).values_list("pk", flat=True)), {e.pk for e in endpoints[:2]}
                )
                self.assertEqual(current.description, target.description)
                if effect == "fk":
                    self.assertEqual(current.hw_address_id, target.hw_address_id)
                self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).server_id, link.server_id)
                self.assertTrue(type(endpoints[0]).objects.filter(pk=endpoints[0].pk).exists())
                self.assertIsNone(current_request.get())

    def test_named_relation_assignment_preserves_later_main_changes_and_refuses_revert(self):
        for effect in ("rename", "source", "scalar"):
            with self.subTest(effect=effect):
                target, link, endpoints, _field, branch = self._cleanup_target("tags", f"named-cleanup-later-{effect}")
                replacement_server = _make_db_server(name=f"mapping-later-source-{effect}")
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.snapshot()
                    local.description = "completed cleanup"
                    local.save()
                    local.tags.remove(type(endpoints[0]).objects.get(pk=endpoints[0].pk))
                request_finished.send(sender=type(self))
                observed = []

                def change_main(
                    sender,
                    instance,
                    using,
                    *,
                    effect=effect,
                    target=target,
                    endpoints=endpoints,
                    link=link,
                    replacement_server=replacement_server,
                    observed=observed,
                    **kwargs,
                ):
                    if using != "default" or instance.pk != target.pk or kwargs.get("action") != "post_remove":
                        return
                    current = type(target).objects.get(pk=target.pk)
                    self.assertEqual(current.description, "completed cleanup")
                    self.assertEqual(list(current.tags.values_list("pk", flat=True)), [endpoints[1].pk])
                    with event_tracking(_change_request(self.user)):
                        if effect == "rename":
                            endpoint = type(endpoints[1]).objects.get(pk=endpoints[1].pk)
                            endpoint.name = "mapping-cleanup-later-main-name"
                            endpoint.save()
                        elif effect == "source":
                            mapping = KeaDhcpLink.objects.get(pk=link.pk)
                            mapping.server = replacement_server
                            mapping.save()
                        else:
                            current.description = "newer main scalar"
                            current.save()
                    observed.append(
                        (
                            list(current.tags.values_list("name", flat=True)),
                            current.description,
                            KeaDhcpLink.objects.get(pk=link.pk).server_id,
                        )
                    )

                through = type(target)._meta.get_field("tags").through
                m2m_changed.connect(change_main, sender=through, weak=False)
                try:
                    branch.merge(user=self.user)
                finally:
                    m2m_changed.disconnect(change_main, sender=through)
                self.assertEqual(len(observed), 1)
                expected = (
                    ["mapping-cleanup-later-main-name" if effect == "rename" else endpoints[1].name],
                    "newer main scalar" if effect == "scalar" else "completed cleanup",
                    replacement_server.pk if effect == "source" else link.server_id,
                )
                self.assertEqual(observed[0], expected)
                before_revert = ObjectChange.objects.count()
                applied = branch.applied_changes.count()
                with self.assertRaisesMessage(AbortRequest, "changed"):
                    branch.revert(user=self.user)
                branch.refresh_from_db()
                self.assertEqual(branch.status, "merged")
                self.assertEqual(branch.applied_changes.count(), applied)
                self.assertEqual(ObjectChange.objects.count(), before_revert)
                current = type(target).objects.get(pk=target.pk)
                self.assertEqual(
                    (
                        list(current.tags.values_list("name", flat=True)),
                        current.description,
                        KeaDhcpLink.objects.get(pk=link.pk).server_id,
                    ),
                    expected,
                )
                self.assertTrue(type(endpoints[0]).objects.filter(pk=endpoints[0].pk).exists())
                self.assertIsNone(current_request.get())

    def test_native_cleanup_evidence_is_scoped_to_dry_run_error_and_retry(self):
        from core.signals import _signals_received
        from utilities.exceptions import AbortTransaction

        from netbox_kea.dhcp_mapping_lifecycle import _replay_operation

        target, link, endpoints, _field, branch = self._cleanup_target("classes", "cleanup-action-context")
        self._delete(branch, endpoints[0])
        self._delete(branch, target)
        before = ObjectChange.objects.count()
        with self.assertRaises(AbortTransaction):
            branch.merge(user=self.user, commit=False)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertFalse(branch.applied_changes.exists())
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertIsNone(_replay_operation.get())
        self.assertFalse(getattr(_signals_received, "pre_delete", set()))
        self.assertEqual(
            set(type(target).objects.get(pk=target.pk).client_classes.values_list("pk", flat=True)),
            {e.pk for e in endpoints[:2]},
        )
        rules = {"netbox_dhcp.subnet": [{"name": {"eq": "permitted-delete"}}]}
        with override_settings(PROTECTION_RULES=rules), self.assertRaises(AbortRequest):
            branch.merge(user=self.user)
        self.assertIsNone(_replay_operation.get())
        self.assertFalse(branch.applied_changes.exists())
        self.assertEqual(ObjectChange.objects.count(), before)
        enclosing = _change_request(self.user)
        with event_tracking(enclosing):
            branch.merge(user=self.user)
            self.assertIs(current_request.get(), enclosing)
        self.assertIsNone(_replay_operation.get())
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        branch.revert(user=self.user)
        self.assertIsNone(_replay_operation.get())
        self.assertEqual(
            set(type(target).objects.get(pk=target.pk).client_classes.values_list("pk", flat=True)),
            {e.pk for e in endpoints[:2]},
        )
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_missing_branch_target_table_refuses_mapping_access_and_deletion(self):
        for kind in ("subnet", "reservation"):
            with self.subTest(kind=kind):
                _, _, _, target, link = self._imported(kind, 4, f"missing-table-{kind}")
                branch = _provisioned_branch(self, f"mapping-missing-table-{kind}")
                with connection.cursor() as cursor:
                    cursor.execute('DROP TABLE "' + branch.schema_name + '"."' + target._meta.db_table + '" CASCADE')
                with activate_branch(branch):
                    with (
                        self.subTest(operation="read"),
                        self.assertRaisesMessage(AbortRequest, "Create a fresh branch"),
                    ):
                        KeaDhcpLink.objects.filter(pk=link.pk).exists()
                    with (
                        self.subTest(operation="delete"),
                        self.assertRaisesMessage(AbortRequest, "Create a fresh branch"),
                    ):
                        type(target).objects.filter(pk=target.pk).delete()
                self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_inactive_explicit_branch_alias_refuses_mapping_write_fallback(self):
        _, _, _, _, link = self._imported("subnet", 4, "inactive-alias")
        branch = _provisioned_branch(self, "mapping-inactive-alias")
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "' + branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')
        self.assertIsNone(branching.active_branch())
        with self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
            KeaDhcpLink.objects.using(branch.connection_name).filter(pk=link.pk).update(kea_subnet_id=99)
        link.refresh_from_db(using="default")
        self.assertEqual(link.kea_subnet_id, 7)

    def test_explicit_old_branch_alias_refuses_fallback_with_another_branch_active(self):
        _, _, _, _, link = self._imported("subnet", 4, "other-active-alias")
        old = _provisioned_branch(self, "mapping-explicit-old-alias")
        fresh = _provisioned_branch(self, "mapping-other-active-alias")
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "' + old.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')
        with activate_branch(fresh), self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
            KeaDhcpLink.objects.using(old.connection_name).filter(pk=link.pk).update(kea_subnet_id=99)
        link.refresh_from_db(using="default")
        self.assertEqual(link.kea_subnet_id, 7)

    def test_mapping_exemption_refuses_all_ordinary_write_boundaries(self):
        _, _, _, _, link = self._imported("subnet", 4, "exempt-writes")
        branch = _provisioned_branch(self, "mapping-exempt-writes")
        config = deepcopy(settings.PLUGINS_CONFIG)
        config["netbox_branching"]["exempt_models"] = [
            *config["netbox_branching"].get("exempt_models", []),
            "netbox_kea.keadhcplink",
        ]
        writes = (
            link.save,
            link.save_base,
            lambda: KeaDhcpLink.objects.filter(pk=link.pk).update(kea_subnet_id=99),
            lambda: KeaDhcpLink.objects.create(
                server_id=link.server_id,
                family=6,
                kea_subnet_id=99,
                object_type_id=link.object_type_id,
                object_id=link.object_id,
            ),
            lambda: KeaDhcpLink.objects.bulk_update([link], ["kea_subnet_id"]),
            lambda: KeaDhcpLink.objects.bulk_create(
                [
                    KeaDhcpLink(
                        server_id=link.server_id,
                        family=6,
                        kea_subnet_id=99,
                        object_type_id=link.object_type_id,
                        object_id=link.object_id,
                    )
                ]
            ),
        )
        with override_settings(PLUGINS_CONFIG=config), activate_branch(branch):
            for index, write in enumerate(writes):
                with self.subTest(writer=index), self.assertRaisesMessage(AbortRequest, "exemption"):
                    write()
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).kea_subnet_id, 7)

    def test_old_branch_mapping_update_refuses_main_fallback(self):
        _, _, _, _, link = self._imported("subnet", 6, "old-update")
        branch = _provisioned_branch(self, "mapping-old-update")
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "' + branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')
        with activate_branch(branch), self.assertRaisesMessage(AbortRequest, "Create a fresh branch"):
            KeaDhcpLink.objects.filter(pk=link.pk).update(kea_subnet_id=99)
        link.refresh_from_db(using="default")
        self.assertEqual(link.kea_subnet_id, 7)

    def test_reimport_observation_does_not_add_mapping_history(self):
        server, intent, observation, target, link = self._imported("reservation", 6, "no-op")
        changes = ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(KeaDhcpLink), changed_object_id=link.pk
        )
        before = changes.count()
        created = link.created
        synced = link.last_synced
        with event_tracking(_change_request(self.user)):
            summary = import_server_config(server, intent, observation)
        self.assertEqual(summary.errors, 0, summary.warnings)
        link.refresh_from_db()
        self.assertGreater(link.last_synced, synced)
        self.assertEqual(link.created, created)
        self.assertEqual(link.object_id, target.pk)
        self.assertEqual(changes.count(), before)

    def test_mapping_update_or_create_records_the_persisted_identity(self):
        server, _, _, _, link = self._imported("subnet", 4, "ordinary-update")
        with event_tracking(_change_request(self.user)):
            updated, created = KeaDhcpLink.objects.update_or_create(pk=link.pk, defaults={"kea_subnet_id": 11})
        self.assertFalse(created)
        self.assertEqual(updated.kea_subnet_id, 11)
        change = ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(KeaDhcpLink),
            changed_object_id=link.pk,
            action="update",
        ).latest("time")
        self.assertEqual(change.prechange_data["kea_subnet_id"], 7)
        self.assertEqual(change.prechange_data["server"], server.pk)
        self.assertEqual(change.postchange_data["kea_subnet_id"], 11)

    def test_incomplete_generation_history_refuses_the_whole_action(self):
        malformed = (
            ("created", None),
            ("created", "invalid"),
            ("created", "2026-10-04T01:00:00"),
            ("kea_identity", "missing"),
            ("object_type", "missing"),
        )
        for index, (field, value) in enumerate(malformed):
            with self.subTest(field=field, value=value):
                _, _, _, target, link = self._imported("subnet", 4, f"incomplete-{index}")
                branch = _provisioned_branch(self, f"mapping-incomplete-{index}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                self._delete(branch, target)
                change = branch.get_changes().get(
                    changed_object_type=ContentType.objects.get_for_model(KeaDhcpLink),
                    changed_object_id=link.pk,
                )
                if value == "missing":
                    del change.prechange_data[field]
                else:
                    change.prechange_data[field] = value
                change.save(update_fields=["prechange_data"])
                with self.assertRaisesMessage(AbortRequest, "incomplete"):
                    branch.merge(user=self.user)
                branch.refresh_from_db()
                self.assertEqual(branch.status, "ready")
                self.assertFalse(branch.applied_changes.exists())
                self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_native_refusal_rolls_back_and_retry_records_every_delete(self):
        _, _, _, target, link = self._imported("subnet", 4, "protection")
        unrelated = VRF.objects.create(name="mapping-unrelated")
        branch = _provisioned_branch(self, "mapping-protection")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            VRF.objects.get(pk=unrelated.pk).delete()
        request_finished.send(sender=type(self))
        self._delete(branch, target)
        before = ObjectChange.objects.count()
        rules = {"netbox_dhcp.subnet": [{"name": {"eq": "permitted-delete"}}]}
        with override_settings(PROTECTION_RULES=rules), self.assertRaisesMessage(AbortRequest, "protection rule"):
            branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertFalse(branch.applied_changes.exists())
        self.assertTrue(VRF.objects.filter(pk=unrelated.pk).exists())
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertIsNone(current_request.get())
        branch.merge(user=self.user)
        branch.refresh_from_db()
        recorded = set(branch.get_merged_changes().values_list("changed_object_type__model", "changed_object_id"))
        self.assertIn(("vrf", unrelated.pk), recorded)
        self.assertIn(("subnet", target.pk), recorded)
        self.assertIn(("keadhcplink", link.pk), recorded)
        self.assertIsNone(current_request.get())
        branch.revert(user=self.user)
        self.assertTrue(VRF.objects.filter(pk=unrelated.pk).exists())
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_import_after_preflight_prevents_the_whole_merge(self):
        server, intent, observation, target, link = self._imported("reservation", 4, "late-merge")
        KeaDhcpLink.objects.filter(pk=link.pk).delete()
        unrelated = VRF.objects.create(name="mapping-late-unrelated")
        branch = _provisioned_branch(self, "mapping-late-merge")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            VRF.objects.get(pk=unrelated.pk).delete()
        request_finished.send(sender=type(self))
        self._delete(branch, target)
        adopted = []
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            replay_pid = cursor.fetchone()[0]

        def importing():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    self.assertNotEqual(cursor.fetchone()[0], replay_pid)
                with event_tracking(_change_request(self.user)):
                    summary = import_server_config(server, intent, observation)
                self.assertEqual(summary.errors, 0, summary.warnings)
                mapping = KeaDhcpLink.objects.get(server=server, family=4)
                self.assertEqual(mapping.object_id, target.pk)
                adopted.append(mapping.pk)
            finally:
                connections.close_all()

        def import_before_replay(sender, branch, **kwargs):
            with ThreadPoolExecutor(1) as pool:
                pool.submit(importing).result(timeout=20)

        pre_merge.connect(import_before_replay, weak=False)
        try:
            with self.assertRaisesMessage(AbortRequest, "mapping"):
                branch.merge(user=self.user)
        finally:
            pre_merge.disconnect(import_before_replay)
        self.assertEqual(len(adopted), 1)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertFalse(branch.applied_changes.exists())
        self.assertTrue(VRF.objects.filter(pk=unrelated.pk).exists())
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=adopted[0], object_id=target.pk).exists())

    def test_main_target_change_refuses_the_whole_merge(self):
        _, _, _, target, link = self._imported("subnet", 4, "main-change")
        unrelated = VRF.objects.create(name="mapping-main-change-unrelated")
        branch = _provisioned_branch(self, "mapping-main-change")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            VRF.objects.get(pk=unrelated.pk).delete()
        request_finished.send(sender=type(self))
        self._delete(branch, target)
        with event_tracking(_change_request(self.user)):
            target.snapshot()
            target.description = "main retained change"
            target.save()
        with self.assertRaisesMessage(AbortRequest, "mapping"):
            branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertFalse(branch.applied_changes.exists())
        self.assertTrue(VRF.objects.filter(pk=unrelated.pk).exists())
        self.assertEqual(type(target).objects.get(pk=target.pk).description, "main retained change")
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_revert_refuses_a_reused_target_primary_key(self):
        _, _, _, target, link = self._imported("subnet", 6, "reused-target")
        branch = _provisioned_branch(self, "mapping-reused-target")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        branch.refresh_from_db()
        reused = type(target).objects.create(
            pk=target.pk,
            name=target.name,
            subnet_id=target.subnet_id,
            prefix=target.prefix,
            dhcp_server=target.dhcp_server,
        )
        self.assertNotEqual(reused.created, target.created)
        with self.assertRaisesMessage(AbortRequest, "mapping"):
            branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(type(target).objects.get(pk=target.pk).created, reused.created)
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_merge_preserves_same_millisecond_reused_target_and_mapping(self):
        _, _, _, target, link = self._imported("subnet", 4, "same-millisecond")
        original = timezone.now().replace(microsecond=123100)
        replacement = original.replace(microsecond=123900)
        self.assertEqual(DjangoJSONEncoder().encode(original), DjangoJSONEncoder().encode(replacement))
        type(target).objects.filter(pk=target.pk).update(created=original)
        KeaDhcpLink.objects.filter(pk=link.pk).update(created=original)
        branch = _provisioned_branch(self, "mapping-same-millisecond")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        type(target).objects.filter(pk=target.pk).delete()
        reused = type(target).objects.create(
            pk=target.pk,
            name=target.name,
            subnet_id=target.subnet_id,
            prefix=target.prefix,
            dhcp_server=target.dhcp_server,
        )
        KeaDhcpLink.objects.create(
            pk=link.pk,
            server_id=link.server_id,
            family=link.family,
            kea_subnet_id=link.kea_subnet_id,
            object_type_id=link.object_type_id,
            object_id=target.pk,
        )
        type(target).objects.filter(pk=reused.pk).update(created=replacement)
        KeaDhcpLink.objects.filter(pk=link.pk).update(created=replacement)
        with self.assertRaisesMessage(AbortRequest, "reused"):
            branch.merge(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertEqual(type(target).objects.get(pk=target.pk).created, replacement)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, replacement)

    def test_original_and_native_restored_generation_keep_full_precision(self):
        _, _, _, target, link = self._imported("subnet", 6, "precise-generation")
        original = timezone.now().replace(microsecond=123100)
        type(target).objects.filter(pk=target.pk).update(created=original)
        KeaDhcpLink.objects.filter(pk=link.pk).update(created=original)
        branch = _provisioned_branch(self, "mapping-precise-generation")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        branch.refresh_from_db()
        branch.revert(user=self.user)
        self.assertEqual(type(target).objects.get(pk=target.pk).created, original)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, original)

    def test_merge_preserves_a_reused_mapping_primary_key(self):
        _, _, _, target, link = self._imported("reservation", 6, "reused-mapping")
        original = timezone.now().replace(microsecond=123100)
        replacement = original.replace(microsecond=123900)
        KeaDhcpLink.objects.filter(pk=link.pk).update(created=original)
        branch = _provisioned_branch(self, "mapping-reused-mapping")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        KeaDhcpLink.objects.filter(pk=link.pk).delete()
        KeaDhcpLink.objects.create(
            pk=link.pk,
            server_id=link.server_id,
            family=link.family,
            kea_identity=link.kea_identity,
            object_type_id=link.object_type_id,
            object_id=target.pk,
        )
        KeaDhcpLink.objects.filter(pk=link.pk).update(created=replacement)
        with self.assertRaisesMessage(AbortRequest, "reused"):
            branch.merge(user=self.user)
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, replacement)

    def test_main_save_waits_until_native_revert_commits(self):
        _, _, _, target, link = self._imported("subnet", 4, "save-revert")
        target.description = "original"
        target.save()
        branch = _provisioned_branch(self, "mapping-save-revert")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.snapshot()
            changed.description = "branch"
            changed.save()
        branch.merge(user=self.user)
        branch.refresh_from_db()
        selected, release, writer_ready, writer_done = Event(), Event(), Event(), Event()
        writer_pid = []

        def pause_revert(sender, instance, **kwargs):
            if instance.pk == target.pk and instance.description == "original":
                selected.set()
                self.assertTrue(release.wait(20), "The test did not release native revert")

        def revert():
            try:
                Branch.objects.get(pk=branch.pk).revert(user=self.user)
            finally:
                connections.close_all()

        def save():
            try:
                obj = type(target).objects.get(pk=target.pk)
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    writer_pid.append(cursor.fetchone()[0])
                obj.description = "writer"
                writer_ready.set()
                obj.save()
            finally:
                writer_done.set()
                connections.close_all()

        pre_save.connect(pause_revert, sender=type(target), weak=False)
        try:
            with ThreadPoolExecutor(2) as pool:
                replay = pool.submit(revert)
                try:
                    self.assertTrue(selected.wait(20), "Native revert did not reach the real target save")
                    writer = pool.submit(save)
                    self.assertTrue(writer_ready.wait(20), "The real main writer did not start")
                    deadline = timezone.now() + timedelta(seconds=20)
                    blocked = False
                    while not writer_done.is_set() and timezone.now() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s "
                                "AND locktype = 'advisory' AND NOT granted)",
                                writer_pid,
                            )
                            blocked = cursor.fetchone()[0]
                        if blocked:
                            break
                        writer_done.wait(0.02)
                finally:
                    release.set()
                replay.result(timeout=20)
                writer.result(timeout=20)
        finally:
            release.set()
            pre_save.disconnect(pause_revert, sender=type(target))
        self.assertEqual(type(target).objects.get(pk=target.pk).description, "writer")
        self.assertTrue(blocked, "The writer did not coordinate before native selection and mutation")
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk, object_id=target.pk).exists())

    def _while_main_writer_holds_lock(self, target, attempt):
        selected, release, attempted = Event(), Event(), Event()
        writer_thread = []

        def pause_writer(sender, instance, **kwargs):
            if get_ident() in writer_thread and instance.pk == target.pk:
                selected.set()
                self.assertTrue(release.wait(20), "The test did not release the real main writer")

        def writer():
            writer_thread.append(get_ident())
            try:
                changed = type(target).objects.get(pk=target.pk)
                changed.description = "latest main writer"
                changed.save()
            finally:
                connections.close_all()

        def contender():
            try:
                attempt()
            finally:
                attempted.set()
                connections.close_all()

        pre_save.connect(pause_writer, sender=type(target), weak=False)
        try:
            with ThreadPoolExecutor(2) as pool:
                writing = pool.submit(writer)
                try:
                    self.assertTrue(selected.wait(20), "The real main writer did not hold the metadata lock")
                    competing = pool.submit(contender)
                    self.assertTrue(attempted.wait(10), "An existing transaction waited for the metadata lock")
                    competing.result(timeout=20)
                finally:
                    release.set()
                writing.result(timeout=20)
        finally:
            release.set()
            pre_save.disconnect(pause_writer, sender=type(target))

    def test_inactive_branch_transactions_refuse_main_lock_contention(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                _, _, _, target, link = self._imported("subnet", 4, f"inactive-transaction-{manual}")
                branch = _provisioned_branch(self, f"mapping-inactive-transaction-{manual}")

                def write_main(branch=branch, target=target, manual=manual):
                    alias = branch.connection_name
                    type(target).objects.using(alias).get(pk=target.pk)
                    self.assertIn(alias, branching.connection_aliases())
                    self.assertIsNone(branching.active_branch())
                    changed = type(target).objects.get(pk=target.pk)
                    changed.description = "competing writer"
                    if manual:
                        branch_connection = connections[alias]
                        branch_connection.set_autocommit(False)
                        try:
                            with branch_connection.cursor() as cursor:
                                cursor.execute("SELECT 1")
                            with self.assertRaisesMessage(AbortRequest, "Retry"):
                                changed.save()
                        finally:
                            branch_connection.rollback()
                            branch_connection.set_autocommit(True)
                    else:
                        with transaction.atomic(using=alias), self.assertRaisesMessage(AbortRequest, "Retry"):
                            changed.save()

                self._while_main_writer_holds_lock(target, write_main)
                self.assertEqual(type(target).objects.get(pk=target.pk).description, "latest main writer")
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_native_merge_and_revert_refuse_a_main_writer_then_preserve_its_commit(self):
        for action in ("merge", "revert"):
            with self.subTest(action=action):
                _, _, _, target, link = self._imported("subnet", 6, f"writer-first-{action}")
                branch = _provisioned_branch(self, f"mapping-writer-first-{action}")
                branch.merge_strategy = "squash"
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    changed = type(target).objects.get(pk=target.pk)
                    changed.description = "branch writer"
                    changed.save()
                if action == "revert":
                    branch.merge(user=self.user)
                    branch.refresh_from_db()
                expected_status = branch.status
                applied = branch.applied_changes.count()

                def replay(branch=branch, action=action, expected_status=expected_status, applied=applied):
                    native = Branch.objects.get(pk=branch.pk)
                    with self.assertRaisesMessage(AbortRequest, "Retry"):
                        getattr(native, action)(user=self.user)
                    native.refresh_from_db()
                    self.assertEqual(native.status, expected_status)
                    self.assertEqual(native.applied_changes.count(), applied)
                    self.assertIsNone(current_request.get())

                self._while_main_writer_holds_lock(target, replay)
                with self.assertRaisesMessage(AbortRequest, "has changed"):
                    getattr(branch, action)(user=self.user)
                self.assertEqual(type(target).objects.get(pk=target.pk).description, "latest main writer")
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def _writer_during_revert(self, target, branch, write, selection_signal=None, selection_sender=None):
        selected, release_replay, release_writer, writer_ready, writer_selected, writer_done = (
            Event() for _ in range(6)
        )
        replay_thread, writer_thread, writer_pid = [], [], []

        def pause_replay(sender, instance, **kwargs):
            if get_ident() in replay_thread and instance.pk == target.pk:
                selected.set()
                self.assertTrue(release_replay.wait(20), "The test did not release native revert")

        def pause_writer(sender, **kwargs):
            if get_ident() in writer_thread and kwargs.get("action", "pre_delete") == "pre_remove":
                writer_selected.set()
                self.assertTrue(release_writer.wait(20), "The test did not release the native writer")
            elif get_ident() in writer_thread and selection_signal is pre_delete:
                writer_selected.set()
                self.assertTrue(release_writer.wait(20), "The test did not release the native collector")

        def replay():
            replay_thread.append(get_ident())
            try:
                Branch.objects.get(pk=branch.pk).revert(user=self.user)
            finally:
                connections.close_all()

        def writer():
            writer_thread.append(get_ident())
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    writer_pid.append(cursor.fetchone()[0])
                writer_ready.set()
                write()
            finally:
                writer_done.set()
                connections.close_all()

        pre_save.connect(pause_replay, sender=type(target), weak=False)
        if selection_signal is not None:
            selection_signal.connect(pause_writer, sender=selection_sender, weak=False)
        try:
            with ThreadPoolExecutor(2) as pool:
                replay_future = pool.submit(replay)
                try:
                    self.assertTrue(selected.wait(20), "Native revert did not reach target mutation")
                    writer_future = pool.submit(writer)
                    self.assertTrue(writer_ready.wait(20), "The main writer did not start")
                    deadline = timezone.now() + timedelta(seconds=20)
                    blocked = False
                    while not writer_done.is_set() and not writer_selected.is_set() and timezone.now() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s "
                                "AND locktype = 'advisory' AND NOT granted)",
                                writer_pid,
                            )
                            blocked = cursor.fetchone()[0]
                        if blocked:
                            break
                        writer_done.wait(0.02)
                finally:
                    release_replay.set()
                try:
                    replay_future.result(timeout=20)
                finally:
                    release_writer.set()
                writer_future.result(timeout=20)
        finally:
            release_replay.set()
            release_writer.set()
            pre_save.disconnect(pause_replay, sender=type(target))
            if selection_signal is not None:
                selection_signal.disconnect(pause_writer, sender=selection_sender)
        return blocked

    def test_m2m_set_selects_relations_after_native_revert(self):
        from netbox_dhcp.models import ClientClass

        _, _, _, target, link = self._imported("subnet", 4, "m2m-revert")
        a, b, c = (
            ClientClass.objects.create(name=f"mapping-class-{name}", dhcp_server=target.dhcp_server)
            for name in ("a", "b", "c")
        )
        target.client_classes.set([a])
        branch = _provisioned_branch(self, "mapping-m2m-revert")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.snapshot()
            changed.client_classes.set([b])
        branch.merge(user=self.user)
        blocked = self._writer_during_revert(
            target,
            branch,
            lambda: type(target).objects.get(pk=target.pk).client_classes.set([c]),
            m2m_changed,
            type(target).client_classes.through,
        )
        self.assertEqual(
            set(type(target).objects.get(pk=target.pk).client_classes.values_list("pk", flat=True)), {c.pk}
        )
        self.assertTrue(blocked, "M2M selection occurred before coordination")
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_custom_tags_select_relations_after_native_revert(self):
        from extras.models import Tag

        _, _, _, target, link = self._imported("reservation", 6, "tags-revert")
        a, b, c = (
            Tag.objects.create(name=f"mapping-tag-{name}", slug=f"mapping-tag-{name}") for name in ("a", "b", "c")
        )
        target.tags.set([a])
        branch = _provisioned_branch(self, "mapping-tags-revert")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.snapshot()
            changed.tags.set([b])
        branch.merge(user=self.user)
        blocked = self._writer_during_revert(
            target,
            branch,
            lambda: type(target).objects.get(pk=target.pk).tags.set([c]),
            m2m_changed,
            type(target)._meta.get_field("tags").remote_field.through,
        )
        self.assertEqual(set(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)), {c.pk})
        self.assertTrue(blocked, "Custom tag selection occurred before coordination")
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_reverse_and_dynamic_m2m_managers_preserve_native_behavior(self):
        from netbox_dhcp.models import ClientClass

        _, _, _, target, link = self._imported("subnet", 4, "reverse-m2m")
        a, b = (
            ClientClass.objects.create(name=f"mapping-reverse-{name}", dhcp_server=target.dhcp_server)
            for name in ("a", "b")
        )
        target.client_classes(manager="objects").set([a])
        branch = _provisioned_branch(self, "mapping-reverse-m2m")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.snapshot()
            changed.client_classes.set([b])
        branch.merge(user=self.user)
        blocked = self._writer_during_revert(
            target,
            branch,
            lambda: a.subnet_set(manager="objects").clear(),
            m2m_changed,
            type(target).client_classes.through,
        )
        self.assertFalse(type(target).objects.get(pk=target.pk).client_classes.exists())
        self.assertFalse(b.subnet_set.filter(pk=target.pk).exists())
        self.assertTrue(blocked, "Reverse relation clear occurred before coordination")
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_generic_relation_removal_coordinates_before_deleting_target_dependency(self):
        from ipam.models import IPAddress

        _, _, _, target, link = self._imported("reservation", 4, "generic-remove")
        owner = _device_with_interface("mapping-generic-owner")
        address = IPAddress.objects.create(address="198.18.0.17/24", assigned_object=owner)
        target.ipv4_address = address
        target.save()
        branch = _provisioned_branch(self, "mapping-generic-remove")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.ipv4_address = None
            changed.save()
        branch.merge(user=self.user)
        blocked = self._writer_during_revert(target, branch, lambda: owner.ip_addresses.remove(address))
        self.assertTrue(blocked, "Generic relation removal selected before coordination")
        self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())
        self.assertIsNone(type(target).objects.get(pk=target.pk).ipv4_address_id)
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_reverse_fk_set_evaluates_input_after_native_revert(self):
        from netbox_dhcp.models import DHCPServer

        _, _, _, target, link = self._imported("reservation", 4, "reverse-fk-revert")
        original = target.dhcp_server
        selected_server = DHCPServer.objects.create(name="mapping-selected-server")
        destination = DHCPServer.objects.create(name="mapping-destination-server")
        branch = _provisioned_branch(self, "mapping-reverse-fk-revert")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.dhcp_server = selected_server
            changed.save()
        branch.merge(user=self.user)
        blocked = self._writer_during_revert(
            target,
            branch,
            lambda: destination.child_host_reservations.set(selected_server.child_host_reservations.all()),
        )
        self.assertEqual(type(target).objects.get(pk=target.pk).dhcp_server_id, original.pk)
        self.assertTrue(blocked, "Reverse FK input evaluation occurred before coordination")
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_parent_delete_collects_after_native_revert(self):
        from netbox_dhcp.models import DHCPServer

        _, _, _, target, link = self._imported("reservation", 6, "parent-revert")
        original = target.dhcp_server
        parent = DHCPServer.objects.create(name="mapping-cascade-parent")
        branch = _provisioned_branch(self, "mapping-parent-revert")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(target).objects.get(pk=target.pk)
            changed.dhcp_server = parent
            changed.save()
        branch.merge(user=self.user)
        blocked = self._writer_during_revert(target, branch, parent.delete, pre_delete, type(target))
        self.assertEqual(type(target).objects.get(pk=target.pk).dhcp_server_id, original.pk)
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(blocked, "The parent collector selected children before coordination")

    def test_main_import_selects_restored_mapping_after_native_revert(self):
        server, intent, observation, target, link = self._imported("subnet", 4, "import-revert")
        branch = _provisioned_branch(self, "mapping-import-revert")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        summaries = []

        def importing():
            with event_tracking(_change_request(self.user)):
                summaries.append(import_server_config(server, intent, observation))

        blocked = self._writer_during_revert(target, branch, importing)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].errors, 0, summaries[0].warnings)
        self.assertEqual(summaries[0].subnets_created, 0)
        self.assertTrue(blocked, "The importer selected mapping state before coordination")
        self.assertEqual(KeaDhcpLink.objects.get(server=server, family=4).pk, link.pk)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).object_id, target.pk)

    def test_missing_source_server_refuses_branch_deletion_capture(self):
        server, _, _, target, link = self._imported("reservation", 4, "missing-source-capture")
        branch = _provisioned_branch(self, "mapping-missing-source-capture")
        server.delete()
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            before = ObjectChange.objects.count()
            with self.assertRaisesMessage(AbortRequest, "Server"):
                type(target).objects.get(pk=target.pk).delete()
            self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
            self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
            self.assertEqual(ObjectChange.objects.count(), before)
        self.assertIsNone(current_request.get())

    def test_missing_source_server_refuses_before_target_restore(self):
        server, _, _, target, link = self._imported("subnet", 6, "missing-source-restore")
        branch = _provisioned_branch(self, "mapping-missing-source-restore")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        branch.refresh_from_db()
        server.delete()
        before = ObjectChange.objects.count()
        with self.assertRaisesMessage(AbortRequest, "Server"):
            branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertEqual(ObjectChange.objects.count(), before)

    def test_missing_target_dependency_refuses_the_whole_revert(self):
        _, _, _, target, link = self._imported("subnet", 6, "missing-prefix")
        branch = _provisioned_branch(self, "mapping-missing-prefix")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        target.prefix.delete()
        before = ObjectChange.objects.count()
        with self.assertRaisesMessage(AbortRequest, "dependency"):
            branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_missing_dependency_update_is_not_a_planned_restoration(self):
        _, _, _, target, link = self._imported("subnet", 4, "missing-updated-prefix")
        prefix = target.prefix
        branch = _provisioned_branch(self, "mapping-missing-updated-prefix")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            changed = type(prefix).objects.get(pk=prefix.pk)
            changed.snapshot()
            changed.description = "branch prefix update"
            changed.save()
        self._delete(branch, target)
        branch.merge(user=self.user)
        branch.refresh_from_db()
        prefix.delete()
        before = ObjectChange.objects.count()
        applied = branch.applied_changes.count()
        with self.assertRaisesMessage(AbortRequest, "dependency is missing"):
            branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertFalse(type(prefix).objects.filter(pk=prefix.pk).exists())

    def test_reimported_source_identity_refuses_the_whole_revert(self):
        server, intent, observation, target, link = self._imported("subnet", 4, "source-reimport")
        branch = _provisioned_branch(self, "mapping-source-reimport")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        with event_tracking(_change_request(self.user)):
            summary = import_server_config(server, intent, observation)
        self.assertEqual(summary.errors, 0, summary.warnings)
        replacement = KeaDhcpLink.objects.get(server=server, family=4)
        self.assertNotEqual(replacement.pk, link.pk)
        before = ObjectChange.objects.count()
        with self.assertRaisesMessage(AbortRequest, "identity"):
            branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertTrue(KeaDhcpLink.objects.filter(pk=replacement.pk, object_id=replacement.object_id).exists())

    def test_parent_replay_refuses_a_new_main_mapping_before_any_mutation(self):
        server, _, _, target, link = self._imported("reservation", 4, "late-parent")
        KeaDhcpLink.objects.filter(pk=link.pk).delete()
        unrelated = VRF.objects.create(name="mapping-parent-unrelated")
        parent = target.dhcp_server
        branch = _provisioned_branch(self, "mapping-late-parent")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            VRF.objects.get(pk=unrelated.pk).delete()
        request_finished.send(sender=type(self))
        self._delete(branch, parent)
        type(target).objects.filter(pk=target.pk).delete()
        config = {"subnet4": []}
        observation = _reservation_snapshot(
            config, 4, [{"subnet-id": 0, "hw-address": "02:00:00:00:00:09", "hostname": "late-mapping"}]
        )
        intent = parse_dhcp_config(config, 4)
        adopted = []

        def import_after_preflight(sender, **kwargs):
            with event_tracking(_change_request(self.user)):
                summary = import_server_config(server, intent, observation)
            self.assertEqual(summary.errors, 0, summary.warnings)
            adopted.append(KeaDhcpLink.objects.get(server=server, family=4))

        pre_merge.connect(import_after_preflight, weak=False)
        try:
            with self.assertRaisesMessage(AbortRequest, "mapping"):
                branch.merge(user=self.user)
        finally:
            pre_merge.disconnect(import_after_preflight)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertFalse(branch.applied_changes.exists())
        self.assertTrue(VRF.objects.filter(pk=unrelated.pk).exists())
        self.assertTrue(type(parent).objects.filter(pk=parent.pk).exists())
        self.assertTrue(type(target).objects.filter(pk=adopted[0].object_id).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=adopted[0].pk).exists())

    def test_revert_preserves_a_branch_created_global_reservation_adopted_on_main(self):
        server, intent, observation, target, _ = self._imported("reservation", 6, "branch-created")
        type(target).objects.filter(pk=target.pk).delete()
        branch = _provisioned_branch(self, "mapping-branch-created")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            created = type(target).objects.create(
                name=target.name,
                dhcp_server=target.dhcp_server,
                hw_address=target.hw_address,
                hostname=target.hostname,
            )
        branch.merge(user=self.user)
        with event_tracking(_change_request(self.user)):
            summary = import_server_config(server, intent, observation)
        self.assertEqual(summary.errors, 0, summary.warnings)
        adopted = KeaDhcpLink.objects.get(server=server, family=6)
        self.assertEqual(adopted.object_id, created.pk)
        before = ObjectChange.objects.count()
        with self.assertRaisesMessage(AbortRequest, "mapping"):
            branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertTrue(type(target).objects.filter(pk=created.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=adopted.pk, object_id=created.pk).exists())

    def test_reentrant_import_during_replay_refuses_unrecorded_mapping_deletion(self):
        server, intent, observation, target, link = self._imported("reservation", 4, "reentrant-import")
        KeaDhcpLink.objects.filter(pk=link.pk).delete()
        branch = _provisioned_branch(self, "mapping-reentrant-import")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        before = ObjectChange.objects.count()
        adopted = []

        def importing(sender, operation, **kwargs):
            if operation == "merge":
                with event_tracking(_change_request(self.user)):
                    summary = import_server_config(server, intent, observation)
                self.assertEqual(summary.errors, 0, summary.warnings)
                adopted.append(KeaDhcpLink.objects.get(server=server, family=4).pk)

        squash_dependency_graph_built.connect(importing, weak=False)
        try:
            with self.assertRaisesMessage(AbortRequest, "mapping"):
                branch.merge(user=self.user)
        finally:
            squash_dependency_graph_built.disconnect(importing)
        self.assertEqual(len(adopted), 1)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertFalse(branch.applied_changes.exists())
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=adopted[0]).exists())
        self.assertIsNone(current_request.get())

    def test_reentrant_import_during_replay_preserves_newer_mapped_target_state(self):
        server, _, _, target, link = self._imported("subnet", 4, "reentrant-update")
        branch = _provisioned_branch(self, "mapping-reentrant-update")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        changed_intent = parse_dhcp_config(
            {"subnet4": [{"id": 7, "subnet": "198.18.0.0/24", "valid-lifetime": 600}]}, 4
        )
        before = ObjectChange.objects.count()
        observed = []

        def importing(sender, operation, **kwargs):
            if operation == "merge":
                with event_tracking(_change_request(self.user)):
                    summary = import_server_config(server, changed_intent)
                self.assertEqual(summary.errors, 0, summary.warnings)
                current = type(target).objects.get(pk=target.pk)
                mapping = KeaDhcpLink.objects.get(server=server, family=4)
                self.assertEqual(current.valid_lifetime, 600)
                self.assertEqual(mapping.pk, link.pk)
                observed.append(current.pk)

        squash_dependency_graph_built.connect(importing, weak=False)
        try:
            with self.assertRaisesMessage(AbortRequest, "has changed"):
                branch.merge(user=self.user)
        finally:
            squash_dependency_graph_built.disconnect(importing)
        self.assertEqual(observed, [target.pk])
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertFalse(branch.applied_changes.exists())
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertEqual(type(target).objects.get(pk=target.pk).valid_lifetime, target.valid_lifetime)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).last_synced, link.last_synced)
        self.assertIsNone(current_request.get())

    def test_conflicting_mapping_target_identity_refuses_the_whole_revert(self):
        _, _, _, target, link = self._imported("reservation", 4, "target-identity")
        branch = _provisioned_branch(self, "mapping-target-identity")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        branch.refresh_from_db()
        conflicting = KeaDhcpLink.objects.create(
            server_id=link.server_id,
            family=6,
            kea_identity="new-global-identity",
            object_type_id=link.object_type_id,
            object_id=target.pk,
        )
        before = ObjectChange.objects.count()
        applied = branch.applied_changes.count()
        with self.assertRaisesMessage(AbortRequest, "identity conflicts"):
            branch.revert(user=self.user)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=conflicting.pk).exists())

    def test_http_delete_and_native_jobs_clear_history_on_success_error_and_dry_run(self):
        from core.signals import _signals_received
        from netbox_branching.jobs import MergeBranchJob, RevertBranchJob

        _, _, _, target, link = self._imported("subnet", 4, "http-job")
        branch = _provisioned_branch(self, "mapping-http-job")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self.client.force_login(self.user)
        self.client.cookies[COOKIE_NAME] = branch.schema_id
        response = self.client.post(reverse("plugins:netbox_dhcp:subnet_delete", args=[target.pk]), {"confirm": True})
        self.assertEqual(response.status_code, 302)
        with activate_branch(branch):
            self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
            self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertIsNone(current_request.get())
        self.assertFalse(getattr(_signals_received, "pre_delete", set()))
        receipts = []

        def record_native_receipt(sender, instance, using, **kwargs):
            if using == "default":
                receipts.append(
                    (
                        sender._meta.model_name,
                        connection.in_atomic_block,
                        branching.has_native_delete_receipt(branch, instance),
                    )
                )

        def run_job(runner, *, commit=True):
            job = Job.objects.create(name=runner.name, object=branch, user=self.user, job_id=uuid.uuid4(), data={})
            runner.handle(job, commit=commit)
            job.refresh_from_db()
            branch.refresh_from_db()
            self.assertIsNone(current_request.get())
            self.assertFalse(getattr(_signals_received, "pre_delete", set()))
            return job

        for model in (type(target), KeaDhcpLink):
            post_delete.connect(record_native_receipt, sender=model, weak=False)
        try:
            before = ObjectChange.objects.count()
            self.assertEqual(run_job(MergeBranchJob, commit=False).status, "completed")
            self.assertEqual(branch.status, "ready")
            self.assertFalse(branch.applied_changes.exists())
            self.assertEqual(ObjectChange.objects.count(), before)
            rules = {"netbox_dhcp.subnet": [{"name": {"eq": "permitted-delete"}}]}
            with override_settings(PROTECTION_RULES=rules):
                self.assertEqual(run_job(MergeBranchJob).status, "errored")
            self.assertEqual(branch.status, "ready")
            self.assertFalse(branch.applied_changes.exists())
            self.assertEqual(ObjectChange.objects.count(), before)
            self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
            self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
            self.assertEqual(run_job(MergeBranchJob).status, "completed")
            self.assertEqual(branch.status, "merged")
            self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
            self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
            applied = branch.applied_changes.count()
            before_revert = ObjectChange.objects.count()
            self.assertEqual(run_job(RevertBranchJob, commit=False).status, "completed")
            self.assertEqual(branch.status, "merged")
            self.assertEqual(branch.applied_changes.count(), applied)
            self.assertEqual(ObjectChange.objects.count(), before_revert)
            self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
            self.assertEqual(run_job(RevertBranchJob).status, "completed")
            self.assertEqual(branch.status, "ready")
            self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
            self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
            self.assertEqual({name for name, _, _ in receipts}, {"subnet", "keadhcplink"})
            self.assertTrue(all(atomic and recorded for _, atomic, recorded in receipts))
        finally:
            for model in (type(target), KeaDhcpLink):
                post_delete.disconnect(record_native_receipt, sender=model)

    def test_native_action_restores_enclosing_request_history(self):
        from core.signals import _signals_received

        _, _, _, target, link = self._imported("reservation", 6, "enclosing-request")
        branch = _provisioned_branch(self, "mapping-enclosing-request")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        request = _change_request(self.user)
        prior = {(ContentType.objects.get_for_model(VRF), 42)}
        _signals_received.pre_delete = prior
        try:
            with event_tracking(request):
                branch.merge(user=self.user)
                self.assertIs(current_request.get(), request)
                self.assertIs(_signals_received.pre_delete, prior)
            self.assertIsNone(current_request.get())
            branch.refresh_from_db()
            branch.revert(user=self.user)
            self.assertFalse(_signals_received.pre_delete)
            self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        finally:
            request_finished.send(sender=type(self))


_RETRY = "Nothing changed. Retry this operation."
_CONTENTION = f"DHCP mapping metadata is changing in another transaction. {_RETRY}"
_STALE_GATE = f"This deletion now reaches an imported DHCP target or its mapping. {_RETRY}"
_LOCK_CONFLICT = f"A database lock conflict stopped this change. {_RETRY}"
_EVENTS_RECORDER = "netbox_kea.tests.utils.record_dispatched_events"


class _PausedImport:
    """A real import that holds the mapping key while it waits in its first Subnet save."""

    def __init__(self, test, suffix):
        from netbox_dhcp.models import Subnet

        self.test, self.subnet = test, Subnet
        self.server = _make_db_server(name=f"paused-{suffix}", ca_url="https://kea.example.invalid", dhcp6=False)
        self.intent = parse_dhcp_config({"subnet4": [{"id": 9, "subnet": "198.19.0.0/24"}]}, 4)
        self.paused, self.release = Event(), Event()
        self.thread, self.summaries = [], []
        self.pool = ThreadPoolExecutor(1)
        self.future = None
        pre_save.connect(self._pause, sender=Subnet, weak=False)
        test.addCleanup(self._cleanup)

    def _pause(self, sender, instance, **kwargs):
        if get_ident() in self.thread and not self.paused.is_set():
            self.paused.set()
            self.release.wait(30)

    def _import(self):
        self.thread.append(get_ident())
        try:
            with event_tracking(_change_request(self.test.user)):
                self.summaries.append(import_server_config(self.server, self.intent, None))
        finally:
            connections.close_all()

    def start(self):
        self.future = self.pool.submit(self._import)
        self.test.assertTrue(self.paused.wait(20), "The import did not reach its first Subnet save")

    def finish(self):
        self.release.set()
        self.future.result(timeout=30)
        self.test.assertEqual(self.summaries[0].errors, 0, self.summaries[0].warnings)
        self.test.assertTrue(KeaDhcpLink.objects.filter(server=self.server, family=4).exists())

    def _cleanup(self):
        self.release.set()
        self.pool.shutdown(wait=True)
        pre_save.disconnect(self._pause, sender=self.subnet)


class DhcpMappingLockScopeTest(TransactionTestCase):
    """Only operations whose effect reaches a DHCP target or mapping take the mapping lock."""

    setUp = DhcpMappingRecoveryTest.setUp
    _imported = DhcpMappingRecoveryTest._imported
    _delete = DhcpMappingRecoveryTest._delete

    def _workers(self):
        pool = ThreadPoolExecutor(2)
        self.addCleanup(pool.shutdown, wait=True)
        return pool

    @contextmanager
    def _during_import(self, suffix):
        paused = _PausedImport(self, suffix)
        paused.start()
        yield paused
        paused.finish()

    def _stale_ip_cleanup(self):
        kea = {**settings.PLUGINS_CONFIG.get("netbox_kea", {}), "stale_ip_cleanup": "remove"}
        return override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "netbox_kea": kea})

    def _backend_pid(self, pids):
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pids.append(cursor.fetchone()[0])

    def _blocked_by(self, waiter, holder, done):
        """Return whether PostgreSQL reports the *waiter* backend blocked by the *holder* backend."""
        deadline = timezone.now() + timedelta(seconds=20)
        while not done.done() and timezone.now() < deadline:
            if waiter and holder:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [holder[0], waiter[0]])
                    if cursor.fetchone()[0]:
                        return True
            Event().wait(0.02)
        return False

    def _delete_in_transaction(self, obj):
        """Delete *obj* in its own transaction, as a NetBox view does, and return the refusal message or None."""
        try:
            with transaction.atomic():
                type(obj).objects.get(pk=obj.pk).delete()
        except AbortRequest as error:
            return error.message
        finally:
            connections.close_all()
        return None

    def test_unrelated_http_deletes_and_tag_edit_complete_during_import(self):
        from dcim.models import Device, Site
        from extras.models import Tag
        from ipam.models import IPAddress, Prefix

        interface = _device_with_interface("lock-scope-unrelated")
        device = Device.objects.get(pk=interface.device_id)
        site = Site.objects.get(pk=device.site_id)
        assigned = IPAddress.objects.create(address="203.0.113.10/24", assigned_object=interface)
        loose = IPAddress.objects.create(address="203.0.113.20/24")
        prefix = Prefix.objects.create(prefix="203.0.113.0/24")
        tag = Tag.objects.create(name="lock-scope-unrelated", slug="lock-scope-unrelated")
        doomed = Tag.objects.create(name="lock-scope-doomed", slug="lock-scope-doomed")
        self.client.force_login(self.user)
        with self._during_import("unrelated-http"):
            for route, obj in (
                ("ipam:ipaddress_delete", loose),
                ("ipam:prefix_delete", prefix),
                ("dcim:device_delete", device),
                ("dcim:site_delete", site),
                ("extras:tag_delete", doomed),
            ):
                with self.subTest(route=route):
                    response = self.client.post(reverse(route, args=[obj.pk]), {"confirm": True})
                    self.assertEqual(response.status_code, 302)
                    self.assertFalse(
                        type(obj).objects.filter(pk=obj.pk).exists(),
                        [str(message) for message in get_messages(response.wsgi_request)],
                    )
            response = self.client.post(
                reverse("extras:tag_edit", args=[tag.pk]),
                {"name": "lock-scope-renamed", "slug": tag.slug, "color": "112233", "weight": 0, "description": ""},
            )
            self.assertEqual(response.status_code, 302, response.content[:500])
            self.assertEqual(Tag.objects.get(pk=tag.pk).name, "lock-scope-renamed")
        self.assertFalse(IPAddress.objects.filter(pk=assigned.pk).exists())

    def test_stale_ip_delete_completes_during_import(self):
        from ipam.models import IPAddress

        server = _make_db_server(name="lock-scope-stale", ca_url="https://stale.example.invalid", dhcp6=False)
        with self._stale_ip_cleanup():
            _reconcile(server, [_lease()])
            stale = _row()
            with self._during_import("stale-ip"):
                report = _reconcile(server, [])
        self.assertEqual((report.removed, report.errors), (1, 0))
        self.assertFalse(IPAddress.objects.filter(pk=stale.pk).exists())

    def test_relevant_stale_ip_delete_is_a_row_failure_during_import(self):
        from ipam.models import IPAddress
        from netbox_dhcp.models import DHCPServer, HostReservation

        server = _make_db_server(name="lock-scope-relevant-stale", ca_url="https://stale.example.invalid", dhcp6=False)
        dhcp_server = DHCPServer.objects.create(name="lock-scope-relevant-stale")
        paused = _PausedImport(self, "relevant-stale")
        workers = self._workers()
        main, referenced = get_ident(), []

        def reserve(address):
            try:
                HostReservation.objects.create(name="lock-scope-late", dhcp_server=dhcp_server, ipv4_address=address)
            finally:
                connections.close_all()

        def reference_the_other_address(sender, instance, **kwargs):
            # The first stale delete commits a reference to the second address, then an import takes the key.
            if get_ident() != main or referenced:
                return
            other = next(address for address in addresses if address.pk != instance.pk)
            referenced.append(other)
            workers.submit(reserve, other).result(timeout=20)
            paused.start()

        with self._stale_ip_cleanup():
            _reconcile(server, [_lease("10.0.0.5"), _lease("10.0.0.6")])
            addresses = [_row("10.0.0.5"), _row("10.0.0.6")]
            pre_delete.connect(reference_the_other_address, sender=IPAddress, weak=False)
            try:
                report = _reconcile(server, [])
            finally:
                pre_delete.disconnect(reference_the_other_address, sender=IPAddress)
        paused.finish()
        self.assertEqual((report.removed, report.errors), (1, 1))
        self.assertIn("lease", report.incomplete)
        self.assertTrue(IPAddress.objects.filter(pk=referenced[0].pk).exists())
        self.assertTrue(HostReservation.objects.filter(ipv4_address=referenced[0]).exists())

    def test_ip_delete_whose_only_dhcp_effect_is_an_empty_set_null_update_completes_during_import(self):
        from ipam.models import IPAddress

        address = IPAddress.objects.create(address="203.0.113.30/24")
        with self._during_import("empty-set-null"), CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                IPAddress.objects.get(pk=address.pk).delete()
        self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())
        update = 'UPDATE "netbox_dhcp_hostreservation" SET "ipv4_address_id" = NULL'
        self.assertTrue(
            any(query["sql"].startswith(update) for query in captured.captured_queries),
            "The native collector did not schedule the empty SET_NULL update",
        )

    def test_relevant_device_delete_refuses_during_import_and_completes_after_it(self):
        from dcim.models import Device
        from ipam.models import IPAddress
        from netbox_dhcp.models import DHCPServer, HostReservation

        interface = _device_with_interface("lock-scope-relevant")
        address = IPAddress.objects.create(address="203.0.113.40/24", assigned_object=interface)
        reservation = HostReservation.objects.create(
            name="lock-scope-relevant",
            dhcp_server=DHCPServer.objects.create(name="lock-scope-relevant"),
            ipv4_address=address,
        )
        url = reverse("dcim:device_delete", args=[interface.device_id])
        self.client.force_login(self.user)
        with self._during_import("relevant-device"):
            response = self.client.post(url, {"confirm": True}, follow=True)
            self.assertContains(response, _CONTENTION)
            self.assertTrue(Device.objects.filter(pk=interface.device_id).exists())
            self.assertEqual(HostReservation.objects.get(pk=reservation.pk).ipv4_address_id, address.pk)
        response = self.client.post(url, {"confirm": True})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Device.objects.filter(pk=interface.device_id).exists())
        self.assertIsNone(HostReservation.objects.get(pk=reservation.pk).ipv4_address_id)

    def _deletion_raced_by_a_writer(self, deleted, paused_at, target, change):
        paused, release, outcome = Event(), Event(), []

        def pause(execute, sql, params, many, context):
            if not paused.is_set() and paused_at(sql):
                paused.set()
                release.wait(20)
            return execute(sql, params, many, context)

        def delete():
            try:
                with connections["default"].execute_wrapper(pause):
                    outcome.append(self._delete_in_transaction(deleted))
            finally:
                connections.close_all()

        def write():
            try:
                current = type(target).objects.get(pk=target.pk)
                for field, value in change.items():
                    setattr(current, field, value)
                current.save()
            finally:
                connections.close_all()

        workers = self._workers()
        deleting = workers.submit(delete)
        try:
            self.assertTrue(paused.wait(20), "The deletion did not reach the paused statement")
            workers.submit(write).result(timeout=20)
        finally:
            release.set()
        deleting.result(timeout=20)
        return outcome

    def test_a_deletion_made_relevant_after_its_probe_refuses_before_any_change(self):
        from ipam.models import IPAddress
        from netbox_dhcp.models import DHCPServer

        _, _, _, target, link = self._imported("reservation", 4, "made-relevant")
        empty = DHCPServer.objects.create(name="lock-scope-empty")
        loose = IPAddress.objects.create(address="203.0.113.50/24")
        cases = (
            # A writer moves the mapped reservation under the probed DHCP server before the native collection.
            ("collection", empty, lambda sql: sql.startswith("SAVEPOINT"), {"dhcp_server": empty}),
            # A writer commits a reference to the probed address before the row fence.
            ("fence", loose, lambda sql: sql.endswith("FOR UPDATE"), {"ipv4_address": loose}),
        )
        for name, deleted, paused_at, change in cases:
            with self.subTest(name):
                outcome = self._deletion_raced_by_a_writer(deleted, paused_at, target, change)
                self.assertEqual(outcome, [_STALE_GATE])
                self.assertTrue(type(deleted).objects.filter(pk=deleted.pk).exists())
                current = type(target).objects.get(pk=target.pk)
                for field, value in change.items():
                    self.assertEqual(getattr(current, f"{field}_id"), value.pk)
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk, object_id=target.pk).exists())

    def test_dependency_delete_during_replay_rolls_back_the_whole_revert(self):
        _, _, _, target, link = self._imported("subnet", 4, "dependency-replay")
        prefix = target.prefix
        branch = _provisioned_branch(self, "lock-scope-dependency-replay")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        branch.merge(user=self.user)
        branch.refresh_from_db()
        applied = branch.applied_changes.count()
        paused, release, replay_thread, outcome = Event(), Event(), [], []

        def pause_restore(sender, instance, **kwargs):
            if get_ident() in replay_thread and instance.pk == target.pk:
                paused.set()
                release.wait(20)

        def revert():
            replay_thread.append(get_ident())
            try:
                Branch.objects.get(pk=branch.pk).revert(user=self.user)
            except Exception as error:  # noqa: BLE001 - the test asserts the native failure type
                outcome.append(error)
            finally:
                connections.close_all()

        workers = self._workers()
        pre_save.connect(pause_restore, sender=type(target), weak=False)
        try:
            replay = workers.submit(revert)
            try:
                self.assertTrue(paused.wait(20), "Native revert did not reach the target restoration")
                deleted = workers.submit(self._delete_in_transaction, prefix).result(timeout=20)
            finally:
                release.set()
            replay.result(timeout=30)
        finally:
            pre_save.disconnect(pause_restore, sender=type(target))
        self.assertIsNone(deleted)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], IntegrityError)
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertEqual(branch.applied_changes.count(), applied)
        self.assertFalse(type(prefix).objects.filter(pk=prefix.pk).exists())
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_tag_rename_before_replay_validation_refuses_the_merge(self):
        from extras.models import Tag

        _, _, _, target, link = self._imported("subnet", 4, "tag-before-replay")
        tag = Tag.objects.create(name="lock-scope-tag", slug="lock-scope-tag")
        target.tags.add(tag)
        branch = _provisioned_branch(self, "lock-scope-tag-before-replay")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        self._delete(branch, target)
        renamed, release = Event(), Event()
        rename_pid, merge_pid, outcome = [], [], []

        def rename():
            try:
                with transaction.atomic():
                    self._backend_pid(rename_pid)
                    changed = Tag.objects.get(pk=tag.pk)
                    changed.name = "lock-scope-tag-renamed"
                    changed.save()
                    renamed.set()
                    release.wait(20)
            finally:
                connections.close_all()

        def merge():
            try:
                self._backend_pid(merge_pid)
                Branch.objects.get(pk=branch.pk).merge(user=self.user)
            except AbortRequest as error:
                outcome.append(error.message)
            finally:
                connections.close_all()

        workers = self._workers()
        renaming = workers.submit(rename)
        try:
            self.assertTrue(renamed.wait(20), "The Tag rename did not run")
            merging = workers.submit(merge)
            blocked = self._blocked_by(merge_pid, rename_pid, merging)
        finally:
            release.set()
        renaming.result(timeout=20)
        merging.result(timeout=30)
        self.assertTrue(blocked, "Replay validation did not wait on the open Tag rename")
        self.assertEqual(len(outcome), 1, outcome)
        self.assertIn("Main Tag data has changed", outcome[0])
        branch.refresh_from_db()
        self.assertEqual(branch.status, "ready")
        self.assertFalse(branch.applied_changes.exists())
        self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertEqual(Tag.objects.get(pk=tag.pk).name, "lock-scope-tag-renamed")

    @override_settings(EVENTS_PIPELINE=[_EVENTS_RECORDER])
    def test_lock_error_at_a_coordinated_view_commit_returns_the_refusal(self):
        self.client.force_login(self.user)
        self.client.get(reverse("home"))
        for api in (False, True):
            with self.subTest(api=api):
                _, _, _, target, link = self._imported("subnet", 4, f"commit-lock-{api}")
                route = "plugins-api:netbox_dhcp-api:subnet-detail" if api else "plugins:netbox_dhcp:subnet_delete"
                url = reverse(route, args=[target.pk])
                referer = f"http://testserver{reverse('plugins:netbox_dhcp:subnet_list')}"
                holder = connection.Database.connect(**connection.get_connection_params())
                DISPATCHED_EVENTS.clear()
                try:
                    # The view's COMMIT checks the ObjectChange user reference while another transaction locks it.
                    with holder.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM users_user WHERE id = %s FOR UPDATE", [self.user.pk])
                    with connection.cursor() as cursor:
                        cursor.execute("SET lock_timeout = '300ms'")
                    if api:
                        response = self.client.delete(url)
                    else:
                        response = self.client.post(url, {"confirm": True}, headers={"referer": referer})
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute("RESET lock_timeout")
                    holder.rollback()
                    holder.close()
                if api:
                    self.assertEqual(response.status_code, 400, response.content[:500])
                    self.assertEqual(response.json(), {"detail": _LOCK_CONFLICT})
                else:
                    self.assertEqual((response.status_code, response.url), (302, referer))
                    self.assertIn(_LOCK_CONFLICT, [str(message) for message in get_messages(response.wsgi_request)])
                self.assertEqual(DISPATCHED_EVENTS, [])
                self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
                self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())
                # The recorder sees the same delete once nothing holds the lock, so the empty list above means something.
                response = self.client.delete(url) if api else self.client.post(url, {"confirm": True})
                self.assertIn(response.status_code, (204, 302))
                self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
                self.assertTrue(DISPATCHED_EVENTS)

    def test_deadlock_in_an_uncoordinated_deletion_maps_to_the_retry_refusal(self):
        from ipam.models import IPAddress

        first = IPAddress.objects.create(address="203.0.113.61/24")
        second = IPAddress.objects.create(address="203.0.113.62/24")
        holder = connection.Database.connect(**connection.get_connection_params())
        deleter_pid, holder_pid, outcome = [], [], []

        def delete():
            try:
                self._backend_pid(deleter_pid)
                with transaction.atomic():
                    IPAddress.objects.filter(pk__in=[first.pk, second.pk]).delete()
            except Exception as error:  # noqa: BLE001 - the test asserts the refusal type
                outcome.append(error)
            finally:
                connections.close_all()

        def cross():
            with holder.cursor() as cursor:
                cursor.execute("SELECT 1 FROM ipam_ipaddress WHERE id = %s FOR UPDATE", [first.pk])

        workers = self._workers()
        try:
            with holder.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                holder_pid.append(cursor.fetchone()[0])
                cursor.execute("SELECT 1 FROM ipam_ipaddress WHERE id = %s FOR UPDATE", [second.pk])
            deleting = workers.submit(delete)
            blocked = self._blocked_by(deleter_pid, holder_pid, deleting)
            crossing = workers.submit(cross)
            deleting.result(timeout=20)
            crossing.result(timeout=20)
        finally:
            holder.rollback()
            holder.close()
        self.assertTrue(blocked, "The deletion did not wait on the second address")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], AbortRequest, repr(outcome[0]))
        self.assertEqual(outcome[0].message, _LOCK_CONFLICT)
        self.assertIsInstance(outcome[0].__cause__, OperationalError)
        self.assertEqual(IPAddress.objects.filter(pk__in=[first.pk, second.pk]).count(), 2)

    def test_import_commit_fk_failure_maps_to_the_importer_refusal(self):
        from dcim.models import MACAddress
        from netbox_dhcp.models import HostReservation

        server = _make_db_server(name="lock-scope-commit", ca_url="https://kea.example.invalid", dhcp6=False)
        mac = MACAddress.objects.create(mac_address="02:00:00:00:00:31")
        config = {"subnet4": []}
        observation = _reservation_snapshot(config, 4, [{"subnet-id": 0, "hw-address": "02:00:00:00:00:31"}])
        intent = parse_dhcp_config(config, 4)
        workers = self._workers()
        deleted = []

        def delete_the_referenced_mac(sender, instance, created, **kwargs):
            if created and instance.hw_address_id == mac.pk and not deleted:
                deleted.append(workers.submit(self._delete_in_transaction, mac).result(timeout=20))

        post_save.connect(delete_the_referenced_mac, sender=HostReservation, weak=False)
        try:
            with self.assertRaises(AbortRequest) as refused, event_tracking(_change_request(self.user)):
                import_server_config(server, intent, observation)
        finally:
            post_save.disconnect(delete_the_referenced_mac, sender=HostReservation)
        self.assertEqual(deleted, [None])
        self.assertEqual(
            refused.exception.message,
            "The DHCPv4 import referenced an object that no longer exists. Nothing changed for DHCPv4. "
            "Run the import again.",
        )
        self.assertIsInstance(refused.exception.__cause__, IntegrityError)
        self.assertFalse(MACAddress.objects.filter(pk=mac.pk).exists())
        self.assertFalse(HostReservation.objects.filter(name__startswith=server.name).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(server=server).exists())

    @override_settings(EVENTS_PIPELINE=[_EVENTS_RECORDER])
    def test_import_view_commit_failure_dispatches_only_the_committed_family_events(self):
        from dcim.models import MACAddress
        from netbox_dhcp.models import HostReservation

        server = _make_db_server(
            name="lock-scope-two-family", ca_url="https://kea.example.invalid", sync_dhcp_plugin_enabled=True
        )
        mac = MACAddress.objects.create(mac_address="02:00:00:00:00:41")
        hosts = {
            4: _KEA_OBJECTS[4].reservation,
            6: {"subnet-id": 10, "hw-address": "02:00:00:00:00:41", "ip-addresses": ["2001:db8:1::16"]},
        }
        kea = _recorded_kea() | {"reservation-get-page": lambda body: _res_page([hosts[_family(body)]])}
        workers = self._workers()
        deleted, doomed = [], []

        def delete_the_referenced_mac(sender, instance, created, **kwargs):
            # Only the DHCPv6 reservation references this MAC, so the DHCPv4 import commits first.
            if created and instance.hw_address_id == mac.pk and not deleted:
                doomed.append(instance.pk)
                deleted.append(workers.submit(self._delete_in_transaction, mac).result(timeout=20))

        self.client.force_login(self.user)
        DISPATCHED_EVENTS.clear()
        post_save.connect(delete_the_referenced_mac, sender=HostReservation, weak=False)
        try:
            with stub_kea(kea):
                response = self.client.post(reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[server.pk]))
        finally:
            post_save.disconnect(delete_the_referenced_mac, sender=HostReservation)
        self.assertEqual(deleted, [None])
        self.assertEqual(response.status_code, 302)
        refusal = "The DHCPv6 import referenced an object that no longer exists. Nothing changed for DHCPv6. "
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)], [f"{refusal}Run the import again."]
        )
        self.assertFalse(KeaDhcpLink.objects.filter(server=server, family=6).exists())
        committed = list(KeaDhcpLink.objects.filter(server=server, family=4))
        self.assertTrue(committed)
        dispatched = {(event["object_type"].model_class(), event["object_id"]) for event in DISPATCHED_EVENTS}
        self.assertTrue({(type(link.sys4_object), link.object_id) for link in committed} <= dispatched, dispatched)
        self.assertNotIn((HostReservation, doomed[0]), dispatched)
        for event in DISPATCHED_EVENTS:
            model, pk = event["object_type"].model_class(), event["object_id"]
            with self.subTest(model=model, pk=pk, event_type=event["event_type"]):
                self.assertTrue(model.objects.filter(pk=pk).exists(), "An event names a row that never committed")

    def test_select_for_update_queryset_delete_in_autocommit_keeps_the_native_behavior(self):
        from ipam.models import IPAddress
        from netbox_dhcp.models import DHCPServer, HostReservation

        unrelated = IPAddress.objects.create(address="203.0.113.71/24")
        relevant = IPAddress.objects.create(address="203.0.113.72/24")
        reservation = HostReservation.objects.create(
            name="lock-scope-for-update",
            dhcp_server=DHCPServer.objects.create(name="lock-scope-for-update"),
            ipv4_address=relevant,
        )
        self.assertTrue(connection.get_autocommit())
        results = {}
        for name, address in (("unrelated", unrelated), ("relevant", relevant)):
            with self.subTest(name), CaptureQueriesContext(connection) as captured:
                results[name] = IPAddress.objects.filter(pk=address.pk).select_for_update().delete()
                self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())
                coordinated = any("pg_advisory_xact_lock" in query["sql"] for query in captured.captured_queries)
                self.assertEqual(coordinated, name == "relevant")
        self.assertEqual(results["unrelated"], (1, {"ipam.IPAddress": 1}))
        self.assertEqual(results["relevant"], (1, {"ipam.IPAddress": 1}))
        self.assertIsNone(HostReservation.objects.get(pk=reservation.pk).ipv4_address_id)

    def test_import_view_shows_the_retry_refusal(self):
        from netbox_kea.dhcp_mapping_lifecycle import _METADATA_LOCK

        server = _make_db_server(
            name="lock-scope-view", ca_url="https://kea.example.invalid", dhcp6=False, sync_dhcp_plugin_enabled=True
        )
        self.client.force_login(self.user)
        holder = connection.Database.connect(**connection.get_connection_params())
        try:
            with holder.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s, %s)", _METADATA_LOCK)
            with stub_kea(_recorded_kea()), transaction.atomic():
                response = self.client.post(reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[server.pk]))
        finally:
            holder.close()
        self.assertEqual(response.status_code, 302)
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)], [_CONTENTION])
        self.assertFalse(KeaDhcpLink.objects.filter(server=server).exists())
