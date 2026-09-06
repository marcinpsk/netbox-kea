# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Every cross-app migration dependency must resolve without squash replacement.

``migrate`` builds the graph with ``replace_migrations=True``, so a squash's
``replaces`` list remaps a dependency on a migration NetBox has since squashed away
and the graph still builds. The suite, test-database creation and ``migrate --plan``
are therefore all green while the node is genuinely absent — only a graph built
without replacement, which is what ``sqlmigrate`` uses, shows it. That is why this
needs its own test: no amount of running the suite can surface it.

Left unfixed it is latent, not live. It becomes live the moment NetBox drops the
``replaces`` list or squashes the squash, at which point ``migrate`` itself fails.
"""

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

#: Django resolves these itself; they are never literal graph nodes.
_SENTINELS = frozenset({"__first__", "__latest__"})

#: A core app whose graph is known good, so the check is observed to pass and not
#: merely to fail. A test only ever watched failing proves the failure path alone.
_CONTROL_APP = "ipam"


def _unresolved_dependencies(app_label):
    """Return (migration, missing parent) for every dangling cross-app dependency."""
    loader = MigrationLoader(None, load=False, replace_migrations=False)
    loader.load_disk()
    known = set(loader.disk_migrations)
    missing = []
    for key in sorted(k for k in known if k[0] == app_label):
        for parent in loader.disk_migrations[key].dependencies:
            if parent[0] == "__setting__" or parent[1] in _SENTINELS:
                continue
            if parent[0] != app_label and parent not in known:
                missing.append((key[1], f"{parent[0]}.{parent[1]}"))
    return missing


class TestMigrationGraphResolves(SimpleTestCase):
    """Guard against depending on a NetBox migration that a squash removed."""

    def test_no_dependency_needs_squash_replacement(self):
        """Scoped to this app: a co-installed plugin's defect must not fail us.

        The graph raises on the first dangling node it validates, so in a shared
        devcontainer an unscoped check reports whichever sibling plugin happens to be
        broken and every app looks broken.
        """
        missing = _unresolved_dependencies("netbox_kea")

        self.assertEqual(
            missing,
            [],
            "These dependencies only resolve because a squash remaps them, so they "
            "break as soon as NetBox drops its `replaces` list: "
            + "; ".join(f"{name} -> {parent}" for name, parent in missing),
        )

    def test_the_check_can_report_success(self):
        """A known-good app must come back clean, or the test above proves nothing."""
        self.assertEqual(_unresolved_dependencies(_CONTROL_APP), [])
