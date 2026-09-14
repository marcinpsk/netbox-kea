# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The test-database lifecycle must match pytest-django's own flag semantics.

``django_db_setup`` here overrides pytest-django's fixture. Upstream reads the
``--reuse-db`` / ``--create-db`` pair with *two different* expressions
(``pytest_django/fixtures.py``): ``keepdb`` at setup is
``reuse_db and not create_db``, but the teardown branch tests ``reuse_db``
alone. Collapsing both into one value silently loses "rebuild and keep", which
is the only way to repair a database whose migrations were rebased.
"""

import pytest
from django.test import SimpleTestCase

from netbox_kea.tests.conftest import database_lifecycle


class _Flags:
    """A fake of the one Config method the decision reads."""

    def __init__(self, *, reuse_db: bool, create_db: bool) -> None:
        self._values = {"reuse_db": reuse_db, "create_db": create_db}

    def getvalue(self, name: str) -> bool:
        return self._values[name]


class TestDatabaseLifecycle(SimpleTestCase):
    """Every flag combination must behave the way pytest-django documents."""

    def test_reuse_db_alone_keeps_the_database(self):
        """The documented everyday form: build if absent, migrate, keep."""
        keepdb, teardown = database_lifecycle(_Flags(reuse_db=True, create_db=False))
        self.assertTrue(keepdb)
        self.assertFalse(teardown)

    def test_create_db_alone_rebuilds_then_drops(self):
        """A throwaway rebuild: no reuse was asked for, so nothing is kept."""
        keepdb, teardown = database_lifecycle(_Flags(reuse_db=False, create_db=True))
        self.assertFalse(keepdb)
        self.assertTrue(teardown)

    def test_both_flags_rebuild_and_keep(self):
        """The repair path: force a clean rebuild and keep it for the next run.

        This is what a stale schema after a rebased migration needs. Treating
        the pair as "keepdb false" for teardown as well throws the rebuild away,
        so the next run pays the full migration cost again.
        """
        keepdb, teardown = database_lifecycle(_Flags(reuse_db=True, create_db=True))
        self.assertFalse(keepdb, "both flags must rebuild at setup")
        self.assertFalse(teardown, "--reuse-db must keep the rebuilt database")

    def test_neither_flag_is_a_throwaway_database(self):
        """No flags: build it, use it, drop it."""
        keepdb, teardown = database_lifecycle(_Flags(reuse_db=False, create_db=False))
        self.assertFalse(keepdb)
        self.assertTrue(teardown)


@pytest.mark.parametrize(
    ("reuse_db", "create_db"),
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_teardown_never_contradicts_reuse(reuse_db, create_db):
    """Whatever the pair, asking to reuse must never end in a teardown."""
    _, teardown = database_lifecycle(_Flags(reuse_db=reuse_db, create_db=create_db))
    assert teardown is not reuse_db
