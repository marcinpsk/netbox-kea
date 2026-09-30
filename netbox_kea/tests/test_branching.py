# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""With netbox-branching installed, netbox_kea rows stay in main (ADR 0007).

The CI branching job sets NETBOX_KEA_REQUIRE_BRANCHING=1, so this module fails instead of
skipping when netbox-branching is not an installed app.
"""

import importlib
import json
import os
import uuid
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from urllib.parse import urlencode

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

from core.models import ObjectType  # noqa: E402
from django.apps import apps  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection, connections, models, router  # noqa: E402
from django.db.migrations import RunPython, RunSQL, SeparateDatabaseAndState  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402
from django.db.migrations.operations.base import Operation  # noqa: E402
from django.db.migrations.operations.models import ModelOperation  # noqa: E402
from django.db.models import ProtectedError  # noqa: E402
from django.test import Client, RequestFactory, SimpleTestCase, TransactionTestCase, override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import reverse  # noqa: E402
from ipam.models import VRF, IPAddress  # noqa: E402
from netbox.context_managers import event_tracking  # noqa: E402
from netbox_branching import utilities as branching_utilities  # noqa: E402
from netbox_branching.choices import BranchStatusChoices  # noqa: E402
from netbox_branching.constants import BRANCH_HEADER, COOKIE_NAME, QUERY_PARAM  # noqa: E402
from netbox_branching.models import Branch  # noqa: E402
from netbox_branching.utilities import activate_branch, supports_branching  # noqa: E402

from netbox_kea.kea import KeaException  # noqa: E402
from netbox_kea.models import Server  # noqa: E402
from netbox_kea.tests.kea_stub import stub_kea  # noqa: E402
from netbox_kea.tests.utils import _WRITE_VERBS, _make_db_server  # noqa: E402

# Changing this set needs a design decision (docs/design/netbox-branching.md).
BRANCHABLE_MODELS: frozenset[str] = frozenset()

# The on_delete handlers that do not write the referencing row. Every other one does (SET(...) and DB_* too).
_NON_WRITING_ON_DELETE = (models.PROTECT, models.RESTRICT, models.DO_NOTHING)


def _plugin_models() -> list[type[models.Model]]:
    return list(apps.get_app_config(APP_LABEL).get_models())


def _writing_foreign_keys(model: type[models.Model]) -> list[models.ForeignKey]:
    return [
        field
        for field in model._meta.concrete_fields
        if isinstance(field, models.ForeignKey) and field.remote_field.on_delete not in _NON_WRITING_ON_DELETE
    ]


def _models_that_need_a_branch_copy() -> dict[str, list[str]]:
    """Return each plugin model that a delete in a branch writes, with the foreign key path that reaches it.

    A writing foreign key to a branchable model outside the plugin starts a path. A writing foreign
    key to a plugin model on a path extends it, so the rule is transitive.
    """
    paths: dict[str, list[str]] = {}
    for model in _plugin_models():
        for field in _writing_foreign_keys(model):
            target = field.related_model
            if target._meta.app_label != APP_LABEL and supports_branching(target):
                paths.setdefault(model._meta.label, [f"{model._meta.label}.{field.name} -> {target._meta.label}"])
    grown = True
    while grown:
        grown = False
        for model in _plugin_models():
            if model._meta.label in paths:
                continue
            for field in _writing_foreign_keys(model):
                if (target_path := paths.get(field.related_model._meta.label)) is not None:
                    paths[model._meta.label] = [f"{model._meta.label}.{field.name}", *target_path]
                    grown = True
                    break
    return paths


class BranchabilityPinTest(SimpleTestCase):
    """Guard 2: the rule, computed here only, gives the pinned set, and netbox-branching routes none to a branch."""

    def test_the_models_that_need_a_branch_copy_are_the_pinned_set(self):
        paths = _models_that_need_a_branch_copy()

        self.assertEqual(
            set(paths),
            BRANCHABLE_MODELS,
            "The netbox_kea models that a delete in a branch writes changed: "
            + "; ".join(" -> ".join(path) for path in paths.values())
            + ". This change needs a design decision (docs/design/netbox-branching.md): the resolver keeps "
            "every netbox_kea model in main, so open branches lack the table, and the write reaches main.",
        )

    def test_netbox_branching_keeps_every_plugin_model_in_main(self):
        for model in _plugin_models():
            with self.subTest(model=model._meta.label):
                self.assertIs(supports_branching(model), False)

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

    ``header``, ``query`` and ``cookie`` name a branch state: ready, unready (merged), unknown, or "" (empty).
    """

    name: str
    outcome: str
    api: bool = False
    htmx: bool = False
    header: str | None = None
    query: str | None = None
    cookie: str | None = None
    csrf: bool = True


_SELECTIONS = (
    _Selection("UI, no selector", "served"),
    _Selection("UI, X-NetBox-Branch header naming a ready branch", "served", header="ready"),
    _Selection("UI, query naming an unknown branch", "nbb-400", query="unknown"),
    _Selection("UI, query naming an unready branch", "unusable", query="unready"),
    _Selection("UI, empty query with a stale cookie", "served", query="", cookie="unready"),
    _Selection("UI, query naming a ready branch", "refused", query="ready"),
    _Selection("UI, cookie naming an unknown branch", "unusable", cookie="unknown"),
    _Selection("UI, cookie naming an unready branch", "unusable", cookie="unready"),
    _Selection("UI, cookie naming a ready branch", "refused", cookie="ready"),
    _Selection("UI, cookie naming a ready branch, no CSRF token", "csrf-403", cookie="ready", csrf=False),
    _Selection("HTMX, cookie naming a ready branch", "refused", htmx=True, cookie="ready"),
    _Selection("HTMX, query naming a ready branch", "refused", htmx=True, query="ready"),
    _Selection("HTMX, cookie naming an unready branch", "unusable", htmx=True, cookie="unready"),
    _Selection("HTMX, cookie naming an unknown branch", "unusable", htmx=True, cookie="unknown"),
    _Selection("HTMX, query naming an unready branch", "unusable", htmx=True, query="unready"),
    _Selection("API, no selector", "served", api=True),
    _Selection("API, header naming an unknown branch", "nbb-400", api=True, header="unknown"),
    _Selection("API, header naming an unready branch", "nbb-400", api=True, header="unready"),
    _Selection("API, header naming a ready branch", "refused", api=True, header="ready"),
    _Selection("API, query naming an unknown branch", "nbb-400", api=True, query="unknown"),
    _Selection("API, query naming an unready branch", "unusable", api=True, query="unready"),
    _Selection("API, empty query with a stale cookie", "served", api=True, query="", cookie="unready"),
    _Selection("API, query naming a ready branch", "refused", api=True, query="ready"),
    _Selection("API, cookie naming an unknown branch", "unusable", api=True, cookie="unknown"),
    _Selection("API, cookie naming an unready branch", "unusable", api=True, cookie="unready"),
    _Selection("API, cookie naming a ready branch", "refused", api=True, cookie="ready"),
)

_REFUSAL_STATUS = {"refused": 409, "unusable": 409, "nbb-400": 400, "csrf-403": 403}


class SelectorTableTest(TransactionTestCase):
    """The design's selector table: a change to a Server row, sent with each branch selection, CSRF enforced."""

    def test_each_selection_has_the_outcome_of_the_design(self):
        user = get_user_model().objects.create_superuser("selector-user")
        ready = _provisioned_branch(self, "selector ready")
        unready = Branch(name="selector merged")
        unready.save(provision=False)
        Branch.objects.filter(pk=unready.pk).update(status=BranchStatusChoices.MERGED)
        schema_ids = {"ready": ready.schema_id, "unready": unready.schema_id, "unknown": "unknown0", "": ""}

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
                        response = client.patch(
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
            reason = b"is not ready for use" if selection.header == "unready" else b"Invalid branch identifier"
            self.assertIn(reason, response.content)
            return
        if selection.outcome == "csrf-403":
            self.assertIn(b"CSRF", response.content)
            return
        refused = selection.outcome == "refused"
        text = f"Branch {ready.name} is active." if refused else "The selected branch is not usable"
        if selection.api:
            code = "branch_write_refused" if refused else "branch_selection_unusable"
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
        self.sources = f"kea=live; plugin=main; branch={self.branch.schema_id}"

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


_BANNER = "netbox-kea refuses changes"


class BranchPageTest(_BranchReadTestCase):
    """In a branch, plugin pages show the banner, and the IPAddress panel offers no reservation add."""

    def test_a_plugin_page_in_a_branch_shows_the_banner(self):
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(reverse("plugins:netbox_kea:server_list"))

        self.assertContains(response, _BANNER)
        self.assertContains(response, f"netbox-branching's routing for branch {self.branch.name}")

    def test_a_plugin_page_on_main_shows_no_banner(self):
        self.assertNotContains(self.client.get(reverse("plugins:netbox_kea:server_list")), _BANNER)

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
        self.assertNotContains(in_branch, _BANNER, msg_prefix="the banner is for plugin pages only")


@override_settings(ROOT_URLCONF="netbox_kea.tests.branch_refusal_urls")
class BranchActiveResponseTest(TransactionTestCase):
    """The middleware renders BranchActive, which plugin code raises at a change sink, as the 409 of the contract."""

    def setUp(self):
        from netbox_kea.tests.branch_refusal_urls import API_REFUSE_PATH, REFUSE_PATH

        self.user = get_user_model().objects.create_superuser("sink-user")
        self.client.force_login(self.user)
        self.branch = _provisioned_branch(self, "sink")
        self.ui, self.api = f"/{REFUSE_PATH}", f"/{API_REFUSE_PATH}"
        self.text = f"Branch {self.branch.name} is active."

    def test_on_main_the_sink_does_not_refuse(self):
        self.assertContains(self.client.get(self.ui), "not refused")

    def test_a_ui_request_gets_the_409_page(self):
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(self.ui)

        self.assertContains(response, self.text, status_code=409)
        self.assertContains(response, f'href="{branching.main_url()}"', status_code=409)

    def test_a_rest_request_gets_the_409_code(self):
        response = self.client.get(self.api, headers={BRANCH_HEADER: self.branch.schema_id})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "branch_write_refused")

    def test_an_htmx_request_reloads_and_shows_the_message(self):
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(self.ui, headers={"HX-Request": "true"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["HX-Refresh"], "true")
        self.assertContains(self.client.get(reverse("plugins:netbox_kea:server_list")), self.text)
