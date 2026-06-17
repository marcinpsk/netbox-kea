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

import pytest

logger = logging.getLogger(__name__)


def _prepopulate_url_resolver() -> None:
    """Best-effort: import every plugin urlconf so namespaces register early.

    Never blocks collection. Failures are *logged* (not silently swallowed) so a
    real bootstrap error isn't hidden behind a later, confusing ``NoReverseMatch``.
    """
    try:
        import django

        django.setup()
        from django.urls import get_resolver

        get_resolver()._populate()
    except Exception:  # noqa: BLE001 — best effort; never block collection
        logger.exception("Failed to pre-populate Django URL resolver during pytest_configure")


def pytest_configure(config):  # noqa: D103
    _prepopulate_url_resolver()


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

    settings.DATABASES["default"].setdefault("TEST", {})["NAME"] = "test_netbox_kea"

    keepdb = request.config.getvalue("reuse_db") and not request.config.getvalue("create_db")
    verbosity = request.config.option.verbose

    with django_db_blocker.unblock():
        db_cfg = setup_databases(verbosity=verbosity, interactive=False, keepdb=keepdb)

    yield

    if not keepdb:
        with django_db_blocker.unblock():
            teardown_databases(db_cfg, verbosity=verbosity)
