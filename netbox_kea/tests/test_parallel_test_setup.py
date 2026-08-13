# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for parallel pytest worker isolation."""

import os

import pytest
from django.apps import apps
from netbox.registry import registry

from netbox_kea.tests.parallel import isolated_redis_databases, isolated_test_database_name


def test_plugin_is_registered_during_settings_initialization():
    """Register the plugin before NetBox builds its URL namespaces."""
    assert "netbox_kea" in registry["plugins"]["installed"]


def test_xdist_worker_gets_private_postgresql_and_redis_databases():
    """Assign one PostgreSQL database and two Redis databases to a worker."""
    assert isolated_test_database_name("test_netbox_kea", "gw3") == "test_netbox_kea_gw3"
    assert isolated_redis_databases("gw3") == (3, 11)


def test_serial_run_keeps_default_database_targets():
    """Keep the caller's targets when pytest does not use xdist."""
    assert isolated_test_database_name("test_netbox_kea", None) == "test_netbox_kea"
    assert isolated_redis_databases(None) == (0, 1)


def test_database_name_stays_within_postgresql_limit():
    """Keep a worker suffix when the base name reaches PostgreSQL's limit."""
    database_name = isolated_test_database_name(f"test_{'x' * 70}", "gw7")

    assert len(database_name) == 63
    assert database_name.endswith("_gw7")


def test_more_than_eight_workers_is_rejected():
    """Reject workers that cannot receive a private Redis database pair."""
    with pytest.raises(ValueError, match="At most 8 pytest workers are supported"):
        isolated_redis_databases("gw8")


def test_unknown_worker_id_is_rejected():
    """Reject worker identifiers outside pytest-xdist's supported shape."""
    with pytest.raises(ValueError, match="Unsupported pytest worker ID"):
        isolated_redis_databases("worker-1")


@pytest.mark.django_db
def test_active_worker_uses_private_database_targets(settings):
    """Apply the worker identity to the active Django database and Redis settings."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    tasks_database, cache_database = isolated_redis_databases(worker_id)

    assert settings.DATABASES["default"]["TEST"]["NAME"] == isolated_test_database_name(
        os.environ["TEST_DB_NAME"],
        worker_id,
    )
    assert settings.RQ_QUEUES["default"]["DB"] == tasks_database
    assert settings.CACHES["default"]["LOCATION"].endswith(f"/{cache_database}")
    assert apps.is_installed("netbox_kea")
