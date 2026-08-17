# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for parallel pytest worker isolation."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from django.apps import apps
from netbox.registry import registry

from netbox_kea.tests.conftest import pytest_xdist_auto_num_workers
from netbox_kea.tests.parallel import MAX_PARALLEL_WORKERS, isolated_redis_databases, isolated_test_database_name

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_plugin_is_registered_during_settings_initialization():
    """Register the plugin before NetBox builds its URL namespaces."""
    assert "netbox_kea" in registry["plugins"]["installed"]


def test_xdist_worker_gets_private_postgresql_and_redis_databases():
    """Assign one PostgreSQL database and two Redis databases to a worker."""
    assert isolated_test_database_name("test_netbox_kea", "gw3") == "test_netbox_kea_gw3"
    assert isolated_redis_databases("gw3") == (3, 11)


def test_serial_run_has_no_redis_database_targets():
    """Reject Redis targets without an xdist worker identity."""
    assert isolated_test_database_name("test_netbox_kea", None) == "test_netbox_kea"
    with pytest.raises(ValueError, match="requires an xdist worker ID"):
        isolated_redis_databases(None)


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


@pytest.mark.parametrize(
    ("detected_workers", "expected"),
    [("2", 2), (str(MAX_PARALLEL_WORKERS), MAX_PARALLEL_WORKERS), ("32", MAX_PARALLEL_WORKERS)],
)
def test_auto_worker_count_stops_at_the_private_database_ceiling(monkeypatch, pytestconfig, detected_workers, expected):
    """Cap `-n auto` at the last worker that still gets private Redis databases."""
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", detected_workers)

    assert pytest_xdist_auto_num_workers(pytestconfig) == expected
    isolated_redis_databases(f"gw{expected - 1}")  # the highest worker this count starts


def test_auto_worker_count_caps_the_machine_derived_count(monkeypatch, pytestconfig):
    """Cap the count xdist derives from the machine, not only an explicit override."""
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)

    assert 1 <= pytest_xdist_auto_num_workers(pytestconfig) <= MAX_PARALLEL_WORKERS


def test_auto_request_resolves_through_the_installed_hook():
    """Resolve `-n auto` to the capped count in a real pytest run.

    Calling the hook directly cannot prove that pytest loads this conftest before xdist
    resolves `-n auto`, so run pytest against this suite and read the count it settled
    on. The probe plugin stays outside the repository, where no file watcher sees it.
    """
    environment = os.environ.copy()
    environment["PYTEST_XDIST_AUTO_NUM_WORKERS"] = str(MAX_PARALLEL_WORKERS * 4)
    probe = "def pytest_collection_finish(session):\n    print(f'RESOLVED={session.config.option.numprocesses}')\n"

    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "worker_count_probe.py").write_text(probe)
        environment["PYTHONPATH"] = os.pathsep.join(
            [directory, *(str(Path(entry or Path.cwd()).resolve()) for entry in sys.path)]
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(Path(__file__).resolve()),
                "-n",
                "auto",
                "--collect-only",
                "-q",
                "-p",
                "worker_count_probe",
                "-p",
                "no:cacheprovider",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=300,
        )

    assert f"RESOLVED={MAX_PARALLEL_WORKERS}" in result.stdout, result.stdout + result.stderr


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
