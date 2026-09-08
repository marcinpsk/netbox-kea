# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Every cross-app migration dependency must resolve without squash replacement.

``migrate`` builds the graph with ``replace_migrations=True``, so a squash's
``replaces`` list remaps a dependency on a migration NetBox has since squashed away
and the graph still builds. The suite, test-database creation and ``migrate --plan``
are therefore all green while the node is genuinely absent. Only a graph built
without replacement, which is what ``sqlmigrate`` uses, shows it. That is why this
needs its own test: no amount of running the suite can surface it.

Left unfixed it is latent, not live. It becomes live the moment NetBox drops the
``replaces`` list or squashes the squash, at which point ``migrate`` itself fails.

The walker proves a declared parent still exists. It cannot see a dependency that
was dropped, and it skips ``__first__`` outright, so the sufficiency guard below
proves the remaining dependency set still orders ``0001_initial`` correctly.
"""

from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

#: Django resolves these itself; they are never literal graph nodes.
_SENTINELS = frozenset({"__first__", "__latest__"})

#: A core app whose graph is known good, so the check is observed to pass and not
#: merely to fail. A test only ever watched failing proves the failure path alone.
_CONTROL_APP = "ipam"


def _ordering_key(name):
    """Rank migration names by numeric prefix, with the sentinels at the extremes.

    Plain string order is wrong once a prefix gains a digit: ``9999_x`` sorts above
    ``10000_y``. ``__first__`` is the weakest ordering an edge can express, so it ranks
    below every named node.
    """
    if name == "__first__":
        return (0, 0, "")
    if name == "__latest__":
        return (2, 0, "")
    prefix, _, rest = name.partition("_")
    return (1, int(prefix) if prefix.isdigit() else 0, rest)


def _newest_live_ancestor(key, dependency_app, disk_migrations):
    """Walk our own chain and return the newest *dependency_app* node we already reach.

    ``None`` means nothing in our ancestry orders us after that app. A named result is a
    hint, not a proof that the edge is safe to drop: an older node can still precede the
    model the declaring migration needs.

    A ``__first__`` edge is an ordering constraint even though it is not a graph node, so
    it counts here. Dropping it would report ``None`` against an app we do order after.
    """
    pending = [key]
    visited = set()
    ancestors = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for parent in disk_migrations[current].dependencies:
            if parent[0] == key[0] and parent in disk_migrations:
                pending.append(parent)
            elif parent[0] == dependency_app and (parent[1] in _SENTINELS or parent in disk_migrations):
                ancestors.add(parent[1])
    if not ancestors:
        return None
    return f"{dependency_app}.{max(ancestors, key=_ordering_key)}"


def _apps_referenced_by(migration):
    """Apps whose models this migration points at, read from field metadata.

    Read from the loaded fields rather than the source text: a target written as a
    parenthesized or concatenated string still deconstructs to ``app.Model``, so the
    guard cannot be turned green by reformatting the migration.
    """
    referenced = set()
    for operation in migration.operations:
        fields = list(getattr(operation, "fields", []) or [])
        if hasattr(operation, "field"):
            fields.append((getattr(operation, "name", ""), operation.field))
        for _name, field in fields:
            *_, kwargs = field.deconstruct()
            for target in (kwargs.get("to"), kwargs.get("through")):
                if isinstance(target, str) and "." in target:
                    referenced.add(target.split(".")[0].lower())
    return referenced


def _unresolved_dependencies(app_label, disk_migrations=None):
    """Return the declaring name, missing parent, initial flag, and newest live ancestor."""
    if disk_migrations is None:
        loader = MigrationLoader(None, load=False, replace_migrations=False)
        loader.load_disk()
        disk_migrations = loader.disk_migrations
        # An empty load would make every caller pass without inspecting anything.
        if not any(key[0] == app_label for key in disk_migrations):
            raise AssertionError(f"no migrations on disk for {app_label!r}: the check would pass vacuously")
    known = set(disk_migrations)
    missing = []
    for key in sorted(k for k in known if k[0] == app_label):
        migration = disk_migrations[key]
        # Django treats a migration with no same-app parent as initial even when unset.
        initial = bool(migration.initial) or not any(p[0] == app_label for p in migration.dependencies)
        for parent in migration.dependencies:
            if parent[0] == "__setting__" or parent[1] in _SENTINELS:
                continue
            if parent[0] != app_label and parent not in known:
                newest = _newest_live_ancestor(key, parent[0], disk_migrations)
                missing.append((key[1], f"{parent[0]}.{parent[1]}", initial, newest))
    return missing


def _dependency_failure_message(missing):
    return "Dependencies on migrations that do not exist:\n" + "\n".join(
        f"{name} -> {parent} (initial={initial}, newest_live_ancestor={newest})"
        for name, parent, initial, newest in missing
    )


class TestMigrationGraphResolves(SimpleTestCase):
    """Guard against depending on a NetBox migration that a squash removed."""

    def test_no_dependency_needs_squash_replacement(self):
        """Scoped to this app: a co-installed plugin's defect must not fail us.

        The graph raises on the first dangling node it validates, so in a shared
        devcontainer an unscoped check reports whichever sibling plugin happens to be
        broken and every app looks broken.
        """
        missing = _unresolved_dependencies("netbox_kea")

        self.assertEqual(missing, [], _dependency_failure_message(missing))

    def test_the_check_can_report_success(self):
        """A known-good app must come back clean, or the test above proves nothing."""
        self.assertEqual(_unresolved_dependencies(_CONTROL_APP), [])

    def test_initial_still_orders_after_the_apps_it_references(self):
        """Dropping a dependency must not go unnoticed: the walker only sees ones we declare."""
        loader = MigrationLoader(None, load=False, replace_migrations=False)
        loader.load_disk()
        migration = loader.disk_migrations[("netbox_kea", "0001_initial")]
        referenced = _apps_referenced_by(migration)
        self.assertTrue(referenced, "no relation targets found: the guard would pass vacuously")
        dependencies = {app for app, _ in migration.dependencies}
        for app in sorted(referenced - {"netbox_kea"}):
            self.assertIn(app, dependencies, f"0001_initial references {app} but does not depend on it")

    @staticmethod
    def _disk(*specs):
        """Build a disk_migrations mapping from (app, name, dependencies) triples."""
        migrations = []
        for app, name, dependencies in specs:
            migration = Migration(name, app)
            migration.dependencies = list(dependencies)
            migrations.append(migration)
        return {(m.app_label, m.name): m for m in migrations}

    def test_a_sentinel_edge_counts_as_ordering(self):
        """`__first__` orders us after the app even though it is not a graph node."""
        disk = self._disk(
            ("plugin", "0001_initial", [("extras", "__first__")]),
            ("plugin", "0002_later", [("plugin", "0001_initial"), ("extras", "0099_missing")]),
        )

        missing = _unresolved_dependencies("plugin", disk)

        self.assertEqual(missing, [("0002_later", "extras.0099_missing", False, "extras.__first__")])

    def test_ancestors_are_ranked_numerically_not_lexicographically(self):
        """`10000_` is newer than `9999_`, which plain string order gets backwards."""
        disk = self._disk(
            ("plugin", "0001_initial", [("extras", "9999_previous")]),
            ("plugin", "0002_later", [("plugin", "0001_initial"), ("extras", "10000_newest"), ("extras", "0500_gone")]),
            ("extras", "9999_previous", []),
            ("extras", "10000_newest", []),
        )

        missing = _unresolved_dependencies("plugin", disk)

        self.assertEqual(missing, [("0002_later", "extras.0500_gone", False, "extras.10000_newest")])

    def test_the_check_can_report_a_dangling_dependency(self):
        initial = Migration("0001_initial", "plugin")
        initial.initial = True
        initial.dependencies = [("extras", "0001_missing"), ("extras", "0002_live")]
        middle = Migration("0002_middle", "plugin")
        middle.dependencies = [("plugin", "0001_initial"), ("extras", "0003_live")]
        latest = Migration("0003_latest", "plugin")
        latest.initial = False
        latest.dependencies = [("plugin", "0002_middle"), ("extras", "0004_missing"), ("ipam", "0001_missing")]
        migrations = [initial, middle, latest]
        migrations.extend(Migration(name, "extras") for name in ("0002_live", "0003_live", "0005_unrelated"))
        disk = {(migration.app_label, migration.name): migration for migration in migrations}

        missing = _unresolved_dependencies("plugin", disk)

        self.assertEqual(
            missing,
            [
                ("0001_initial", "extras.0001_missing", True, "extras.0002_live"),
                ("0003_latest", "extras.0004_missing", False, "extras.0003_live"),
                ("0003_latest", "ipam.0001_missing", False, None),
            ],
        )
        message = _dependency_failure_message(missing)
        self.assertEqual(
            message,
            "Dependencies on migrations that do not exist:\n"
            "0001_initial -> extras.0001_missing (initial=True, newest_live_ancestor=extras.0002_live)\n"
            "0003_latest -> extras.0004_missing (initial=False, newest_live_ancestor=extras.0003_live)\n"
            "0003_latest -> ipam.0001_missing (initial=False, newest_live_ancestor=None)",
        )
