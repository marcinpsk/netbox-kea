# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the conftest URL-resolver bootstrap hook."""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from netbox_kea.tests.conftest import _prepopulate_url_resolver


class TestPrepopulateUrlResolver(SimpleTestCase):
    """The bootstrap hook must log (not silently swallow) setup failures."""

    def test_failure_is_logged_not_swallowed(self):
        """A resolver-populate error is logged for diagnosability and never propagates."""
        with patch("django.urls.get_resolver", side_effect=RuntimeError("boom")):
            with self.assertLogs("netbox_kea.tests.conftest", level="ERROR") as cm:
                _prepopulate_url_resolver()  # must not raise
        self.assertTrue(
            any("pre-populate" in line.lower() for line in cm.output),
            cm.output,
        )


def test_prepopulation_runs_after_db_unblock_not_in_pytest_configure():
    """Regression: the DB-touching ``_populate()`` must run in ``django_db_setup``
    (after ``django_db_blocker.unblock()``), not in ``pytest_configure`` where
    pytest-django blocks DB access (``RuntimeError: Database access not allowed``,
    so the bootstrap silently never ran)."""
    import inspect

    from netbox_kea.tests import conftest as cf

    assert not hasattr(cf, "pytest_configure"), "prepopulation must not run in pytest_configure (DB is blocked there)"
    assert "_prepopulate_url_resolver()" in inspect.getsource(cf.django_db_setup)
