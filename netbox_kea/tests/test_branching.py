# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Only DHCP Import Mappings follow branches; other plugin rows stay in main (ADRs 0007 and 0008).

The CI branching job sets NETBOX_KEA_REQUIRE_BRANCHING=1, so this module fails instead of
skipping when netbox-branching is not an installed app.
"""

import importlib
import json
import os
import re
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from ipaddress import ip_address
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import pytest

from netbox_kea import branching
from netbox_kea.branching import APP_LABEL

_REQUIRE_BRANCHING = "NETBOX_KEA_REQUIRE_BRANCHING"
_required = os.environ.get(_REQUIRE_BRANCHING, "")
if _required not in {"", "1"}:
    raise ValueError(f"{_REQUIRE_BRANCHING} must be 1 or unset, not {_required!r}")
if not branching.installed():
    if _required:
        raise RuntimeError(f"{_REQUIRE_BRANCHING}=1, but netbox_branching is not an installed app")
    pytest.skip("netbox-branching is not an installed app", allow_module_level=True)

from core.events import OBJECT_DELETED  # noqa: E402
from core.exceptions import JobFailed  # noqa: E402
from core.models import Job, ObjectType  # noqa: E402
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site  # noqa: E402
from django.apps import apps  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.signals import request_finished  # noqa: E402
from django.db import connection, connections, models, router, transaction  # noqa: E402
from django.db.migrations import RunPython, RunSQL, SeparateDatabaseAndState  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402
from django.db.migrations.operations.base import Operation  # noqa: E402
from django.db.migrations.operations.models import ModelOperation  # noqa: E402
from django.db.models import ProtectedError  # noqa: E402
from django.db.models.signals import pre_delete, pre_save  # noqa: E402
from django.test import Client, RequestFactory, SimpleTestCase, TransactionTestCase, override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext, isolate_apps  # noqa: E402
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse  # noqa: E402
from django.utils import timezone  # noqa: E402
from django.utils.html import escape  # noqa: E402
from extras.models import Tag  # noqa: E402
from ipam.models import VRF, IPAddress, IPRange, Prefix  # noqa: E402
from netaddr import IPNetwork  # noqa: E402
from netbox.context_managers import event_tracking  # noqa: E402
from netbox_branching import utilities as branching_utilities  # noqa: E402
from netbox_branching.choices import BranchStatusChoices  # noqa: E402
from netbox_branching.constants import BRANCH_HEADER, COOKIE_NAME, QUERY_PARAM  # noqa: E402
from netbox_branching.models import Branch  # noqa: E402
from netbox_branching.utilities import activate_branch, supports_branching  # noqa: E402
from rest_framework.permissions import SAFE_METHODS  # noqa: E402
from utilities.exceptions import AbortRequest  # noqa: E402

from netbox_kea import server_configuration  # noqa: E402
from netbox_kea.jobs import KeaIpamSyncJob  # noqa: E402
from netbox_kea.kea import KeaCommand, KeaException  # noqa: E402
from netbox_kea.models import IPAMOwnershipLink, KeaDhcpLink, Server, SyncConfig, next_confirmation_number  # noqa: E402
from netbox_kea.tests.kea_stub import _leases_per_subnet, _res_get, _res_page, _subnet_stats, stub_kea  # noqa: E402
from netbox_kea.tests.utils import (  # noqa: E402
    _WRITE_VERBS,
    DISPATCHED_EVENTS,
    _make_db_server,
    _refusal_receivers,
    linked_dhcp_targets,
)

# Changing this set needs a design decision (docs/design/ipam-ownership-branching.md).
EXPOSED_RELATIONS: frozenset[tuple[str, str]] = frozenset(
    (f"netbox_kea.IPAMOwnershipLink.{key}", "CASCADE") for key in ("ip_address", "prefix", "ip_range")
)
_DESIGN_DECISION = (
    "This change needs a design decision (docs/design/ipam-ownership-branching.md): the resolver keeps "
    "main-only plugin models in main, so a delete in a branch that reaches one of these relations writes main's table."
)

# The on_delete handlers that do not write the referencing row. Every other one does (SET(...) and DB_* too).
_NON_WRITING_ON_DELETE = (models.PROTECT, models.RESTRICT, models.DO_NOTHING)


def _plugin_models() -> list[type[models.Model]]:
    return list(apps.get_app_config(APP_LABEL).get_models())


def _rule_models() -> list[type[models.Model]]:
    """Return the models that guard 2 reads: every plugin model, the auto-created through models of an M2M too."""
    return list(apps.get_app_config(APP_LABEL).get_models(include_auto_created=True))


def _on_delete_name(field: models.ForeignObject) -> str:
    handler = field.remote_field.on_delete
    return getattr(handler, "__name__", repr(handler))


def _relation_key(field: models.ForeignObject) -> tuple[str, str]:
    return f"{field.model._meta.label}.{field.name}", _on_delete_name(field)


def _writing_relations(model: type[models.Model]) -> list[models.ForeignObject]:
    """Return each forward many-to-one or one-to-one relation of *model* whose on_delete writes its row."""
    return [
        field
        for field in model._meta.get_fields()
        if isinstance(field, models.ForeignObject)
        and not field.auto_created
        and field.remote_field.on_delete not in _NON_WRITING_ON_DELETE
    ]


def _exposed_relations(rule_models: Sequence[type[models.Model]]) -> dict[tuple[str, str], list[str]]:
    """Return each relation that a delete in a branch reaches, with the relation path that reaches it.

    A writing relation to a branchable model outside the plugin is exposed. A writing relation to a plugin
    model that holds an exposed relation is exposed too, so the rule is transitive.
    """
    exposed: dict[tuple[str, str], list[str]] = {}
    reached: dict[str, list[str]] = {}
    grown = True
    while grown:
        grown = False
        for model in rule_models:
            for field in _writing_relations(model):
                key, target = _relation_key(field), field.related_model._meta
                if key in exposed:
                    continue
                if target.app_label != APP_LABEL and supports_branching(field.related_model):
                    path = [f"{key[0]} ({key[1]}) -> {target.label}"]
                elif target.label in reached:
                    path = [f"{key[0]} ({key[1]})", *reached[target.label]]
                else:
                    continue
                exposed[key] = path
                reached.setdefault(model._meta.label, path)
                grown = True
    return exposed


def _guard_failures(rule_models: Sequence[type[models.Model]], pinned: frozenset[tuple[str, str]]) -> list[str]:
    """Return why the relations that a delete in a branch reaches do not match the design, or nothing."""
    exposed = _exposed_relations(rule_models)
    failures = []
    if set(exposed) != pinned:
        failures.append(
            "The relations that a delete in a branch reaches changed: "
            + "; ".join(" -> ".join(path) for path in exposed.values())
            + f". Pinned: {sorted(pinned)}."
        )
    receivers = _refusal_receivers(pre_delete)
    by_key = {_relation_key(field): field for model in rule_models for field in _writing_relations(model)}
    for key in exposed:
        field = by_key[key]
        model = field.model._meta
        if not (isinstance(field, models.ForeignKey) and field.concrete):
            failures.append(f"{key[0]} is not a concrete ForeignKey, so no pre_delete receiver refuses it.")
        if field.remote_field.on_delete is not models.CASCADE:
            failures.append(f"{key[0]} is {key[1]}, not CASCADE: it writes main's row with no pre_delete signal.")
        if model.auto_created:
            failures.append(f"{key[0]} is on the auto-created model {model.label}, which sends no pre_delete signal.")
        if model.label not in receivers:
            failures.append(f"{key[0]} is on {model.label}, which has no branch refusal receiver.")
    return [f"{failure} {_DESIGN_DECISION}" for failure in failures]


class BranchabilityPinTest(SimpleTestCase):
    """Guard 2: pin main-only relations and the one branchable plugin model."""

    def test_the_relations_that_a_delete_in_a_branch_reaches_are_the_pinned_set(self):
        failures = _guard_failures(_rule_models(), EXPOSED_RELATIONS)

        self.assertEqual(failures, [], "\n".join(failures))

    def test_netbox_branching_branches_only_dhcp_import_mappings(self):
        for model in _plugin_models():
            with self.subTest(model=model._meta.label):
                self.assertIs(supports_branching(model), model is KeaDhcpLink)

    def test_the_rule_reads_every_plugin_model(self):
        self.assertEqual(
            {model._meta.label for model in _rule_models()},
            {"netbox_kea.Server", "netbox_kea.SyncConfig", "netbox_kea.KeaDhcpLink", "netbox_kea.IPAMOwnershipLink"},
        )


def _ownership_keys() -> dict[str, models.ForeignKey]:
    return {
        name: models.ForeignKey(target, on_delete=models.CASCADE, null=True, related_name="+")
        for name, target in (("ip_address", IPAddress), ("prefix", Prefix), ("ip_range", IPRange))
    }


class BranchabilityGuardProbeTest(SimpleTestCase):
    """Guard 2 fails for each shape that the design refuses, shown with probe models in an isolated registry."""

    def _probe(self, name: str, **fields: models.Field) -> list[type[models.Model]]:
        """Define a probe model with *fields* and the refusal receivers, and return it with its through models."""
        with isolate_apps() as registry:
            meta = type("Meta", (), {"app_label": APP_LABEL})
            probe = type(name, (models.Model,), {"__module__": __name__, "Meta": meta, **fields})
        branching.connect_refusal(probe)
        for signal in (pre_save, pre_delete):
            self.addCleanup(signal.disconnect, sender=probe, dispatch_uid=branching.refusal_uid(probe))
        assert registry is not None, "isolate_apps() as a context manager returns its registry"
        return list(registry.all_models[APP_LABEL].values())

    def _keys(self, probe: str, *names: str) -> frozenset[tuple[str, str]]:
        return frozenset((f"{APP_LABEL}.{probe}.{name}", "CASCADE") for name in names)

    def test_a_probe_shaped_like_the_link_passes(self):
        rule_models = self._probe("LinkShapedProbe", **_ownership_keys())

        self.assertEqual(
            _guard_failures(rule_models, self._keys("LinkShapedProbe", "ip_address", "prefix", "ip_range")), []
        )

    def test_an_added_set_null_key_to_a_branchable_model_fails(self):
        rule_models = self._probe(
            "SetNullProbe", **_ownership_keys(), vrf=models.ForeignKey(VRF, on_delete=models.SET_NULL, null=True)
        )
        pinned = self._keys("SetNullProbe", "ip_address", "prefix", "ip_range")

        changed = _guard_failures(rule_models, pinned)
        pinned_too = _guard_failures(rule_models, pinned | {(f"{APP_LABEL}.SetNullProbe.vrf", "SET_NULL")})

        self.assertIn("SetNullProbe.vrf (SET_NULL) -> ipam.VRF", "\n".join(changed))
        self.assertEqual(len(pinned_too), 1, pinned_too)
        self.assertIn("SetNullProbe.vrf is SET_NULL, not CASCADE", pinned_too[0])
        self.assertIn("docs/design/ipam-ownership-branching.md", pinned_too[0])

    def test_an_auto_created_many_to_many_through_model_fails(self):
        rule_models = self._probe("ManyToManyProbe", prefixes=models.ManyToManyField(Prefix, related_name="+"))
        through = next(model for model in rule_models if model._meta.auto_created)
        pinned = frozenset({(f"{through._meta.label}.prefix", "CASCADE")})

        failures = "\n".join(_guard_failures(rule_models, pinned))

        self.assertIn(f"on the auto-created model {through._meta.label}", failures)

    def test_a_disconnected_refusal_receiver_on_the_link_fails(self):
        link = apps.get_model(APP_LABEL, "IPAMOwnershipLink")
        pre_delete.disconnect(sender=link, dispatch_uid=branching.refusal_uid(link))
        self.addCleanup(branching.connect_refusal, link)

        failures = _guard_failures(_rule_models(), EXPOSED_RELATIONS)

        self.assertEqual(len(failures), 3, failures)
        self.assertTrue(all("which has no branch refusal receiver" in failure for failure in failures), failures)


class ResolverTest(SimpleTestCase):
    """The resolver answers for plugin models only, also for the historical models of a migration."""

    def test_the_resolver_is_registered_once(self):
        self.assertEqual(branching_utilities._branching_resolvers.count(branching.is_branchable), 1)

    def test_the_resolver_defers_for_other_apps(self):
        self.assertIsNone(branching.is_branchable(VRF))

    def test_the_resolver_keeps_a_change_logged_server_in_main(self):
        self.assertFalse(branching.is_branchable(Server))
        self.assertFalse(supports_branching(Server))

    def test_the_resolver_keeps_a_historical_server_in_main(self):
        # Branch migrate hands the resolver historical models. At 0016, sync_vrf was still SET_NULL.
        state = MigrationLoader(None, ignore_no_migrations=True).project_state(
            (APP_LABEL, "0016_charfield_blank_not_null")
        )
        historical_server = state.apps.get_model(APP_LABEL, "Server")

        self.assertIs(branching.is_branchable(historical_server), False)

    def test_the_active_branch_is_the_branching_context(self):
        branch = Branch(name="context only")

        with activate_branch(branch):
            self.assertIs(branching.active_branch(), branch)
        self.assertIsNone(branching.active_branch())

    def test_refuse_in_branch_raises_branch_active_with_the_branch(self):
        branch = Branch(name="refusal only")

        with activate_branch(branch), self.assertRaises(branching.BranchActive) as refused:
            branching.refuse_in_branch("a test change")

        self.assertIs(refused.exception.branch, branch)
        self.assertEqual(refused.exception.operation, "a test change")

    def test_refuse_in_branch_does_nothing_on_main(self):
        self.assertIsNone(branching.refuse_in_branch("a test change"))

    def test_branch_active_is_not_a_kea_error(self):
        self.assertFalse(issubclass(branching.BranchActive, KeaException))

    def test_branch_active_is_an_abort_request_whose_message_escapes_the_branch_name(self):
        refused = branching.BranchActive("A test change", Branch(name="<b>probe</b>"))

        self.assertIsInstance(refused, AbortRequest)
        self.assertIn("A test change is refused. Branch &lt;b&gt;probe&lt;/b&gt; is active.", refused.message)
        self.assertIn("A test change is refused. Branch <b>probe</b> is active.", str(refused))


class SuiteConfigurationTest(SimpleTestCase):
    """The run loads netbox-branching as NetBox requires it."""

    def test_netbox_branching_is_the_last_plugin(self):
        self.assertEqual(settings.PLUGINS[-1], "netbox_branching")


def _database_operations(operations: Sequence[Operation]) -> list[Operation]:
    """Return the operations that reach the database, with each SeparateDatabaseAndState opened."""
    flat: list[Operation] = []
    for operation in operations:
        if isinstance(operation, SeparateDatabaseAndState):
            flat.extend(_database_operations(operation.database_operations))
        else:
            flat.append(operation)
    return flat


def _model_name(operation: Operation) -> str | None:
    if hasattr(operation, "model_name"):
        return operation.model_name
    if isinstance(operation, ModelOperation):
        return operation.name
    return None


def _writes_only_main_only_plugin_tables(operations: Sequence[Operation]) -> bool:
    """Say whether every operation is a model operation on a plugin model that stays in main.

    RunPython and RunSQL can write any table, so a migration with either is not decided here.
    """
    for operation in _database_operations(operations):
        name = _model_name(operation)
        if name is None:
            return False
        try:
            model = apps.get_model(APP_LABEL, name)
        except LookupError as exc:
            raise AssertionError(
                f"{operation!r} names {APP_LABEL}.{name}, which is not a live model. "
                "Decide fake_on_branch by hand, and teach this guard the case."
            ) from exc
        if supports_branching(model):
            return False
    return True


class MigrationFakeOnBranchTest(SimpleTestCase):
    """Guard 4: branch migrate runs each plugin migration only where the design says so."""

    def _migrations(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migrations = [migration for (app, _), migration in loader.disk_migrations.items() if app == APP_LABEL]
        self.assertIn("0001_initial", {migration.name for migration in migrations}, "the guard reads no migration")
        return sorted(migrations, key=lambda migration: migration.name)

    def _declared(self, migration):
        # netbox-branching reads the module attribute, not the Migration class.
        module = importlib.import_module(f"{APP_LABEL}.migrations.{migration.name}")
        return getattr(module, "fake_on_branch", None)

    def test_a_migration_that_runs_code_declares_fake_on_branch(self):
        code = (RunPython, RunSQL, SeparateDatabaseAndState)
        for migration in self._migrations():
            if not any(isinstance(operation, code) for operation in migration.operations):
                continue
            with self.subTest(migration=migration.name):
                self.assertIsInstance(
                    self._declared(migration),
                    bool,
                    f"{migration.name} runs RunPython, RunSQL or SeparateDatabaseAndState. Set the module "
                    "attribute fake_on_branch to True or False: netbox-branching cannot tell what it writes.",
                )

    def test_a_migration_that_writes_only_main_only_plugin_tables_is_faked_on_branch(self):
        for migration in self._migrations():
            if not _writes_only_main_only_plugin_tables(migration.operations):
                continue
            with self.subTest(migration=migration.name):
                self.assertIs(
                    self._declared(migration),
                    True,
                    f"{migration.name} changes only netbox_kea tables that stay in main. Set the module "
                    "attribute fake_on_branch = True, or branch migrate runs it through the branch connection.",
                )

    def test_netbox_branching_fakes_every_plugin_migration(self):
        # The private decision function of branch migrate: it reads the module attribute first.
        from netbox_branching.models.branches import _fake_for_branch

        for migration in self._migrations():
            with self.subTest(migration=migration.name):
                self.assertIs(_fake_for_branch(migration), True)


def _provisioned_branch(test: TransactionTestCase, name: str) -> Branch:
    branch = Branch(name=name)
    branch.save(provision=False)
    test.addCleanup(connections[_branch_alias(branch)].close)
    test.addCleanup(branch.deprovision)
    branch.provision(user=None)
    branch.refresh_from_db()
    test.assertEqual(branch.status, BranchStatusChoices.READY)
    return branch


class BranchConnectionCleanupTest(TransactionTestCase):
    def test_provisioned_branch_cleanup_closes_its_dynamic_connection(self):
        owner = TransactionTestCase()
        self.addCleanup(owner.doCleanups)
        branch = _provisioned_branch(owner, "connection cleanup")
        alias = _branch_alias(branch)
        with activate_branch(branch):
            VRF.objects.exists()
        self.assertIsNotNone(connections[alias].connection)

        self.assertTrue(owner.doCleanups())

        self.assertIsNone(connections[alias].connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [branch.schema_name])
            self.assertIsNone(cursor.fetchone())


class OptionalMappingReplayTest(TransactionTestCase):
    def test_native_merge_and_revert_do_not_acquire_the_mapping_lock_without_dhcp(self):
        from netbox_kea.dhcp_mapping_lifecycle import _METADATA_LOCK

        if apps.is_installed("netbox_dhcp"):
            self.skipTest("This profile verifies native replay without DHCP targets")
        user = get_user_model().objects.create_superuser("ordinary-native-replay-admin")
        for strategy in ("squash", "iterative"):
            with self.subTest(strategy=strategy):
                vrf = VRF.objects.create(name=f"ordinary native {strategy}", description="original")
                branch = _provisioned_branch(self, f"ordinary native {strategy}")
                branch.merge_strategy = strategy
                branch.save(provision=False)
                with activate_branch(branch), event_tracking(_change_request(user)):
                    local = VRF.objects.get(pk=vrf.pk)
                    local.snapshot()
                    local.description = "branch edit"
                    local.save()
                holder = connection.Database.connect(**connection.get_connection_params())
                try:
                    with holder.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_lock(%s, %s)", _METADATA_LOCK)
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", _METADATA_LOCK)
                            self.assertFalse(cursor.fetchone()[0])
                        branch.merge(user=user)
                        vrf.refresh_from_db()
                        self.assertEqual(vrf.description, "branch edit")
                        branch.revert(user=user)
                        vrf.refresh_from_db()
                        self.assertEqual(vrf.description, "original")
                finally:
                    holder.close()


class ProvisionedBranchTest(TransactionTestCase):
    """A fresh branch copies DHCP Import Mappings and reads other plugin rows from main."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A deployment runs migrate after the upgrade, which stores the resolver's answer.
        call_command("migrate", verbosity=0)

    def test_stored_features_enable_branching_only_for_dhcp_import_mappings(self):
        for model in _plugin_models():
            with self.subTest(model=model._meta.label):
                features = ObjectType.objects.get_for_model(model).features

                self.assertEqual("branching" in features, model is KeaDhcpLink)

    def test_a_provisioned_branch_copies_only_the_mapping_plugin_table(self):
        branch = _provisioned_branch(self, "mapping plugin table")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", [branch.schema_name]
            )
            tables = {row[0] for row in cursor.fetchall()}

        self.assertIn(VRF._meta.db_table, tables, "the branch copied no table, so the check below reads nothing")
        self.assertEqual({table for table in tables if table.startswith(f"{APP_LABEL}_")}, {KeaDhcpLink._meta.db_table})

    def test_a_server_edited_in_main_after_branch_creation_reads_mains_values_in_the_branch(self):
        server = _make_db_server(name="before-branch", ca_url="https://before.example.com")
        branch = _provisioned_branch(self, "server edited in main")

        server.name, server.ca_url = "after-branch", "https://after.example.com"
        server.save()
        with activate_branch(branch):
            in_branch = Server.objects.get(pk=server.pk)

        self.assertEqual((in_branch.name, in_branch.ca_url), ("after-branch", "https://after.example.com"))

    def test_a_vrf_delete_in_a_branch_is_refused_while_a_server_syncs_into_it(self):
        vrf = VRF.objects.create(name="sync target")
        server = _make_db_server(sync_vrf=vrf)
        branch = _provisioned_branch(self, "vrf delete")

        with activate_branch(branch), self.assertRaises(ProtectedError) as refused:
            VRF.objects.get(pk=vrf.pk).delete()

        self.assertEqual({type(obj) for obj in refused.exception.protected_objects}, {Server})
        with activate_branch(branch):
            self.assertTrue(VRF.objects.filter(pk=vrf.pk).exists(), "the branch copy of the VRF is gone")
        self.assertTrue(VRF.objects.filter(pk=vrf.pk).exists(), "main's VRF is gone")
        server.refresh_from_db()
        self.assertEqual(server.sync_vrf_id, vrf.pk)

    def test_a_merge_that_deletes_a_vrf_a_server_now_syncs_into_fails_and_changes_nothing(self):
        vrf = VRF.objects.create(name="unused at branch time")
        server = _make_db_server()
        user = get_user_model().objects.create_user("merge-user")
        branch = _provisioned_branch(self, "vrf delete then merge")
        request = RequestFactory().get("/")
        request.user, request.id = user, uuid.uuid4()
        with activate_branch(branch), event_tracking(request):
            VRF.objects.get(pk=vrf.pk).delete()
        server.sync_vrf = vrf
        server.save()

        with self.assertRaises(ProtectedError):
            branch.merge(user=user)

        branch.refresh_from_db()
        self.assertEqual(
            branch.status, BranchStatusChoices.READY, "netbox-branching restores READY after a failed merge"
        )
        self.assertTrue(VRF.objects.filter(pk=vrf.pk).exists(), "the merge deleted main's VRF")
        server.refresh_from_db()
        self.assertEqual(server.sync_vrf_id, vrf.pk)


# The Server row that each request below tries to change, and the new value it sends.
_BEFORE, _AFTER = "https://before.example.com", "https://after.example.com"
_VERSION_OK = {"result": 0, "arguments": {"extended": "3.2.0"}}


def _branch_alias(branch: Branch) -> str:
    """Return the connection alias that netbox-branching routes branchable writes to while *branch* is active."""
    with activate_branch(branch):
        return router.db_for_write(VRF)


@contextmanager
def _writes(branch: Branch) -> Iterator[list[str]]:
    """Collect each INSERT, UPDATE or DELETE statement on main's connection and on the branch connection."""
    writes: list[str] = []
    with ExitStack() as stack:
        captures = [
            stack.enter_context(CaptureQueriesContext(connections[alias]))
            for alias in ("default", _branch_alias(branch))
        ]
        yield writes
    writes.extend(
        query["sql"]
        for capture in captures
        for query in capture.captured_queries
        if query["sql"].lstrip().upper().startswith(_WRITE_VERBS)
    )


def _logged_in_client(user) -> tuple[Client, str]:
    """Return a client that enforces CSRF like a browser, and the CSRF token that a page on main gave it."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    client.get(reverse("plugins:netbox_kea:server_list"))
    return client, client.cookies[settings.CSRF_COOKIE_NAME].value


@dataclass(frozen=True)
class _Selection:
    """One row of the design's selector table: how a change request selects a branch, and its outcome.

    ``header``, ``query`` and ``cookie`` name a branch state: ready, an unready state (merged, archived,
    failed), unknown, or "" (empty). A UI row sends POST; an API row sends ``method``.
    """

    name: str
    outcome: str
    api: bool = False
    htmx: bool = False
    header: str | None = None
    query: str | None = None
    cookie: str | None = None
    csrf: bool = True
    method: str = "PATCH"


_UNREADY = {
    "merged": BranchStatusChoices.MERGED,
    "archived": BranchStatusChoices.ARCHIVED,
    "failed": BranchStatusChoices.FAILED,
}
_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

_SELECTIONS = (
    _Selection("UI, no selector", "served"),
    _Selection("UI, X-NetBox-Branch header naming a ready branch", "served", header="ready"),
    _Selection("UI, empty active_branch cookie", "served", cookie=""),
    _Selection("UI, query naming an unknown branch", "nbb-400", query="unknown"),
    _Selection("UI, query naming a ready branch", "refused", query="ready"),
    _Selection("UI, cookie naming an unknown branch", "unusable", cookie="unknown"),
    _Selection("UI, cookie naming a ready branch", "refused", cookie="ready"),
    _Selection("UI, cookie naming a ready branch, no CSRF token", "csrf-403", cookie="ready", csrf=False),
    _Selection("HTMX, cookie naming a ready branch", "refused", htmx=True, cookie="ready"),
    _Selection("HTMX, query naming a ready branch", "refused", htmx=True, query="ready"),
    _Selection("HTMX, cookie naming an unknown branch", "unusable", htmx=True, cookie="unknown"),
    _Selection("API, no selector", "served", api=True),
    _Selection("API, empty active_branch cookie", "served", api=True, cookie=""),
    _Selection("API, header naming an unknown branch", "nbb-400", api=True, header="unknown"),
    _Selection("API, header naming a ready branch", "refused", api=True, header="ready"),
    _Selection("API, query naming an unknown branch", "nbb-400", api=True, query="unknown"),
    _Selection("API, query naming a ready branch", "refused", api=True, query="ready"),
    _Selection("API, cookie naming an unknown branch", "unusable", api=True, cookie="unknown"),
    _Selection("API, cookie naming a ready branch", "refused", api=True, cookie="ready"),
    *(
        row
        for state in _UNREADY
        for row in (
            _Selection(f"UI, query naming the {state} branch", "unusable", query=state),
            _Selection(f"UI, empty query with the {state} branch's cookie", "served", query="", cookie=state),
            _Selection(f"UI, cookie naming the {state} branch", "unusable", cookie=state),
            _Selection(f"HTMX, cookie naming the {state} branch", "unusable", htmx=True, cookie=state),
            _Selection(f"HTMX, query naming the {state} branch", "unusable", htmx=True, query=state),
            _Selection(f"API, query naming the {state} branch", "unusable", api=True, query=state),
            _Selection(
                f"API, empty query with the {state} branch's cookie", "served", api=True, query="", cookie=state
            ),
            _Selection(f"API, cookie naming the {state} branch", "unusable", api=True, cookie=state),
            *(
                _Selection(
                    f"API {method}, header naming the {state} branch", "nbb-400", api=True, header=state, method=method
                )
                for method in _UNSAFE_METHODS
            ),
        )
    ),
)

_REFUSAL_STATUS = {"refused": 409, "unusable": 409, "nbb-400": 400, "csrf-403": 403}


class SelectorTableTest(TransactionTestCase):
    """The design's selector table: a change to a Server row, sent with each branch selection, CSRF enforced."""

    def test_each_selection_has_the_outcome_of_the_design(self):
        user = get_user_model().objects.create_superuser("selector-user")
        ready = _provisioned_branch(self, "selector ready")
        schema_ids = {"ready": ready.schema_id, "unknown": "unknown0", "": ""}
        for state, status in _UNREADY.items():
            unready = Branch(name=f"selector {state}")
            unready.save(provision=False)
            Branch.objects.filter(pk=unready.pk).update(status=status)
            schema_ids[state] = unready.schema_id

        for number, selection in enumerate(_SELECTIONS):
            with self.subTest(selection.name):
                server = _make_db_server(name=f"selector-{number}", ca_url=_BEFORE, dhcp6=False)
                client, token = _logged_in_client(user)
                if selection.cookie is not None:
                    client.cookies[COOKIE_NAME] = schema_ids[selection.cookie]
                headers = {"X-CSRFToken": token} if selection.csrf else {}
                if selection.header is not None:
                    headers[BRANCH_HEADER] = schema_ids[selection.header]
                if selection.htmx:
                    headers["HX-Request"] = "true"
                query = "" if selection.query is None else f"?{urlencode({QUERY_PARAM: schema_ids[selection.query]})}"

                with stub_kea({"version-get": _VERSION_OK}) as kea, _writes(ready) as writes:
                    if selection.api:
                        response = client.generic(
                            selection.method,
                            reverse("plugins-api:netbox_kea-api:server-detail", args=[server.pk]) + query,
                            data=json.dumps({"ca_url": _AFTER}),
                            content_type="application/json",
                            headers=headers,
                        )
                    else:
                        response = client.post(
                            reverse("plugins:netbox_kea:server_edit", args=[server.pk]) + query,
                            data={"name": server.name, "ca_url": _AFTER, "dhcp4": True, "ssl_verify": True},
                            headers=headers,
                        )
                server.refresh_from_db()

                if selection.outcome == "served":
                    self.assertEqual(response.status_code, 200 if selection.api else 302, response.content[:500])
                    self.assertEqual(server.ca_url, _AFTER)
                    self.assertIn("version-get", kea.commands())
                    continue
                self.assertEqual(response.status_code, _REFUSAL_STATUS[selection.outcome], response.content[:500])
                self.assertEqual(server.ca_url, _BEFORE)
                self.assertEqual(kea.commands(), [])
                self.assertEqual(writes, [])
                self._assert_the_refusal_says_why(selection, response, client, ready)

    def _assert_the_refusal_says_why(self, selection: _Selection, response, client: Client, ready: Branch) -> None:
        if selection.outcome == "nbb-400":
            reason = b"is not ready for use" if selection.header in _UNREADY else b"Invalid branch identifier"
            self.assertIn(reason, response.content)
            return
        if selection.outcome == "csrf-403":
            self.assertIn(b"CSRF", response.content)
            return
        refused = selection.outcome == "refused"
        text = f"Branch {ready.name} is active." if refused else "The selected branch is not usable"
        if selection.api:
            code = branching.BRANCH_WRITE_REFUSED if refused else branching.BRANCH_SELECTION_UNUSABLE
            self.assertEqual(response.json()["code"], code)
            self.assertIn(text, response.json()["detail"])
        elif selection.htmx:
            self.assertEqual(response.content, b"")
            if refused:
                self.assertEqual(response.headers["HX-Refresh"], "true")
                page = client.get(reverse("plugins:netbox_kea:server_list"))
            else:
                self.assertEqual(response.headers["HX-Redirect"], branching.main_url())
                page = client.get(response.headers["HX-Redirect"])
            self.assertContains(page, text)
        else:
            self.assertContains(response, text, status_code=409)
            self.assertContains(response, f'href="{branching.main_url()}"', status_code=409)


class _BranchReadTestCase(TransactionTestCase):
    """A superuser, a Server and an IP address in main, then a provisioned branch."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("branch-reader")
        self.client.force_login(self.user)
        self.server = _make_db_server(name="read-in-branch", dhcp6=False)
        self.ip = IPAddress.objects.create(address="192.0.2.10/24", dns_name="host.example.com")
        self.branch = _provisioned_branch(self, "reads")


class SourcesHeaderTest(_BranchReadTestCase):
    """In a branch, plugin and GraphQL responses say where the plugin's data comes from; other responses do not."""

    def setUp(self):
        super().setUp()
        self.sources = f"kea=live; plugin=main; dhcp-import-mappings=branch; branch={self.branch.schema_id}"

    def test_a_plugin_page_carries_the_sources_header(self):
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(reverse("plugins:netbox_kea:server_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers[branching.SOURCES_HEADER], self.sources)

    def test_a_plugin_page_on_main_carries_no_sources_header(self):
        response = self.client.get(reverse("plugins:netbox_kea:server_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(branching.SOURCES_HEADER, response.headers)

    def test_a_rest_read_carries_the_sources_header(self):
        response = self.client.get(
            reverse("plugins-api:netbox_kea-api:server-list"), headers={BRANCH_HEADER: self.branch.schema_id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers[branching.SOURCES_HEADER], self.sources)
        self.assertEqual([row["name"] for row in response.json()["results"]], [self.server.name])

    def test_a_graphql_server_list_query_carries_the_sources_header(self):
        response = self.client.post(
            reverse("graphql"),
            data=json.dumps({"query": "query { server_list { name } }"}),
            content_type="application/json",
            headers={BRANCH_HEADER: self.branch.schema_id},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.headers[branching.SOURCES_HEADER], self.sources)
        self.assertEqual(response.json()["data"]["server_list"], [{"name": self.server.name}])

    def test_a_core_job_api_read_carries_no_sources_header(self):
        response = self.client.get(reverse("core-api:job-list"), headers={BRANCH_HEADER: self.branch.schema_id})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(branching.SOURCES_HEADER, response.headers)

    def test_an_old_branch_read_names_unavailable_mappings(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "' + self.branch.schema_name + '"."' + KeaDhcpLink._meta.db_table + '"')
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(reverse("plugins:netbox_kea:server_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers[branching.SOURCES_HEADER],
            f"kea=live; plugin=main; dhcp-import-mappings=unavailable; branch={self.branch.schema_id}",
        )


_BANNER_TEXT = "netbox-kea refuses live and import changes"


class BranchPageTest(_BranchReadTestCase):
    """In a branch, plugin pages show the banner, and the IPAddress panel offers no reservation add."""

    def test_a_plugin_page_in_a_branch_shows_the_banner(self):
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(reverse("plugins:netbox_kea:server_list"))

        self.assertContains(response, _BANNER_TEXT)
        self.assertContains(response, f"netbox-branching's routing for branch {self.branch.name}")
        self.assertContains(response, "DHCP Import Mappings follow the selected branch")
        self.assertNotContains(response, "DHCP plugin links come from main")

    def test_a_plugin_page_on_main_shows_no_banner(self):
        self.assertNotContains(self.client.get(reverse("plugins:netbox_kea:server_list")), _BANNER_TEXT)

    def test_the_ipaddress_panel_lists_mains_servers_without_reservation_links(self):
        page = reverse("ipam:ipaddress", args=[self.ip.pk])
        add = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

        on_main = self.client.get(page)
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id
        in_branch = self.client.get(page)

        self.assertContains(on_main, add)
        self.assertNotContains(on_main, "Kea servers (main)")
        self.assertContains(in_branch, "Kea servers (main)")
        self.assertContains(in_branch, self.server.name)
        self.assertNotContains(in_branch, add)
        self.assertNotContains(in_branch, _BANNER_TEXT, msg_prefix="the banner is for plugin pages only")


@override_settings(ROOT_URLCONF="netbox_kea.tests.branch_refusal_urls")
class BranchActiveResponseTest(TransactionTestCase):
    """The middleware renders BranchActive, which plugin code raises at a change sink, as the 409 of the contract."""

    def setUp(self):
        from netbox_kea.tests.branch_refusal_urls import API_REFUSE_PATH, OUTSIDE_REFUSE_PATH, REFUSE_PATH

        self.user = get_user_model().objects.create_superuser("sink-user")
        self.client.force_login(self.user)
        self.branch = _provisioned_branch(self, "sink")
        self.ui, self.api, self.outside = f"/{REFUSE_PATH}", f"/{API_REFUSE_PATH}", f"/{OUTSIDE_REFUSE_PATH}"
        self.text = f"Branch {self.branch.name} is active."

    def test_on_main_the_sink_does_not_refuse(self):
        self.assertContains(self.client.get(self.ui), "not refused")

    def test_a_ui_request_gets_the_409_page(self):
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(self.ui)

        self.assertContains(response, self.text, status_code=409)
        self.assertContains(response, f'href="{branching.main_url()}"', status_code=409)

    def test_a_view_outside_the_plugin_gets_the_409_page_too(self):
        self.assertFalse(branching.plugin_owned(resolve(self.outside).func))
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(self.outside)

        self.assertContains(response, self.text, status_code=409)

    def test_a_rest_view_outside_the_plugin_gets_netbox_branchings_400_for_an_unready_branch_header(self):
        from netbox_kea.tests.branch_refusal_urls import API_OUTSIDE_SAVE_PATH

        # netbox-branching 1.2.1 lets the view run, with its own 400 response as the active branch.
        server = _make_db_server(name="outside", ca_url=_BEFORE, dhcp6=False)
        merged = Branch(name="sink merged")
        merged.save(provision=False)
        Branch.objects.filter(pk=merged.pk).update(status=BranchStatusChoices.MERGED)
        url = f"/{API_OUTSIDE_SAVE_PATH}{server.pk}/"
        self.assertFalse(branching.plugin_owned(resolve(url).func))

        with _writes(self.branch) as writes:
            response = self.client.post(url, headers={BRANCH_HEADER: merged.schema_id})

        self.assertEqual(response.status_code, 400, response.content[:500])
        self.assertIn(b"is not ready for use", response.content)
        self.assertEqual(writes, [])
        self.assertEqual(Server.objects.get(pk=server.pk).last_updated, server.last_updated)

    def test_a_rest_request_gets_the_409_code(self):
        response = self.client.get(self.api, headers={BRANCH_HEADER: self.branch.schema_id})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], branching.BRANCH_WRITE_REFUSED)

    def test_an_htmx_request_reloads_and_shows_the_message(self):
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(self.ui, headers={"HX-Request": "true"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["HX-Refresh"], "true")
        self.assertContains(self.client.get(reverse("plugins:netbox_kea:server_list")), self.text)


def _save_with(instance: models.Model, **fields: object) -> None:
    for name, value in fields.items():
        setattr(instance, name, value)
    instance.save()


class DhcpTargetBranchLifecycleTest(TransactionTestCase):
    """DHCP target deletes remove branch mappings and preserve main until merge."""

    def setUp(self):
        if not apps.is_installed("netbox_dhcp"):
            if _required:
                self.fail("The branching CI job requires netbox_dhcp for target deletion tests.")
            self.skipTest("netbox_dhcp is not installed")
        self.user = get_user_model().objects.create_superuser("dhcp-target-admin")
        self.server = _make_db_server(name="dhcp-target-links")
        self.targets = linked_dhcp_targets(self.server)
        self.branch = _provisioned_branch(self, "dhcp target delete")
        self.branch.merge_strategy = "squash"
        self.branch.save(provision=False)

    def _delete_and_merge(self, *, queryset):
        link_pks = {link.pk for _, link in self.targets}
        branch_link_pks = link_pks.copy()
        with activate_branch(self.branch), event_tracking(_change_request(self.user)):
            for target, link in self.targets:
                with self.subTest(model=target._meta.label, object_id=target.pk):
                    self.assertTrue(supports_branching(type(target)))
                    if queryset:
                        type(target).objects.filter(pk=target.pk).delete()
                    else:
                        type(target).objects.get(pk=target.pk).delete()
                    self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
                    branch_link_pks.remove(link.pk)
                    self.assertSetEqual(set(KeaDhcpLink.objects.values_list("pk", flat=True)), branch_link_pks)
        for target, _ in self.targets:
            self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
        self.assertSetEqual(set(KeaDhcpLink.objects.values_list("pk", flat=True)), link_pks)

        self.branch.merge(user=self.user)

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatusChoices.MERGED)
        for target, link in self.targets:
            self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
            self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())

    def test_target_instance_delete_removes_only_the_branch_mapping(self):
        self._delete_and_merge(queryset=False)

    def test_target_queryset_delete_removes_only_the_branch_mapping(self):
        self._delete_and_merge(queryset=True)


class PluginRowWritesInBranchTest(TransactionTestCase):
    """Server, settings and ownership writes remain main-only; mappings follow the branch."""

    def setUp(self):
        self.server = _make_db_server(name="rows", ca_url=_BEFORE)
        self.link = KeaDhcpLink.objects.create(
            server=self.server,
            family=4,
            kea_subnet_id=7,
            object_type=ContentType.objects.get_for_model(VRF),
            object_id=1,
        )
        self.config = SyncConfig.get()
        self.branch = _provisioned_branch(self, "rows")

    def _main(self) -> tuple:
        return (
            list(Server.objects.values_list("pk", "ca_url")),
            list(KeaDhcpLink.objects.values_list("pk", "kea_subnet_id")),
            list(SyncConfig.objects.values_list("pk", "interval_minutes")),
        )

    def test_the_receivers_cover_every_main_only_plugin_model(self):
        labels = {model._meta.label for model in _plugin_models()}

        self.assertEqual(
            labels,
            {"netbox_kea.Server", "netbox_kea.SyncConfig", "netbox_kea.KeaDhcpLink", "netbox_kea.IPAMOwnershipLink"},
        )
        main_only = {"netbox_kea.Server", "netbox_kea.SyncConfig", "netbox_kea.IPAMOwnershipLink"}
        self.assertEqual((_refusal_receivers(pre_save), _refusal_receivers(pre_delete)), (main_only, main_only))

    def test_a_save_in_a_branch_is_refused(self):
        before = self._main()
        changes = {
            "server": lambda: _save_with(self.server, ca_url=_AFTER),
            "sync config": lambda: _save_with(self.config, interval_minutes=9),
            "new server": lambda: _make_db_server(name="new in branch"),
        }
        for label, change in changes.items():
            with self.subTest(label):
                with activate_branch(self.branch), self.assertRaises(branching.BranchActive) as refused:
                    change()
                self.assertIs(refused.exception.branch, self.branch)

        self.assertEqual(self._main(), before)

    def test_a_delete_in_a_branch_is_refused(self):
        before = self._main()
        deletes = {
            "server": lambda: Server.objects.get(pk=self.server.pk).delete(),
            "server queryset": lambda: Server.objects.all().delete(),
            "sync config queryset": lambda: SyncConfig.objects.all().delete(),
        }
        for label, delete in deletes.items():
            with self.subTest(label), activate_branch(self.branch), self.assertRaises(branching.BranchActive):
                delete()

        self.assertEqual(self._main(), before)

    def test_mapping_save_and_delete_change_only_branch_rows(self):
        before = self._main()
        with activate_branch(self.branch):
            link = KeaDhcpLink.objects.get(pk=self.link.pk)
            link.kea_subnet_id = 8
            link.save()
            self.assertEqual(KeaDhcpLink.objects.get(pk=link.pk).kea_subnet_id, 8)
            KeaDhcpLink.objects.filter(pk=link.pk).delete()
            self.assertFalse(KeaDhcpLink.objects.filter(pk=link.pk).exists())

        self.assertEqual(self._main(), before)

    def test_a_server_delete_in_a_branch_keeps_its_jobs(self):
        job = Job.objects.create(
            object_type=ContentType.objects.get_for_model(Server),
            object_id=self.server.pk,
            name="kept",
            job_id=uuid.uuid4(),
        )

        with activate_branch(self.branch), self.assertRaises(branching.BranchActive):
            Server.objects.get(pk=self.server.pk).delete()

        self.assertTrue(Job.objects.filter(pk=job.pk).exists(), "the refused delete removed the Server's job")

    def test_deletes_are_refused_when_main_created_the_owned_object_after_provisioning(self):
        objects = (
            IPAddress.objects.create(address="198.18.0.10/24"),
            Prefix.objects.create(prefix="198.18.0.0/24"),
            IPRange.objects.create(
                start_address=IPNetwork("198.18.0.100/24"), end_address=IPNetwork("198.18.0.199/24")
            ),
        )
        links = [_link(self.server, obj) for obj in objects]
        before = _ipam_state()
        for obj, link in zip(objects, links, strict=True):
            with self.subTest(model=obj._meta.label), activate_branch(self.branch):
                self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())
                with self.assertRaises(branching.BranchActive) as refused:
                    IPAMOwnershipLink.objects.filter(pk=link.pk).delete()
                self.assertIs(refused.exception.branch, self.branch)
                self.assertIn(f"A delete of {link._meta.label} {link.pk}", str(refused.exception))

        with activate_branch(self.branch), self.assertRaises(branching.BranchActive):
            Server.objects.filter(pk=self.server.pk).delete()

        self.assertEqual(_ipam_state(), before)

    def test_on_main_a_save_and_a_delete_are_not_refused(self):
        self.server.ca_url = _AFTER
        self.server.save()
        self.link.delete()

        self.assertEqual(Server.objects.get(pk=self.server.pk).ca_url, _AFTER)
        self.assertFalse(KeaDhcpLink.objects.filter(pk=self.link.pk).exists())


_OWNED_KEY = {IPAddress: "ip_address", Prefix: "prefix", IPRange: "ip_range"}
_OWNED_SOURCE = {IPAddress: "lease", Prefix: "subnet", IPRange: "pool"}
_EVENTS_RECORDER = "netbox_kea.tests.utils.record_dispatched_events"


def _link(server: Server, obj: models.Model) -> IPAMOwnershipLink:
    """Link *obj* to *server* in main, as the IPAM sync would."""
    return IPAMOwnershipLink.objects.create(
        server=server,
        family=4,
        source=_OWNED_SOURCE[type(obj)],
        confirmation=next_confirmation_number(),
        **{_OWNED_KEY[type(obj)]: obj},
    )


def _ipam_state() -> tuple:
    """Return the IPAM rows, the device rows and the ownership links that the active connection reads."""
    return (
        list(IPAddress.objects.order_by("pk").values_list("pk", "address", "description")),
        list(Prefix.objects.order_by("pk").values_list("pk", "prefix", "description")),
        list(IPRange.objects.order_by("pk").values_list("pk", "start_address", "end_address", "description")),
        list(Device.objects.order_by("pk").values_list("pk", "name")),
        list(Interface.objects.order_by("pk").values_list("pk", "name")),
        list(IPAMOwnershipLink.objects.order_by("pk").values_list("pk", *_OWNED_KEY.values(), "confirmation")),
    )


def _device_with_interface(name: str) -> Interface:
    site = Site.objects.create(name=name, slug=name)
    manufacturer = Manufacturer.objects.create(name=name, slug=name)
    device_type = DeviceType.objects.create(manufacturer=manufacturer, model=name, slug=name)
    role = DeviceRole.objects.create(name=name, slug=name)
    device = Device.objects.create(name=name, site=site, device_type=device_type, role=role)
    return Interface.objects.create(device=device, name="eth0", type="1000base-t")


class _OwnedObjectsTestCase(TransactionTestCase):
    """Main holds an unowned IP address, and an IP address on a device interface, a Prefix and an IP Range that a
    Kea Server owns. Then a provisioned branch."""

    branch_name = "ownership"

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("owner-admin")
        self.client = Client(raise_request_exception=False)
        self.client.force_login(self.user)
        self.server = _make_db_server(name="owner", dhcp6=False)
        self.interface = _device_with_interface("owner-device")
        self.unowned = IPAddress.objects.create(address="192.0.2.5/24")
        self.owned = {
            "ipaddress": IPAddress.objects.create(address="192.0.2.10/24", assigned_object=self.interface),
            "prefix": Prefix.objects.create(prefix="192.0.2.0/24"),
            "iprange": IPRange.objects.create(
                start_address=IPNetwork("192.0.2.100/24"), end_address=IPNetwork("192.0.2.199/24")
            ),
        }
        for obj in self.owned.values():
            _link(self.server, obj)
        self.branch = _provisioned_branch(self, self.branch_name)
        self.main_before = _ipam_state()
        with activate_branch(self.branch):
            self.branch_before = _ipam_state()
        DISPATCHED_EVENTS.clear()

    def _in_branch(self) -> None:
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

    def _refusal(self, obj: models.Model) -> str:
        return escape(
            f"A delete of {obj._meta.verbose_name} {obj}, which Kea Server {self.server} owns, is refused. "
            f"Branch {self.branch} is active."
        )

    def _assert_main_unchanged(self) -> None:
        self.assertEqual(_ipam_state(), self.main_before, "main changed")

    def _assert_nothing_changed(self) -> None:
        self._assert_main_unchanged()
        with activate_branch(self.branch):
            self.assertEqual(_ipam_state(), self.branch_before, "the branch changed")

    def _bulk_delete(self, model: type[models.Model], *objects: models.Model):
        return self.client.post(
            reverse(f"ipam:{model._meta.model_name}_bulk_delete"),
            {"pk": [obj.pk for obj in objects], "_confirm": True, "confirm": True},
            follow=True,
        )


@override_settings(EVENTS_PIPELINE=[_EVENTS_RECORDER])
class OwnedObjectDeleteInBranchTest(_OwnedObjectsTestCase):
    """A delete in a branch that reaches an ownership link is refused, and main and the branch do not change."""

    def test_the_fixtures_link_each_owned_object_in_main(self):
        self.assertEqual(IPAMOwnershipLink.objects.count(), 3)
        self.assertIn(IPAMOwnershipLink._meta.label, _refusal_receivers(pre_delete))

    def test_a_ui_delete_of_an_owned_object_is_refused(self):
        self._in_branch()
        for name, obj in self.owned.items():
            with self.subTest(name):
                response = self.client.post(
                    reverse(f"ipam:{name}_delete", args=[obj.pk]), {"confirm": True}, follow=True
                )

                self.assertEqual(response.redirect_chain, [(obj.get_absolute_url(), 302)])
                self.assertContains(response, self._refusal(obj))
        self._assert_nothing_changed()
        self.assertEqual(DISPATCHED_EVENTS, [])

    def test_a_ui_bulk_delete_of_an_owned_object_is_refused(self):
        self._in_branch()
        for obj in self.owned.values():
            with self.subTest(type(obj).__name__):
                response = self._bulk_delete(type(obj), obj)

                self.assertContains(response, self._refusal(obj))
        self._assert_nothing_changed()
        self.assertEqual(DISPATCHED_EVENTS, [])

    def test_a_rest_delete_of_an_owned_object_is_refused(self):
        for name, obj in self.owned.items():
            with self.subTest(name):
                response = self.client.delete(
                    reverse(f"ipam-api:{name}-detail", args=[obj.pk]), headers={BRANCH_HEADER: self.branch.schema_id}
                )

                self.assertEqual(response.status_code, 400, response.content[:500])
                self.assertIn(self._refusal(obj), response.json()["detail"])
        self._assert_nothing_changed()

    def test_a_queryset_delete_of_an_owned_object_is_refused(self):
        for name, obj in self.owned.items():
            with self.subTest(name), activate_branch(self.branch), self.assertRaises(AbortRequest) as refused:
                type(obj).objects.filter(pk=obj.pk).delete()

            self.assertIsInstance(refused.exception, branching.BranchActive)
            self.assertIs(refused.exception.branch, self.branch)
        self._assert_nothing_changed()

    def test_a_device_delete_that_reaches_an_owned_ip_address_is_refused(self):
        device = self.interface.device
        with activate_branch(self.branch), self.assertRaises(branching.BranchActive):
            Device.objects.get(pk=device.pk).delete()
        self._in_branch()

        response = self.client.post(reverse("dcim:device_delete", args=[device.pk]), {"confirm": True}, follow=True)

        self.assertContains(response, self._refusal(self.owned["ipaddress"]))
        self._assert_nothing_changed()
        self.assertEqual(DISPATCHED_EVENTS, [])

    def test_a_mixed_ui_bulk_delete_is_refused_and_dispatches_no_event(self):
        owned = self.owned["ipaddress"]
        self._in_branch()
        order = list(IPAddress.objects.filter(pk__in=[owned.pk, self.unowned.pk]).values_list("pk", flat=True))
        self.assertEqual(order, [self.unowned.pk, owned.pk], "the bulk delete must reach the unowned row first")

        response = self._bulk_delete(IPAddress, self.unowned, owned)

        self.assertContains(response, self._refusal(owned))
        self._assert_nothing_changed()
        self.assertEqual(DISPATCHED_EVENTS, [])
        # The recorder sees a delete that the branch allows, so the empty list above means something.
        self._bulk_delete(IPAddress, self.unowned)
        self.assertEqual(
            [(event["object_id"], event["event_type"]) for event in DISPATCHED_EVENTS],
            [(self.unowned.pk, OBJECT_DELETED)],
        )
        self._assert_main_unchanged()

    def test_a_rest_bulk_delete_leaves_main_unchanged_whatever_the_status(self):
        # netbox-branching's REST bulk delete rolls back the wrong connection (R2-5): only main is asserted.
        body = json.dumps([{"id": self.unowned.pk}, {"id": self.owned["ipaddress"].pk}])
        for enclosing in (False, True):
            with self.subTest(enclosing_default_atomic=enclosing):
                with ExitStack() as stack:
                    if enclosing:
                        stack.enter_context(transaction.atomic())
                    self.client.delete(
                        reverse("ipam-api:ipaddress-list"),
                        data=body,
                        content_type="application/json",
                        headers={BRANCH_HEADER: self.branch.schema_id},
                    )

                self._assert_main_unchanged()


class BranchNameEscapeTest(_OwnedObjectsTestCase):
    """NetBox marks an AbortRequest message safe, so the refusal escapes the free-text branch name."""

    branch_name = "<b>probe</b>"

    def _assert_the_name_is_text(self, response) -> None:
        self.assertContains(response, "Branch &lt;b&gt;probe&lt;/b&gt; is active.")
        self.assertNotContains(response, "<b>probe</b>")

    def test_a_refused_ui_delete_shows_the_branch_name_as_text(self):
        obj = self.owned["ipaddress"]
        self._in_branch()

        response = self.client.post(reverse("ipam:ipaddress_delete", args=[obj.pk]), {"confirm": True}, follow=True)

        self.assertEqual(response.redirect_chain, [(obj.get_absolute_url(), 302)])
        self._assert_the_name_is_text(response)
        self._assert_nothing_changed()

    def test_a_refused_ui_bulk_delete_shows_the_branch_name_as_text(self):
        self._in_branch()

        response = self._bulk_delete(IPAddress, self.owned["ipaddress"])

        self._assert_the_name_is_text(response)
        self._assert_nothing_changed()


def _change_request(user) -> Any:
    """Return a request that records ObjectChanges, as a UI change would, so netbox-branching can replay them."""
    request: Any = RequestFactory().get("/")
    request.user, request.id = user, uuid.uuid4()
    return request


class OwnershipBranchLifecycleTest(_OwnedObjectsTestCase):
    """Links stay in main through sync, discard, merge and revert, and CASCADE removes them only on main."""

    def _status(self) -> str:
        self.branch.refresh_from_db()
        return self.branch.status

    def _links_of(self, obj: models.Model) -> list[int]:
        return list(IPAMOwnershipLink.objects.filter(**{_OWNED_KEY[type(obj)]: obj.pk}).values_list("pk", flat=True))

    def test_the_link_table_is_in_main_only(self):
        table = IPAMOwnershipLink._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_schema()")
            (main_schema,) = cursor.fetchone()
            cursor.execute("SELECT table_schema FROM information_schema.tables WHERE table_name = %s", [table])
            schemas = [row[0] for row in cursor.fetchall()]

        self.assertEqual(schemas, [main_schema])
        self.assertNotEqual(main_schema, self.branch.schema_name)

    def test_a_sync_after_main_deleted_an_owned_object_deletes_it_in_the_branch(self):
        obj = self.owned["ipaddress"]
        with event_tracking(_change_request(self.user)):
            IPAddress.objects.get(pk=obj.pk).delete()
        self.assertEqual(self._links_of(obj), [], "main's CASCADE left the link")

        self.branch.sync(user=self.user)

        self.assertEqual(self._status(), BranchStatusChoices.READY)
        with activate_branch(self.branch):
            self.assertFalse(IPAddress.objects.filter(pk=obj.pk).exists())

    def test_a_discard_leaves_mains_objects_and_links_unchanged(self):
        with activate_branch(self.branch), event_tracking(_change_request(self.user)):
            _save_with(IPAddress.objects.get(pk=self.owned["ipaddress"].pk), description="edited in the branch")
            IPAddress.objects.get(pk=self.unowned.pk).delete()

        self.branch.delete()

        self.assertFalse(Branch.objects.filter(pk=self.branch.pk).exists())
        self._assert_main_unchanged()

    def test_an_unowned_delete_that_is_merged_and_reverted_returns_the_object(self):
        with activate_branch(self.branch), event_tracking(_change_request(self.user)):
            IPAddress.objects.get(pk=self.unowned.pk).delete()

        self.branch.merge(user=self.user)
        merged = IPAddress.objects.filter(pk=self.unowned.pk).exists()
        self.branch.revert(user=self.user)

        self.assertFalse(merged, "the merge did not delete the object in main")
        self.assertEqual(self._status(), BranchStatusChoices.READY)
        self._assert_main_unchanged()

    def test_a_merge_deletes_an_object_that_main_linked_after_the_branch_deleted_it(self):
        with activate_branch(self.branch), event_tracking(_change_request(self.user)):
            IPAddress.objects.get(pk=self.unowned.pk).delete()
        link = _link(self.server, self.unowned)

        self.branch.merge(user=self.user)

        self.assertEqual(self._status(), BranchStatusChoices.MERGED)
        self.assertFalse(IPAddress.objects.filter(pk=self.unowned.pk).exists())
        self.assertFalse(IPAMOwnershipLink.objects.filter(pk=link.pk).exists())

    def test_a_revert_deletes_an_object_that_main_linked_after_the_merge(self):
        with activate_branch(self.branch), event_tracking(_change_request(self.user)):
            created = IPAddress.objects.create(address="192.0.2.50/24")
        self.branch.merge(user=self.user)
        self.assertTrue(IPAddress.objects.filter(pk=created.pk).exists(), "the merge did not create the object")
        link = _link(self.server, IPAddress.objects.get(pk=created.pk))

        self.branch.revert(user=self.user)

        self.assertEqual(self._status(), BranchStatusChoices.READY)
        self.assertFalse(IPAddress.objects.filter(pk=created.pk).exists())
        self.assertFalse(IPAMOwnershipLink.objects.filter(pk=link.pk).exists())


class SyncJobInBranchTest(TransactionTestCase):
    """KeaIpamSyncJob runs on main only: in a branch it fails before any read."""

    def test_the_sync_job_fails_in_a_branch_before_any_query_or_kea_command(self):
        _make_db_server(name="job")
        branch = _provisioned_branch(self, "job")
        job = Job.objects.create(name="Kea IPAM Sync", job_id=uuid.uuid4())

        with activate_branch(branch):
            alias = router.db_for_write(VRF)
            with (
                stub_kea({}) as kea,
                CaptureQueriesContext(connections["default"]) as on_main,
                CaptureQueriesContext(connections[alias]) as on_branch,
                self.assertRaises(JobFailed),
            ):
                KeaIpamSyncJob(job).run()

        self.assertEqual(kea.commands(), [])
        self.assertEqual([query["sql"] for query in (*on_main.captured_queries, *on_branch.captured_queries)], [])
        self.assertIn(branch.name, job.error)

    def test_the_real_runner_refuses_a_branch_without_actor_or_sync_side_effects(self):
        from core.models import ObjectChange

        _make_db_server(name="job")
        branch = _provisioned_branch(self, "job-runner")
        job = Job.objects.create(name=KeaIpamSyncJob.name, job_id=uuid.uuid4())
        changes_before = ObjectChange.objects.count()

        with activate_branch(branch), stub_kea({}) as kea, CaptureQueriesContext(connections["default"]) as queries:
            KeaIpamSyncJob.handle(job)

        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn(branch.name, job.error)
        self.assertEqual(kea.commands(), [])
        self.assertFalse(get_user_model().objects.filter(username__iexact="netbox-kea-sync").exists())
        self.assertEqual(ObjectChange.objects.count(), changes_before)
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPRange.objects.exists())
        sync_tables = ("netbox_kea_server", "netbox_kea_syncconfig", "ipam_ipaddress", "ipam_prefix", "ipam_iprange")
        self.assertFalse(any(table in query["sql"] for table in sync_tables for query in queries.captured_queries))


# Guard 3 in a provisioned branch: the Kea transport.

_KEA_WRITES = [member for member in KeaCommand if member.is_write]


class KeaTransportInBranchTest(TransactionTestCase):
    """A mutating Kea command in a branch raises BranchActive before any send, also from a clone in a thread."""

    def setUp(self):
        self.server = _make_db_server(name="transport")
        self.branch = _provisioned_branch(self, "transport")

    def test_a_client_built_in_a_branch_refuses_every_write_also_from_a_clone_in_a_thread(self):
        with activate_branch(self.branch):
            client = Server.objects.get(pk=self.server.pk).get_client(version=4)

        # Called on main: the binding refuses, not the context, as in a thread-pool worker.
        for command in _KEA_WRITES:
            with self.subTest(command=command.value), stub_kea({}) as kea, ThreadPoolExecutor(1) as pool:
                with self.assertRaises(branching.BranchActive) as refused:
                    client.command(command, 4)
                in_thread = pool.submit(client.clone().command, command, 4).exception()

                self.assertIs(refused.exception.branch, self.branch)
                self.assertIsInstance(in_thread, branching.BranchActive)
                self.assertIs(in_thread.branch, self.branch)
                self.assertEqual(kea.commands(), [])

    def test_a_client_built_on_main_refuses_a_write_while_a_branch_is_active(self):
        client = self.server.get_client(version=4)

        with stub_kea({"lease4-del": {"result": 0}}) as kea:
            with activate_branch(self.branch), self.assertRaises(branching.BranchActive):
                client.command(KeaCommand.LEASE4_DEL, 4, arguments={"ip-address": "192.0.2.1"})
            self.assertEqual(kea.commands(), [])
            client.command(KeaCommand.LEASE4_DEL, 4, arguments={"ip-address": "192.0.2.1"})

        self.assertEqual(kea.commands(), ["lease4-del"], "on main the write is sent")

    def test_a_config_mutation_in_a_branch_leaves_the_cache_generation_alone(self):
        generation_key = server_configuration._generation_key(self.server, 4)
        generation = server_configuration._cache_generation(self.server, 4)

        with activate_branch(self.branch), stub_kea({}) as kea, self.assertRaises(branching.BranchActive):
            self.server.get_client(version=4).subnet_del(4, 1)

        self.assertEqual(cache.get(generation_key), generation)
        self.assertEqual(kea.commands(), [])

    def test_a_client_built_in_a_branch_sends_a_read(self):
        with activate_branch(self.branch), stub_kea({"config-get": {"result": 0}}) as kea:
            self.server.get_client(version=4).command(KeaCommand.CONFIG_GET, 4)

        self.assertEqual(kea.commands(), ["config-get"])


# Guard 1: the URL tree.

_RECORDINGS = Path(__file__).with_name("kea_recordings")


@dataclass(frozen=True)
class _KeaObjects:
    """The Kea objects of one family that the routes name: each exists in the recorded configuration."""

    subnet_id: int
    pool: str
    network_name: str
    option_code: int
    option_space: str
    lease: dict
    reservation: dict


_KEA_OBJECTS = {
    4: _KeaObjects(
        subnet_id=10,
        pool="192.0.2.10-192.0.2.20",
        network_name="office",
        option_code=224,
        option_space="dhcp4",
        lease={
            "ip-address": "192.0.2.15",
            "hw-address": "aa:bb:cc:dd:ee:01",
            "subnet-id": 10,
            "hostname": "lease4.example.com",
            "cltt": 1_700_000_000,
            "valid-lft": 4000,
            "state": 0,
        },
        reservation={"subnet-id": 10, "hw-address": "aa:bb:cc:dd:ee:02", "ip-address": "192.0.2.16", "hostname": "r4"},
    ),
    6: _KeaObjects(
        subnet_id=10,
        pool="2001:db8:1::10-2001:db8:1::ff",
        network_name="office",
        option_code=1000,
        option_space="dhcp6",
        lease={
            "ip-address": "2001:db8:1::15",
            "duid": "00:01:02:03:04:05",
            "iaid": 1,
            "type": "IA_NA",
            "prefix-len": 128,
            "subnet-id": 10,
            "hostname": "lease6.example.com",
            "cltt": 1_700_000_000,
            "valid-lft": 4000,
            "preferred-lft": 3000,
            "state": 0,
        },
        reservation={
            "subnet-id": 10,
            "duid": "00:01:02:03:04:06",
            "ip-addresses": ["2001:db8:1::16"],
            "hostname": "r6",
        },
    ),
}


def _family(body: dict) -> int:
    (service,) = body.get("service") or ["dhcp4"]
    return 6 if service == "dhcp6" else 4


def _recorded_kea() -> dict:
    """Return stub_kea responses for the reads that plugin pages send: the recorded configuration, one lease, one
    reservation. Every command it answers is a read, so a page that sends any other command fails the guard.
    """
    recorded = {family: json.loads((_RECORDINGS / f"dhcp{family}.json").read_text()) for family in (4, 6)}

    def config_get(body):
        return recorded[_family(body)]["config-get"]

    def list_commands(body):
        return recorded[_family(body)]["list-commands"]

    def reservation_page(body):
        return _res_page([_KEA_OBJECTS[_family(body)].reservation])

    def reservation_get(body):
        return _res_get(_KEA_OBJECTS[_family(body)].reservation)

    responses: dict = {
        "config-get": config_get,
        "list-commands": list_commands,
        "status-get": {"result": 0, "arguments": {"pid": 1, "uptime": 3600, "reload": 0}},
        "version-get": {"result": 0, "text": "3.2.0", "arguments": {"extended": "3.2.0"}},
        "reservation-get-page": reservation_page,
        "reservation-get": reservation_get,
    }
    for family in (4, 6):
        objects, replies = _KEA_OBJECTS[family], recorded[family]

        def subnet_get(body, family=family):
            configuration = recorded[family]["config-get"]["arguments"][f"Dhcp{family}"]
            key = f"subnet{family}"
            subnets = [*configuration[key], *(s for n in configuration["shared-networks"] for s in n[key])]
            found = [s for s in subnets if s["id"] == body["arguments"]["id"]]
            return {"result": 0, "arguments": {key: found}} if found else {"result": 3}

        def network_get(body, family=family, replies=replies):
            return replies[f"network{family}-get"]["present" if body["arguments"]["name"] == "office" else "absent"]

        def lease_page(body, objects=objects):
            # Kea returns the leases after the "from" address.
            after = ip_address(body["arguments"]["from"]) < ip_address(objects.lease["ip-address"])
            return {"result": 0, "arguments": {"leases": [objects.lease], "count": 1}} if after else {"result": 3}

        def lease_get(body, objects=objects):
            found = body["arguments"].get("ip-address") == objects.lease["ip-address"]
            return {"result": 0, "arguments": objects.lease} if found else {"result": 3}

        responses |= {
            f"subnet{family}-list": replies[f"subnet{family}-list"],
            f"subnet{family}-get": subnet_get,
            f"network{family}-get": network_get,
            f"lease{family}-get-page": lease_page,
            f"lease{family}-get": lease_get,
            f"lease{family}-get-by-state": _leases_per_subnet({objects.subnet_id: [objects.lease]}),
            f"stat-lease{family}-get": _subnet_stats(family, objects.subnet_id),
        }
    return responses


@dataclass(frozen=True)
class _Route:
    """One URL pattern whose callback is defined inside netbox_kea."""

    name: str
    pattern: str
    parameters: tuple[str, ...]
    callback: Any

    @property
    def api(self) -> bool:
        return hasattr(self.callback, "actions")


def _plugin_routes(resolver: URLResolver | None = None, name: str = "", pattern: str = "", parameters=()) -> list:
    """Walk the whole URL tree and return each pattern whose callback module is inside netbox_kea."""
    routes = []
    for entry in (resolver or get_resolver()).url_patterns:
        text = pattern + str(entry.pattern)
        found = (*parameters, *entry.pattern.regex.groupindex)
        if isinstance(entry, URLResolver):
            namespace = f"{name}{entry.namespace}:" if entry.namespace else name
            routes += _plugin_routes(entry, namespace, text, found)
        elif isinstance(entry, URLPattern) and branching.plugin_owned(entry.callback):
            routes.append(_Route(f"{name}{entry.name}", text, found, entry.callback))
    return routes


_PARAMETER = re.compile(r"<(?:\w+:)?(\w+)>|\(\?P<(\w+)>[^)]*\)")
_FAMILY = re.compile(r"[a-z]([46])(?:_|$)")


def _route_arguments(route: _Route, server: Server, ip: IPAddress) -> dict[str, object]:
    """Return a value for each parameter of *route* that names a real object, or fail and name the route."""
    family = _FAMILY.search(route.name.rsplit(":", 1)[-1])
    objects = _KEA_OBJECTS[int(family.group(1))] if family else None
    by_family = {
        "subnet_id": lambda o: o.subnet_id,
        "pool": lambda o: o.pool,
        "network_name": lambda o: o.network_name,
        "code": lambda o: o.option_code,
        "space": lambda o: o.option_space,
        "ip_address": lambda o: o.lease["ip-address"],
    }
    arguments: dict[str, object] = {}
    for parameter in route.parameters:
        if parameter == "pk":
            arguments[parameter] = ip.pk if route.name.endswith(":ipaddress_kea_reservations") else server.pk
        elif parameter == "format":
            arguments[parameter] = "json"
        elif parameter in by_family and objects is not None:
            arguments[parameter] = by_family[parameter](objects)
        else:
            raise AssertionError(
                f"Guard 1 cannot build the argument {parameter!r} of {route.name} ({route.pattern}). "
                "Teach _route_arguments the object it names, so the guard checks this route."
            )
    return arguments


def _route_query(route: _Route, ip: IPAddress) -> str:
    """Return the query string that a page of *route* needs to show a real object."""
    short = route.name.rsplit(":", 1)[-1]
    if short.startswith(("server_reservation4_edit", "server_reservation4_delete")):
        return urlencode({"identifier_type": "hw-address", "identifier": _KEA_OBJECTS[4].reservation["hw-address"]})
    if short.startswith(("server_reservation6_edit", "server_reservation6_delete")):
        return urlencode({"identifier_type": "duid", "identifier": _KEA_OBJECTS[6].reservation["duid"]})
    if short == "reservation_check_ip":
        return urlencode({"ip": str(ip.address.ip)})
    if short in ("server-leases4", "server-leases6"):
        return urlencode({"ip_address": _KEA_OBJECTS[int(short[-1])].lease["ip-address"]})
    if short in ("server_leases4", "server_leases6"):
        return urlencode({"by": "ip", "q": _KEA_OBJECTS[int(short[-1])].lease["ip-address"]})
    if short in ("server-reservations4", "server-reservations6"):
        return urlencode({"limit": 100})
    return ""


def _view(callback) -> object:
    """Return the view class of a URL callback: two patterns with one path resolve to the first (#246)."""
    return getattr(callback, "view_class", None) or getattr(callback, "cls", None) or callback


def _route_url(route: _Route, server: Server, ip: IPAddress) -> str:
    """Fill the pattern itself, so a route that shares its name with another is checked too."""
    arguments = _route_arguments(route, server, ip)
    path = _PARAMETER.sub(lambda match: str(arguments[match.group(1) or match.group(2)]), route.pattern)
    url = "/" + path.replace("^", "").replace("$", "").replace("\\.", ".").replace("/?", "")
    match = resolve(url)
    if _view(match.func) is not _view(route.callback) or any(
        str(match.kwargs.get(k)) != str(v) for k, v in arguments.items()
    ):
        raise AssertionError(f"Guard 1 built {url} for {route.name} ({route.pattern}), but it resolves elsewhere.")
    query = _route_query(route, ip)
    return f"{url}?{query}" if query else url


# A page GET that main answers with a redirect: NetBox's bulk views, and the lease delete confirmation.
_MAIN_GET_REDIRECTS = frozenset(
    f"plugins:netbox_kea:{name}"
    for name in ("server_bulk_delete", "server_bulk_edit", "server_leases4_delete", "server_leases6_delete")
)


def _main_get_status(route: _Route) -> int:
    """Return the status that a GET of *route* gets on main, from the recorded Kea and real objects."""
    if route.name in _MAIN_GET_REDIRECTS:
        return 302
    if not route.api and not hasattr(_view(route.callback), "get"):
        return 405
    return 200


class UrlTreeBuilderTest(SimpleTestCase):
    """Guard 1 fails, and names the route, when it cannot build a route's arguments."""

    def _route(self, name: str, pattern: str, parameters: tuple[str, ...]) -> _Route:
        return _Route(f"plugins:netbox_kea:{name}", pattern, parameters, callback=None)

    def test_an_unknown_parameter_fails_by_name(self):
        route = self._route("server_widget", "servers/<int:pk>/widgets/<int:widget_id>/", ("pk", "widget_id"))

        with self.assertRaisesMessage(AssertionError, "'widget_id' of plugins:netbox_kea:server_widget"):
            _route_arguments(route, Server(pk=1), IPAddress(pk=2))

    def test_a_kea_parameter_on_a_route_without_a_family_fails_by_name(self):
        route = self._route("server_subnet_edit", "servers/<int:pk>/subnets/<int:subnet_id>/", ("pk", "subnet_id"))

        with self.assertRaisesMessage(AssertionError, "'subnet_id' of plugins:netbox_kea:server_subnet_edit"):
            _route_arguments(route, Server(pk=1), IPAddress(pk=2))

    def test_a_built_url_that_resolves_to_another_view_fails_by_name(self):
        status = next(route for route in _plugin_routes() if route.name.endswith(":server_status"))
        # The Server list pattern, with the status view's callback: the URL reaches the list view.
        route = _Route(status.name, "plugins/kea/servers/", (), status.callback)

        with self.assertRaisesMessage(AssertionError, "for plugins:netbox_kea:server_status"):
            _route_url(route, Server(pk=1), IPAddress(pk=2))


class UrlTreeGuardTest(TransactionTestCase):
    """Guard 1: in a provisioned branch, every plugin URL refuses a change and serves a read as main does."""

    def test_every_plugin_route_in_a_branch(self):
        user = get_user_model().objects.create_superuser("url-tree")
        server = _make_db_server(name="url-tree", dhcp4=True, dhcp6=True, has_control_agent=True)
        ip = IPAddress.objects.create(address="192.0.2.16/24", dns_name="r4.example.com")
        ip.refresh_from_db()
        branch = _provisioned_branch(self, "url tree")
        routes = _plugin_routes()
        self.assertTrue(any(route.api for route in routes) and not all(route.api for route in routes), routes)

        for route in routes:
            url = _route_url(route, server, ip)
            for method in SAFE_METHODS:
                with self.subTest(route=route.name, url=url, method=method):
                    self._assert_served_as_on_main(route, url, method, user, branch)
            # Every unsafe method, also one that the view or the REST action does not accept.
            for method in _UNSAFE_METHODS:
                with self.subTest(route=route.name, url=url, method=method):
                    self._assert_refused(route, url, method, user, branch, server)

    def _client(self, user, route: _Route, branch: Branch | None) -> tuple[Client, dict[str, str]]:
        client = Client(raise_request_exception=False)
        client.force_login(user)
        if branch is None:
            return client, {}
        if route.api:
            return client, {BRANCH_HEADER: branch.schema_id}
        client.cookies[COOKIE_NAME] = branch.schema_id
        return client, {}

    def _assert_served_as_on_main(self, route: _Route, url: str, method: str, user, branch: Branch) -> None:
        client, headers = self._client(user, route, None)
        with stub_kea(_recorded_kea()) as kea:
            on_main = client.generic(method, url, headers=headers)
        if method == "GET":
            self.assertEqual(on_main.status_code, _main_get_status(route), f"{url} on main: {kea.commands()}")

        client, headers = self._client(user, route, branch)
        with stub_kea(_recorded_kea()) as kea, _writes(branch) as writes:
            in_branch = client.generic(method, url, headers=headers)

        self.assertEqual(in_branch.status_code, on_main.status_code)
        self.assertEqual(writes, [])
        self.assertLessEqual(set(kea.commands()), set(_recorded_kea()), "a read sent a Kea command that is not a read")

    def _assert_refused(self, route: _Route, url: str, method: str, user, branch: Branch, server: Server) -> None:
        client, headers = self._client(user, route, branch)
        with stub_kea(_recorded_kea()) as kea, _writes(branch) as writes:
            response = client.generic(method, url, data="{}", content_type="application/json", headers=headers)

        self.assertEqual(response.status_code, 409, response.content[:300])
        if route.api:
            self.assertEqual(response.json()["code"], branching.BRANCH_WRITE_REFUSED)
        else:
            self.assertContains(response, f"Branch {branch.name} is active.", status_code=409)
        self.assertEqual(kea.commands(), [])
        self.assertEqual(writes, [])
        self.assertTrue(Server.objects.filter(pk=server.pk).exists())


class TagWriterRecoveryTest(TransactionTestCase):
    def setUp(self):
        super().setUp()
        if not apps.is_installed("netbox_dhcp"):
            if _required:
                self.fail("The branching CI job requires netbox_dhcp for tag writer recovery tests.")
            self.skipTest("netbox_dhcp is not installed")

    def test_tag_rename_waits_until_mapped_target_merge_commits(self):
        user = get_user_model().objects.create_superuser("tag-writer-reader")
        server = _make_db_server(name="tag-writer-source")
        target, mapping = linked_dhcp_targets(server)[0]
        tag = Tag.objects.create(name="mapping-tag-original", slug="mapping-tag-original")
        target.tags.add(tag)
        branch = _provisioned_branch(self, "tag writer recovery")
        branch.merge_strategy = "squash"
        branch.save(provision=False)
        with activate_branch(branch), event_tracking(_change_request(user)):
            type(target).objects.get(pk=target.pk).delete()
        request_finished.send(sender=type(self))
        selected, release, writer_ready, writer_done = Event(), Event(), Event(), Event()
        writer_pid = []
        replay_pid = []

        def pause_delete(sender, instance, using, **kwargs):
            if using == "default" and instance.pk == target.pk:
                with connections[using].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    replay_pid.append(cursor.fetchone()[0])
                selected.set()
                self.assertTrue(release.wait(20), "Native deletion was not released")

        def merge():
            try:
                Branch.objects.get(pk=branch.pk).merge(user=user)
            finally:
                connections.close_all()

        def rename():
            try:
                changed = Tag.objects.get(pk=tag.pk)
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    writer_pid.append(cursor.fetchone()[0])
                changed.name = "mapping-tag-renamed"
                writer_ready.set()
                with event_tracking(_change_request(user)):
                    changed.save()
            finally:
                writer_done.set()
                connections.close_all()

        pre_delete.connect(pause_delete, sender=type(target), weak=False)
        blocked = False
        try:
            with ThreadPoolExecutor(2) as pool:
                replay = pool.submit(merge)
                try:
                    self.assertTrue(selected.wait(20), "Native target deletion did not start")
                    writer = pool.submit(rename)
                    self.assertTrue(writer_ready.wait(20), "The ordinary tag writer did not start")
                    self.assertNotEqual(writer_pid, replay_pid)
                    deadline = timezone.now() + timedelta(seconds=20)
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
            pre_delete.disconnect(pause_delete, sender=type(target))
        self.assertEqual(Tag.objects.get(pk=tag.pk).name, "mapping-tag-renamed")
        self.assertFalse(type(target).objects.filter(pk=target.pk).exists())
        self.assertFalse(KeaDhcpLink.objects.filter(pk=mapping.pk).exists())
        branch.refresh_from_db()
        self.assertEqual(branch.status, "merged")
        self.assertTrue(blocked, "The tag writer committed newer target semantics before native deletion")


class RenderedBranchControlsTest(TransactionTestCase):
    """A branch page disables changes before JavaScript runs and keeps main controls enabled."""

    def test_server_edit_and_delete_links_are_disabled_only_in_a_branch(self):
        user = get_user_model().objects.create_superuser("branch-controls")
        server = _make_db_server(name="branch-controls")
        branch = _provisioned_branch(self, "controls")
        client = Client()
        client.force_login(user)
        url = reverse("plugins:netbox_kea:server", args=[server.pk])
        edit = reverse("plugins:netbox_kea:server_edit", args=[server.pk])
        delete = reverse("plugins:netbox_kea:server_delete", args=[server.pk])

        with stub_kea(_recorded_kea()):
            on_main = client.get(url)
            client.cookies[COOKIE_NAME] = branch.schema_id
            in_branch = client.get(url)

        self.assertContains(on_main, f'href="{edit}"')
        self.assertContains(on_main, f'hx-get="{delete}"')
        self.assertNotContains(in_branch, f'href="{edit}"')
        self.assertNotContains(in_branch, f'hx-get="{delete}"')
        self.assertContains(in_branch, 'aria-disabled="true"')
        self.assertContains(in_branch, "Switch to main to make this change.")

    def test_every_rendered_plugin_page_disables_its_known_mutation_controls(self):
        from bs4 import BeautifulSoup

        user = get_user_model().objects.create_superuser("page-controls")
        server = _make_db_server(name="page-controls", dhcp4=True, dhcp6=True, has_control_agent=True)
        ip = IPAddress.objects.create(address="192.0.2.16/24", dns_name="r4.example.com")
        ip.refresh_from_db()
        branch = _provisioned_branch(self, "page controls")
        self.client.force_login(user)
        routes = [route for route in _plugin_routes() if not route.api]
        plugin_paths = {urlsplit(_route_url(route, server, ip)).path for route in routes}
        mutation_paths = {
            urlsplit(_route_url(route, server, ip)).path
            for route in routes
            if re.search(r"(?:add|edit|delete|import|enable|disable)$", route.name)
        }
        checked = 0
        covered = set()
        for route in routes:
            if _main_get_status(route) != 200:
                continue
            url = _route_url(route, server, ip)
            for htmx in (False, True):
                headers = {"HX-Request": "true"} if htmx else {}
                self.client.cookies.pop(COOKIE_NAME, None)
                with stub_kea(_recorded_kea()):
                    main = self.client.get(url, headers=headers)
                self.client.cookies[COOKIE_NAME] = branch.schema_id
                with stub_kea(_recorded_kea()):
                    response = self.client.get(url, headers=headers)
                with self.subTest(route=route.name, htmx=htmx):
                    self.assertEqual(response.status_code, 200)
                    if not main.get("Content-Type", "").startswith("text/html"):
                        continue
                    before = BeautifulSoup(main.content, "html.parser")
                    after = BeautifulSoup(response.content, "html.parser")
                    self._assert_no_active_mutation_targets(after, url, mutation_paths, plugin_paths)
                    for control in before.find_all(["a", "button", "input"]):
                        if control.has_attr("disabled"):
                            continue
                        mutation = False
                        for attr in ("href", "hx-get", "data-hx-get"):
                            target = control.get(attr)
                            if target and urlsplit(urljoin(url, target)).path in mutation_paths:
                                mutation = True
                        for attr in (
                            "hx-post",
                            "hx-put",
                            "hx-patch",
                            "hx-delete",
                            "data-hx-post",
                            "data-hx-put",
                            "data-hx-patch",
                            "data-hx-delete",
                        ):
                            target = control.get(attr)
                            if target is not None and urlsplit(urljoin(url, target)).path in plugin_paths:
                                mutation = True
                        form = control.find_parent("form")
                        kind = control.get("type", "submit" if control.name == "button" else "text")
                        if form and kind in ("submit", "image"):
                            method = control.get("formmethod", form.get("method", "get")).lower()
                            target = control.get("formaction", form.get("action", ""))
                            if method == "post" and urlsplit(urljoin(url, target)).path in plugin_paths:
                                mutation = True
                        if not mutation:
                            continue
                        checked += 1
                        covered.add(route.name)
                        candidates = [
                            candidate
                            for candidate in after.find_all(control.name)
                            if candidate.get_text(" ", strip=True) == control.get_text(" ", strip=True)
                            and candidate.get("name") == control.get("name")
                            and candidate.get("value") == control.get("value")
                        ]
                        self.assertTrue(candidates, f"the branch omitted {control} on {url}")
                        self.assertTrue(
                            any(candidate.get("aria-disabled") == "true" for candidate in candidates),
                            f"enabled {control} on {url}",
                        )
        self.assertGreater(checked, 100, "the recorded pages must populate their mutation controls")
        for name in (
            "server",
            "server_subnets4",
            "server_shared_networks4",
            "server_reservations4",
            "server_leases4",
            "sync_jobs",
        ):
            self.assertIn(f"plugins:netbox_kea:{name}", covered)

    def _assert_no_active_mutation_targets(self, page, url, mutation_paths, plugin_paths):
        for control in page.find_all(True):
            for attr in ("href", "hx-get", "data-hx-get"):
                target = control.get(attr)
                if target:
                    self.assertNotIn(
                        urlsplit(urljoin(url, target)).path,
                        mutation_paths,
                        f"active {attr} on {control} at {url}",
                    )
            for attr in (
                "hx-post",
                "hx-put",
                "hx-patch",
                "hx-delete",
                "data-hx-post",
                "data-hx-put",
                "data-hx-patch",
                "data-hx-delete",
            ):
                target = control.get(attr)
                if target is not None:
                    self.assertNotIn(
                        urlsplit(urljoin(url, target)).path,
                        plugin_paths,
                        f"active {attr} on {control} at {url}",
                    )
            if control.name == "form" and control.get("method", "get").lower() == "post":
                self.assertNotIn(
                    urlsplit(urljoin(url, control.get("action", ""))).path,
                    plugin_paths,
                    f"active POST form at {url}",
                )


@override_settings(ROOT_URLCONF="netbox_kea.tests.branch_control_urls")
class RenderedSubmissionControlsTest(TransactionTestCase):
    """Rendered responses preserve read controls and refuse browser submission semantics."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("submission-controls"))
        self.branch = _provisioned_branch(self, "submission controls")
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

    def test_real_forms_and_overrides_refuse_writes_but_preserve_safe_controls(self):
        from bs4 import BeautifulSoup

        response = self.client.get("/kea-controls/read/")
        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.content, "html.parser")
        for name in ("save", "image", "external", "override", "invalid-type"):
            with self.subTest(control=name):
                self.assertEqual(page.find(id=name).get("aria-disabled"), "true")
                self.assertTrue(page.find(id=name).has_attr("disabled"))
        self.assertEqual(page.find(id="implicit").get("method"), "dialog", "implicit submission must not send POST")
        self.assertFalse(page.find(id="implicit").has_attr("action"))
        for name in ("export", "menu", "find", "default-get", "htmx-read"):
            with self.subTest(control=name):
                self.assertFalse(page.find(id=name).has_attr("disabled"))
                self.assertFalse(page.find(id=name).has_attr("aria-disabled"))
        self.assertEqual(page.find(id="implicit-only")["method"], "dialog")
        self.assertEqual(page.find(id="jobs")["href"], "/plugins/kea/sync-jobs/")
        self.assertEqual(page.find(id="export-inherited-action")["formaction"], "/kea-controls/read/")
        self.assertFalse(page.find("input", attrs={"name": "query", "value": "example"}).has_attr("disabled"))
        self.assertEqual(page.find(id="nested-read")["hx-get"], "/kea-controls/read/")
        self.assertFalse(page.find(id="nested-read").find_parent(attrs={"aria-disabled": "true"}))
        self.assertEqual(page.find(id="export")["formmethod"], "get")
        self.assertEqual(page.find(id="export")["formaction"], "/kea-controls/read/")
        self.assertEqual(page.find(id="search")["method"], "get")
        self.assertEqual(page.find(id="cancel")["href"], "/kea-controls/read/")
        self.assertEqual(page.find(id="read")["href"], "/kea-controls/read/")
        self.assertEqual(page.find(id="foreign")["href"], "https://example.invalid/kea-controls/change/")
        self.assertEqual(page.find(id="missing")["href"], "/kea-controls/unknown/")
        for name in ("edit", "modal", "patch"):
            with self.subTest(control=name):
                control = page.find(id=name)
                self.assertEqual(control["aria-disabled"], "true")
                self.assertFalse(any(key in control.attrs for key in ("href", "hx-get", "data-hx-patch")))
                wrapper = control.parent
                self.assertEqual(wrapper["tabindex"], "0")
                tooltip = page.find(id=wrapper["aria-describedby"])
                self.assertIn("Switch to main", tooltip.get_text())
        self.assertFalse(page.find(id="inherited").find_parent("div").has_attr("data-hx-post"))
        self.assertEqual(page.find(id="nested-read").find_parent("div")["hx-target"], "#results")
        self.assertEqual(page.find(id="nested-read").find_parent("div")["hx-swap"], "innerHTML")
        self.assertTrue(page.find(id="inherited").has_attr("disabled"))
        if "Content-Length" in response:
            self.assertEqual(
                int(response["Content-Length"]), len(response.content), "outer middleware may recompute length"
            )

    def test_main_html_non_html_and_streaming_responses_keep_their_bytes(self):
        from netbox_kea.tests.branch_control_urls import HTML

        del self.client.cookies[COOKIE_NAME]
        on_main = self.client.get("/kea-controls/read/")
        self.assertEqual(on_main.content, HTML.encode())
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id
        non_html = self.client.get("/kea-controls/read/?response=json")
        self.assertEqual(non_html.json(), {"html": HTML})
        stream = self.client.get("/kea-controls/read/?response=stream")
        self.assertTrue(stream.streaming)
        self.assertEqual(b"".join(stream.streaming_content), HTML.encode())

    def test_compression_runs_after_controls_are_disabled(self):
        import gzip

        from bs4 import BeautifulSoup

        middleware = [
            "django.middleware.gzip.GZipMiddleware",
            "netbox_kea.tests.branch_control_urls.passthrough",
            *settings.MIDDLEWARE,
        ]
        with override_settings(MIDDLEWARE=middleware):
            response = self.client.get("/kea-controls/read/", headers={"Accept-Encoding": "gzip"})

        self.assertEqual(response["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", response["Vary"])
        page = BeautifulSoup(gzip.decompress(response.content), "html.parser")
        self.assertEqual(page.find(id="save")["aria-disabled"], "true")
        self.assertFalse(page.find(id="edit").has_attr("href"))
        self.assertEqual(int(response["Content-Length"]), len(response.content))

    def test_preencoded_html_reports_a_configuration_error_before_decoding(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaisesRegex(ImproperlyConfigured, "before.*BranchRefusalMiddleware"):
            self.client.get("/kea-controls/read/?response=encoded")

    def test_htmx_fragments_disable_controls_with_their_own_accessible_reasons(self):
        from bs4 import BeautifulSoup

        responses = [self.client.get("/kea-controls/read/", headers={"HX-Request": "true"}) for _ in range(2)]
        reason_ids = []
        for response in responses:
            page = BeautifulSoup(response.content, "html.parser")
            self.assertEqual(page.find(id="save")["aria-disabled"], "true")
            wrapper = page.find(id="save").parent
            reason_ids.append(wrapper["aria-describedby"])
            self.assertEqual(page.find(id=reason_ids[-1])["role"], "tooltip")
        self.assertNotEqual(*reason_ids, "a fragment must not reuse a tooltip ID that can remain on the full page")

    def test_repeated_targets_resolve_once_per_request_kind(self):
        from unittest.mock import patch

        from bs4 import BeautifulSoup
        from django.urls import resolve

        with patch("netbox_kea.branch_controls.resolve", wraps=resolve) as spy:
            response = self.client.get("/kea-controls/read/?response=repeated")
        page = BeautifulSoup(response.content, "html.parser")
        self.assertEqual(len(page.find_all(attrs={"aria-disabled": "true"})), 75)
        self.assertEqual(
            sorted(call.args[0] for call in spy.call_args_list),
            ["/kea-controls/change/", "/kea-controls/read/", "/kea-controls/read/"],
            "one resolve per (target, method, navigation): change GET nav, read PATCH, read POST",
        )


class ResponseMiddlewareOrderTest(SimpleTestCase):
    def test_gzip_after_refusal_is_rejected_while_loading_the_real_chain(self):
        from django.core.exceptions import ImproperlyConfigured
        from django.core.handlers.base import BaseHandler

        for encoder in (
            "django.middleware.gzip.GZipMiddleware",
            "netbox_kea.tests.branch_control_urls.GZipSubclass",
            "netbox_kea.tests.branch_control_urls.GZipAlias",
        ):
            for refusal in (
                "netbox_kea.branching.BranchRefusalMiddleware",
                "netbox_kea.tests.branch_control_urls.RefusalSubclass",
                "netbox_kea.tests.branch_control_urls.RefusalAlias",
            ):
                for outer in ([], ["django.middleware.gzip.GZipMiddleware"]):
                    with self.subTest(encoder=encoder, refusal=refusal, outer=outer):
                        with override_settings(MIDDLEWARE=[*outer, refusal, encoder]):
                            with self.assertRaisesRegex(ImproperlyConfigured, "before.*BranchRefusalMiddleware"):
                                BaseHandler().load_middleware()

    def test_startup_registration_rejects_wrong_gzip_order(self):
        from django.core.exceptions import ImproperlyConfigured

        middleware = [*settings.MIDDLEWARE, "django.middleware.gzip.GZipMiddleware"]
        with override_settings(MIDDLEWARE=middleware):
            with self.assertRaisesRegex(ImproperlyConfigured, "before.*BranchRefusalMiddleware"):
                apps.get_app_config("netbox_kea").ready()

    def test_correct_order_accepts_a_regular_middleware_factory(self):
        from django.core.handlers.base import BaseHandler

        middleware = [
            "django.middleware.gzip.GZipMiddleware",
            "netbox_kea.tests.branch_control_urls.passthrough",
            *settings.MIDDLEWARE,
        ]
        with override_settings(MIDDLEWARE=middleware):
            BaseHandler().load_middleware()
