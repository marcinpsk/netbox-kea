"""Pytest fixtures/hooks for netbox_kea unit tests.

Force the URL resolver to import every plugin's urlconf while the *real*
PLUGINS_CONFIG is active (at session start), so the plugin URL namespaces are
registered and cached in sys.modules before any test enters an
``@override_settings(PLUGINS_CONFIG=<minimal>)`` block.

Some co-installed plugins (in a multi-plugin dev container) read their own
``settings.PLUGINS_CONFIG['<plugin>']`` key at *import* time. When such a
plugin's urlconf is first imported lazily — which, without this hook, happens
inside an overridden test where its key is absent — it raises ``KeyError`` and
its namespace never registers, causing ``NoReverseMatch`` when the nav menu is
rendered by *every* page-rendering view test. Pre-populating the resolver here
avoids that. In an isolated CI environment (only netbox_kea installed) this is a
harmless no-op.
"""

import logging
import os
import warnings
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

from netbox_kea.tests.parallel import MAX_PARALLEL_WORKERS, isolated_test_database_name

logger = logging.getLogger(__name__)

#: NetBox release that ``netbox_kea/tests/query_counts.json`` was recorded against.
QUERY_COUNT_NETBOX_VERSION = "4.7.0"

#: Where NetBox binds the context manager the baselines are asserted through.
QUERY_COUNT_ASSERTION_SITES = ("utilities.testing.api", "utilities.testing.views")


def running_netbox_version() -> str:
    """Return the running NetBox release without any packaging suffix."""
    from netbox.settings import VERSION

    return VERSION.split("-", 1)[0]


def query_counts_are_comparable(running_version: str, *, update_mode: bool) -> bool:
    """Say whether the committed baselines describe *running_version*.

    Update mode always compares, so a re-record on a new release still writes the file
    instead of quietly recording nothing.
    """
    return update_mode or running_version == QUERY_COUNT_NETBOX_VERSION


@contextmanager
def _skip_query_count_assertion(test_case, name):  # noqa: ARG001 - matches the NetBox signature
    yield


@pytest.fixture(scope="session", autouse=True)
def _query_counts_only_on_the_recorded_netbox():
    """Drop the query-count assertions on any NetBox release but the recorded one.

    A count that differs on another release is a NetBox change, not a plugin
    regression, so failing there reports the wrong thing. Warn instead, so a skipped
    run never reads as a passing one.
    """
    running = running_netbox_version()
    if query_counts_are_comparable(running, update_mode=bool(os.environ.get("UPDATE_QUERY_COUNTS"))):
        yield
        return

    warnings.warn(
        f"Query-count baselines were recorded on NetBox {QUERY_COUNT_NETBOX_VERSION}; "
        f"this run is on {running}, so the query-count assertions are skipped.",
        stacklevel=1,
    )
    with ExitStack() as sites:
        for module in QUERY_COUNT_ASSERTION_SITES:
            sites.enter_context(patch(f"{module}.assert_expected_query_count", _skip_query_count_assertion))
        yield


def pytest_xdist_auto_num_workers(config) -> int:
    """Cap ``-n auto`` at the last worker that still gets private Redis databases.

    Without the cap, ``-n auto`` on a machine with more cores than
    ``MAX_PARALLEL_WORKERS`` starts workers that ``isolated_redis_databases`` rejects,
    and every one of them dies during startup. xdist keeps its own core detection, so
    ``auto`` and ``logical`` stay its decision.
    """
    from xdist.plugin import pytest_xdist_auto_num_workers as detected_num_workers

    return min(detected_num_workers(config), MAX_PARALLEL_WORKERS)


def pytest_configure(config) -> None:
    """Refuse a hand-picked worker count the isolation cannot serve.

    ``pytest_xdist_auto_num_workers`` only caps ``auto``. An explicit ``-n`` sails
    past it, and every worker above the ceiling gets no private Redis databases, so
    the run reports hundreds of setup errors that read like real test failures.
    ``auto`` and ``logical`` stay xdist's decision, capped by the hook.
    """
    requested = getattr(config.option, "numprocesses", None)
    if not isinstance(requested, int) or requested <= MAX_PARALLEL_WORKERS:
        return
    raise pytest.UsageError(
        f"-n {requested} exceeds the isolation limit: at most {MAX_PARALLEL_WORKERS} "
        "pytest workers get private PostgreSQL and Redis databases. Use -n auto."
    )


def pytest_sessionstart(session) -> None:
    """Require xdist before tests can touch shared NetBox services."""
    config = session.config
    if hasattr(config, "workerinput") or getattr(config.option, "numprocesses", None):
        return
    raise pytest.UsageError("The netbox_kea Django suite requires pytest-xdist with -n 1 or greater.")


def _test_database_name() -> str:
    """Return the caller-selected database name for this pytest worker."""
    name = os.environ.get("TEST_DB_NAME", "test_netbox_kea")
    if not name.startswith("test_"):
        raise RuntimeError("TEST_DB_NAME must start with test_.")
    return isolated_test_database_name(name, os.environ.get("PYTEST_XDIST_WORKER"))


def _prepopulate_url_resolver() -> None:
    """Best-effort: import every plugin urlconf so namespaces register early.

    Must run with DB access unblocked (see ``django_db_setup``): ``_populate()`` can
    import plugin urlconfs that touch the database, which pytest-django blocks during
    ``pytest_configure`` (raising ``RuntimeError: Database access not allowed``, so the
    bootstrap silently never ran). Failures are *logged* (not silently swallowed) so a
    real bootstrap error isn't hidden behind a later, confusing ``NoReverseMatch``.
    """
    try:
        import django

        django.setup()
        from django.urls import get_resolver

        get_resolver()._populate()
    except Exception:  # noqa: BLE001 — best effort; never block the test session
        logger.exception("Failed to pre-populate Django URL resolver")


def database_lifecycle(config) -> tuple[bool, bool]:
    """Return (keep the database at setup, tear it down at the end).

    Mirrors pytest-django, which reads the flag pair with two *different*
    expressions: ``keepdb`` is ``reuse_db and not create_db``, while the
    teardown branch tests ``reuse_db`` alone. Passing one combined value to both
    loses "rebuild and keep" (``--reuse-db --create-db``), which is how a
    database whose migrations were rebased is repaired without paying for the
    rebuild twice.
    """
    reuse_db = config.getvalue("reuse_db")
    create_db = config.getvalue("create_db")
    return (reuse_db and not create_db), (not reuse_db)


@pytest.fixture(scope="session")
def django_db_setup(request, django_test_environment, django_db_blocker):
    """Use a plugin-specific test DB name to avoid conflicts with other plugins
    running concurrently in a shared devcontainer (e.g. netbox-routing uses
    'test_netbox'; this fixture switches us to 'test_netbox_kea').

    Mirrors pytest-django's own ``django_db_setup`` semantics so ``--reuse-db``
    and ``--create-db`` keep working: the DB is kept between runs unless
    ``--create-db`` is given, and torn down at session end only when it wasn't
    reused (so re-runs stay fast).
    """
    from django.conf import settings
    from django.test.utils import setup_databases, teardown_databases

    settings.DATABASES["default"].setdefault("TEST", {})["NAME"] = _test_database_name()

    keepdb, teardown_at_end = database_lifecycle(request.config)
    verbosity = request.config.option.verbose

    with django_db_blocker.unblock():
        db_cfg = setup_databases(verbosity=verbosity, interactive=False, keepdb=keepdb)
        # Populate the URL resolver now that DB access is unblocked — importing some
        # plugin urlconfs touches the DB, so doing this in pytest_configure raised
        # "Database access not allowed". Runs once per session, before any DB test.
        _prepopulate_url_resolver()

    yield

    if teardown_at_end:
        with django_db_blocker.unblock():
            teardown_databases(db_cfg, verbosity=verbosity)


#: The plugin's own cache key prefixes. Both are keyed on a Server ID.
_PLUGIN_CACHE_PATTERNS = ("netbox_kea:subnet_catalogue:*", "netbox_kea:subnet_choices:*")


def _drop_plugin_cache_entries() -> None:
    """Delete every cache entry the plugin owns, for any Server ID.

    Scoped to the plugin's prefixes so NetBox's own cached state is untouched. Uses
    ``delete_pattern`` from django_redis, which NetBox requires; a backend without it
    fails loudly here rather than silently skipping the cleanup and letting the
    cross-test leak return.
    """
    from django.core.cache import cache

    if not hasattr(cache, "delete_pattern"):
        raise RuntimeError(
            "The cache backend has no delete_pattern(); netbox_kea test cache hygiene "
            f"needs it to clear {_PLUGIN_CACHE_PATTERNS}."
        )
    for pattern in _PLUGIN_CACHE_PATTERNS:
        cache.delete_pattern(pattern)


@pytest.fixture(autouse=True)
def _plugin_cache_hygiene():
    """Clear the plugin's cache entries around every test.

    The database rolls back per test and the cache backend does not, while test Server
    IDs are reused across tests, so an entry written under one test's Server ID would
    otherwise be served to a later test that stubbed different Kea responses. That
    produced intermittent failures on a different test each parallel run.

    Autouse so no test base has to opt in: the leak came back each time a new base
    created a Server and forgot to clear the cache itself.
    """
    _drop_plugin_cache_entries()
    yield
    _drop_plugin_cache_entries()
