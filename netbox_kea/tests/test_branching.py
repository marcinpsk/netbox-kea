# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""With netbox-branching installed, netbox_kea rows stay in main (ADR 0007).

The CI branching job sets NETBOX_KEA_REQUIRE_BRANCHING=1, so this module fails instead of
skipping when netbox-branching is not an installed app.
"""

import os

import pytest

from netbox_kea import branching

_REQUIRE_BRANCHING = "NETBOX_KEA_REQUIRE_BRANCHING"
_required = os.environ.get(_REQUIRE_BRANCHING, "")
if _required not in {"", "1"}:
    raise ValueError(f"{_REQUIRE_BRANCHING} must be 1 or unset, not {_required!r}")
if not branching.installed():
    if _required:
        raise RuntimeError(f"{_REQUIRE_BRANCHING}=1, but netbox_branching is not an installed app")
    pytest.skip("netbox-branching is not an installed app", allow_module_level=True)

from core.models import ObjectType  # noqa: E402
from django.apps import apps  # noqa: E402
from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection, models  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402
from django.db.models import ProtectedError  # noqa: E402
from django.test import SimpleTestCase, TransactionTestCase  # noqa: E402
from ipam.models import VRF  # noqa: E402
from netbox_branching import utilities as branching_utilities  # noqa: E402
from netbox_branching.choices import BranchStatusChoices  # noqa: E402
from netbox_branching.models import Branch  # noqa: E402
from netbox_branching.utilities import activate_branch, supports_branching  # noqa: E402

from netbox_kea.models import Server  # noqa: E402
from netbox_kea.tests.utils import _make_db_server  # noqa: E402

APP_LABEL = "netbox_kea"
# Changing this set needs a design decision (docs/design/netbox-branching.md).
BRANCHABLE_MODELS: frozenset[str] = frozenset()

# The on_delete handlers that write the referencing row, stated here apart from the resolver.
_WRITING_ON_DELETE = (
    models.CASCADE,
    models.SET_NULL,
    models.SET_DEFAULT,
    models.DB_CASCADE,
    models.DB_SET_NULL,
    models.DB_SET_DEFAULT,
)


def _plugin_models() -> list[type[models.Model]]:
    return list(apps.get_app_config(APP_LABEL).get_models())


def _writes_on_delete(on_delete) -> bool:
    if on_delete in _WRITING_ON_DELETE:
        return True
    # SET(value) builds a new function for each field; its deconstruct path names it.
    deconstruct = getattr(on_delete, "deconstruct", None)
    return deconstruct is not None and deconstruct()[0] == "django.db.models.SET"


def _follows_the_rule(model: type[models.Model]) -> bool:
    """Restate the rule: a concrete foreign key that writes on delete, to a branchable model outside the plugin."""
    return any(
        isinstance(field, models.ForeignKey)
        and field.related_model._meta.app_label != APP_LABEL
        and _writes_on_delete(field.remote_field.on_delete)
        and supports_branching(field.related_model)
        for field in model._meta.concrete_fields
    )


class BranchabilityPinTest(SimpleTestCase):
    """Guard 2: netbox-branching agrees with the rule, and the branchable set is the pinned set."""

    def test_supports_branching_follows_the_rule_for_every_plugin_model(self):
        for model in _plugin_models():
            with self.subTest(model=model._meta.label):
                self.assertEqual(supports_branching(model), _follows_the_rule(model))

    def test_the_branchable_set_is_the_pinned_set(self):
        branchable = {model._meta.label for model in _plugin_models() if _follows_the_rule(model)}

        self.assertEqual(
            branchable,
            BRANCHABLE_MODELS,
            "The branchable netbox_kea models changed. This change needs a design decision "
            "(docs/design/netbox-branching.md): open branches lack the table of a model that becomes "
            "branchable, and keep a stale copy of a model that stops being branchable.",
        )

    def test_the_rule_reads_every_plugin_model(self):
        self.assertEqual(
            {model._meta.label for model in _plugin_models()},
            {"netbox_kea.Server", "netbox_kea.SyncConfig", "netbox_kea.KeaDhcpLink"},
        )


class ResolverTest(SimpleTestCase):
    """The resolver answers for plugin models only, also for the historical models of a migration."""

    def test_the_resolver_is_registered_once(self):
        self.assertEqual(branching_utilities._branching_resolvers.count(branching.is_branchable), 1)

    def test_the_resolver_defers_for_other_apps(self):
        self.assertIsNone(branching.is_branchable(VRF))

    def test_the_resolver_keeps_a_change_logged_server_in_main(self):
        self.assertFalse(branching.is_branchable(Server))
        self.assertFalse(supports_branching(Server))

    def test_a_historical_server_with_a_set_null_vrf_is_branchable(self):
        # Branch migrate hands the resolver historical models. Before 0017, sync_vrf was SET_NULL.
        state = MigrationLoader(None, ignore_no_migrations=True).project_state(
            (APP_LABEL, "0016_charfield_blank_not_null")
        )
        historical_server = state.apps.get_model(APP_LABEL, "Server")

        self.assertTrue(branching.is_branchable(historical_server))

    def test_the_active_branch_is_the_branching_context(self):
        branch = Branch(name="context only")

        with activate_branch(branch):
            self.assertIs(branching.active_branch(), branch)
        self.assertIsNone(branching.active_branch())


class SuiteConfigurationTest(SimpleTestCase):
    """The run loads netbox-branching as NetBox requires it."""

    def test_netbox_branching_is_the_last_plugin(self):
        self.assertEqual(settings.PLUGINS[-1], "netbox_branching")


def _provisioned_branch(test: TransactionTestCase, name: str) -> Branch:
    branch = Branch(name=name)
    branch.save(provision=False)
    test.addCleanup(branch.deprovision)
    branch.provision(user=None)
    branch.refresh_from_db()
    test.assertEqual(branch.status, BranchStatusChoices.READY)
    return branch


class ProvisionedBranchTest(TransactionTestCase):
    """A provisioned branch holds no netbox_kea table, so it reads main's rows."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A deployment runs migrate after the upgrade, which stores the resolver's answer.
        call_command("migrate", verbosity=0)

    def test_stored_features_hold_no_branching_for_plugin_models(self):
        for model in _plugin_models():
            with self.subTest(model=model._meta.label):
                features = ObjectType.objects.get_for_model(model).features

                self.assertNotIn("branching", features)

    def test_a_provisioned_branch_holds_no_plugin_table(self):
        branch = _provisioned_branch(self, "no plugin tables")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", [branch.schema_name]
            )
            tables = {row[0] for row in cursor.fetchall()}

        self.assertIn(VRF._meta.db_table, tables, "the branch copied no table, so the check below reads nothing")
        self.assertEqual({table for table in tables if table.startswith(f"{APP_LABEL}_")}, set())

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
