# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Real importer and native branch recovery preserve DHCP source associations."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
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
from django.core.serializers.json import DjangoJSONEncoder
from django.core.signals import request_finished
from django.db import connection, connections, transaction
from django.db.models.signals import m2m_changed, post_delete, pre_delete, pre_save
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
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
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.test_branching import _change_request, _device_with_interface, _provisioned_branch, _recorded_kea
from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot
from netbox_kea.tests.utils import _make_db_server


class DhcpMappingRecoveryTest(TransactionTestCase):
    def setUp(self):
        from django.apps import apps

        if not apps.is_installed("netbox_dhcp"):
            self.skipTest("netbox_dhcp is not installed")
        self.user = get_user_model().objects.create_superuser("mapping-recovery-admin")

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
                    if kind == "classes":
                        self.assertEqual(len(set(deletion_ids)), 2)
                        self.assertNotIn(target_id, deletion_ids)
                    else:
                        self.assertFalse(deletion_ids)
                self.assertEqual({change.pk: change.prechange_data for change in branch.get_changes()}, history)
                branch.revert(user=self.user)
                restored = type(target).objects.get(pk=target.pk)
                self.assertEqual(
                    set(getattr(restored, field).values_list("pk", flat=True)), {e.pk for e in endpoints[:2]}
                )
                self.assertEqual(restored.description, target.description)

    def test_named_endpoint_creation_composes_with_mapped_target_update(self):
        from extras.models import Tag

        _, _, _, target, link = self._imported("subnet", 6, "created-named-endpoint")
        branch = _provisioned_branch(self, "mapping-created-named-endpoint")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(self.user)):
            tag = Tag.objects.create(name="mapping-created-endpoint", slug="mapping-created-endpoint")
            local = type(target).objects.get(pk=target.pk)
            local.snapshot()
            local.tags.add(tag)
        request_finished.send(sender=type(self))
        history = {change.pk: deepcopy(change.prechange_data) for change in branch.get_changes()}
        branch.merge(user=self.user)
        self.assertEqual(list(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)), [tag.pk])
        self.assertEqual(Tag.objects.get(name=tag.name).pk, tag.pk)
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, link.created)
        self.assertEqual({change.pk: change.prechange_data for change in branch.get_changes()}, history)
        branch.revert(user=self.user)
        self.assertFalse(type(target).objects.get(pk=target.pk).tags.exists())
        self.assertFalse(Tag.objects.filter(pk=tag.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_named_endpoint_deletion_composes_with_mapped_target_deletion(self):
        target, link, endpoints, _field, branch = self._cleanup_target("tags", "named-target-delete")
        endpoint_created = parse_datetime(endpoints[0].serialize_object()["created"])
        self._delete(branch, endpoints[0])
        self._delete(branch, target)
        history = {
            change.pk: (deepcopy(change.prechange_data), deepcopy(change.postchange_data))
            for change in branch.get_changes()
        }
        branch.merge(user=self.user)
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertFalse(type(endpoints[0]).objects.filter(pk=endpoints[0].pk).exists())
        self.assertTrue(type(endpoints[1]).objects.filter(pk=endpoints[1].pk).exists())
        branch.revert(user=self.user)
        restored = type(target).objects.get(pk=target.pk)
        self.assertEqual(restored.created, target.created)
        self.assertEqual(set(restored.tags.values_list("pk", flat=True)), {endpoint.pk for endpoint in endpoints[:2]})
        restored_mapping = KeaDhcpLink.objects.get(pk=link.pk)
        self.assertEqual(
            (restored_mapping.object_id, restored_mapping.server_id, restored_mapping.created),
            (target.pk, link.server_id, link.created),
        )
        restored_endpoint = type(endpoints[0]).objects.get(name=endpoints[0].name)
        self.assertEqual((restored_endpoint.pk, restored_endpoint.created), (endpoints[0].pk, endpoint_created))
        self.assertEqual(
            {change.pk: (change.prechange_data, change.postchange_data) for change in branch.get_changes()}, history
        )

    def test_named_endpoint_creation_composes_with_mapped_target_creation(self):
        from dcim.models import MACAddress
        from extras.models import Tag
        from netbox_dhcp.models import HostReservation

        server, _, _, original, original_mapping = self._imported("reservation", 4, "named-target-create")
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
            link = KeaDhcpLink.objects.create(
                server=server,
                family=4,
                kea_identity="hw-address:02:00:00:00:00:08",
                object_type=ContentType.objects.get_for_model(target),
                object_id=target.pk,
            )
        request_finished.send(sender=type(self))
        history = {
            change.pk: (deepcopy(change.prechange_data), deepcopy(change.postchange_data))
            for change in branch.get_changes()
        }
        endpoint_created = parse_datetime(endpoint.serialize_object()["created"])
        branch.merge(user=self.user)
        restored = HostReservation.objects.get(pk=target.pk)
        self.assertEqual(restored.created, target.created)
        self.assertEqual(list(restored.tags.values_list("pk", flat=True)), [endpoint.pk])
        restored_endpoint = Tag.objects.get(name=endpoint.name)
        self.assertEqual((restored_endpoint.pk, restored_endpoint.created), (endpoint.pk, endpoint_created))
        self.assertEqual(
            (KeaDhcpLink.objects.get(pk=link.pk).object_id, KeaDhcpLink.objects.get(pk=link.pk).created),
            (target.pk, link.created),
        )
        self.assertEqual(
            {change.pk: (change.prechange_data, change.postchange_data) for change in branch.get_changes()}, history
        )
        branch.revert(user=self.user)
        self.assertFalse(HostReservation.objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())
        self.assertFalse(Tag.objects.filter(pk=endpoint.pk).exists())
        self.assertTrue(HostReservation.objects.filter(pk=original.pk).exists())
        self.assertTrue(KeaDhcpLink.objects.filter(pk=original_mapping.pk, object_id=original.pk).exists())

    def test_named_endpoint_cleanup_records_complete_target_history(self):
        target, link, endpoints, _field, branch = self._cleanup_target("tags", "native-named-history")
        self._delete(branch, endpoints[0])
        latest = (
            branch.get_changes()
            .filter(changed_object_type=ContentType.objects.get_for_model(target), changed_object_id=target.pk)
            .latest("time")
        )
        self.assertEqual(latest.postchange_data["tags"], [endpoints[1].name])
        history = {
            change.pk: (deepcopy(change.prechange_data), deepcopy(change.postchange_data))
            for change in branch.get_changes()
        }
        branch.merge(user=self.user)
        self.assertEqual(
            list(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)), [endpoints[1].pk]
        )
        self.assertFalse(type(endpoints[0]).objects.filter(pk=endpoints[0].pk).exists())
        branch.revert(user=self.user)
        self.assertEqual(
            set(type(target).objects.get(pk=target.pk).tags.values_list("pk", flat=True)),
            {endpoint.pk for endpoint in endpoints[:2]},
        )
        self.assertTrue(type(endpoints[0]).objects.filter(pk=endpoints[0].pk).exists())
        self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).created, link.created)
        self.assertEqual(
            {change.pk: (change.prechange_data, change.postchange_data) for change in branch.get_changes()}, history
        )

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
            with self.assertRaisesMessage(AbortRequest, "relation dependency is missing"):
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

    def test_native_named_cleanup_preserves_later_main_changes_and_refuses_revert(self):
        for effect in ("rename", "source", "scalar"):
            with self.subTest(effect=effect):
                target, link, endpoints, _field, branch = self._cleanup_target("tags", f"named-cleanup-later-{effect}")
                replacement_server = _make_db_server(name=f"mapping-later-source-{effect}")
                self._delete(branch, endpoints[0])
                with activate_branch(branch), event_tracking(_change_request(self.user)):
                    local = type(target).objects.get(pk=target.pk)
                    local.snapshot()
                    local.description = "completed cleanup"
                    local.save()
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
                    if using != "default" or instance.pk != endpoints[0].pk:
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

                post_delete.connect(change_main, sender=type(endpoints[0]), weak=False)
                try:
                    branch.merge(user=self.user)
                finally:
                    post_delete.disconnect(change_main, sender=type(endpoints[0]))
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
                self.assertFalse(type(endpoints[0]).objects.filter(pk=endpoints[0].pk).exists())
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
