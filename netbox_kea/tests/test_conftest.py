# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the conftest URL-resolver bootstrap hook."""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from netbox_kea.tests.conftest import _prepopulate_url_resolver, _test_database_name


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


class TestDatabaseName(SimpleTestCase):
    """The test database selector must default safely and support task isolation."""

    @patch.dict("os.environ", {}, clear=True)
    def test_default_name(self):
        self.assertEqual(_test_database_name(), "test_netbox_kea")

    @patch.dict("os.environ", {"TEST_DB_NAME": "test_netbox_kea_lease_guard"}, clear=True)
    def test_explicit_isolated_name(self):
        self.assertEqual(_test_database_name(), "test_netbox_kea_lease_guard")

    @patch.dict("os.environ", {"TEST_DB_NAME": "netbox"}, clear=True)
    def test_non_test_database_name_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "must start with test_"):
            _test_database_name()


def test_prepopulation_runs_after_db_unblock_not_in_pytest_configure():
    """Regression: the DB-touching ``_populate()`` must run in ``django_db_setup``
    (after ``django_db_blocker.unblock()``), not in ``pytest_configure`` where
    pytest-django blocks DB access (``RuntimeError: Database access not allowed``,
    so the bootstrap silently never ran)."""
    import inspect

    from netbox_kea.tests import conftest as cf

    assert not hasattr(cf, "pytest_configure"), "prepopulation must not run in pytest_configure (DB is blocked there)"
    assert "_prepopulate_url_resolver()" in inspect.getsource(cf.django_db_setup)


@patch.dict("os.environ", {"TEST_DB_NAME": "test_parallel_task", "PYTEST_XDIST_WORKER": "gw2"})
def test_database_name_includes_xdist_worker():
    """Give each xdist worker a private database derived from the task name."""
    assert _test_database_name() == "test_parallel_task_gw2"


class TestPluginCacheHygiene(SimpleTestCase):
    """The autouse cleanup must clear the plugin's keys, and only the plugin's keys."""

    def test_drops_every_plugin_key_and_leaves_others_alone(self):
        from django.core.cache import cache

        from netbox_kea.tests.conftest import _PLUGIN_CACHE_PATTERNS, _drop_plugin_cache_entries

        plugin_keys = [pattern.replace("*", "4242:4") for pattern in _PLUGIN_CACHE_PATTERNS]
        unrelated = "netbox_kea_unrelated:keep-me"
        for key in (*plugin_keys, unrelated):
            cache.set(key, "sentinel", 300)
        self.addCleanup(cache.delete, unrelated)

        _drop_plugin_cache_entries()

        for key in plugin_keys:
            self.assertIsNone(cache.get(key), key)
        # Scoped cleanup: NetBox's own cached state must survive.
        self.assertEqual(cache.get(unrelated), "sentinel")

    def test_cleanup_is_autouse_so_no_test_base_has_to_opt_in(self):
        from netbox_kea.tests import conftest as cf

        marker = cf._plugin_cache_hygiene._fixture_function_marker
        self.assertTrue(marker.autouse, "the whole point is that no base has to opt in")
        self.assertEqual(marker.scope, "function", "per-test cleanup, not per-class")

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
    def test_a_backend_without_delete_pattern_fails_loudly(self):
        """Silently skipping the cleanup would let the cross-test cache leak return."""
        from netbox_kea.tests.conftest import _drop_plugin_cache_entries

        with self.assertRaisesRegex(RuntimeError, "delete_pattern"):
            _drop_plugin_cache_entries()


class TestPluginCacheHygieneReachesDjangoTestCases(SimpleTestCase):
    """The autouse fixture must reach unittest-style classes, not only pytest functions.

    Every view test is a Django ``TestCase``. If the fixture did not apply there, each
    base would have to clear the cache itself, which is the leak that keeps coming back.
    ``setUpClass`` runs before the function-scoped fixture, so the planted entry proves
    the cleanup ran for this class rather than for some earlier test.
    """

    _PLANTED = "netbox_kea:subnet_catalogue:v1:4242:4:snapshot:planted"

    @classmethod
    def setUpClass(cls):
        from django.core.cache import cache

        super().setUpClass()
        cache.set(cls._PLANTED, "sentinel", 300)

    def test_the_autouse_cleanup_runs_for_a_django_test_case(self):
        from django.core.cache import cache

        self.assertIsNone(
            cache.get(self._PLANTED),
            "The autouse cache cleanup did not run for a Django TestCase; every view base "
            "would then have to clear the plugin cache itself.",
        )
