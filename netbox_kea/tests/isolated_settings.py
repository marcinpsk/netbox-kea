# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""NetBox test settings with caller-selected PostgreSQL and Redis targets."""

import os

from netbox_kea.tests.parallel import isolated_redis_databases

_test_redis_host = os.environ["TEST_REDIS_HOST"]
if not _test_redis_host.strip():
    raise ValueError("TEST_REDIS_HOST must not be empty.")

_test_database_name = os.environ["TEST_DB_NAME"]
if not _test_database_name.startswith("test_"):
    raise ValueError("TEST_DB_NAME must start with 'test_'.")

_worker_id = os.environ.get("PYTEST_XDIST_WORKER")
if _worker_id is None:
    # The xdist controller loads Django settings but does not run tests.
    # pytest_sessionstart rejects a serial session before test collection.
    _tasks_redis_database, _cache_redis_database = 0, 1
else:
    _tasks_redis_database, _cache_redis_database = isolated_redis_databases(_worker_id)
os.environ["REDIS_HOST"] = _test_redis_host
os.environ["REDIS_CACHE_HOST"] = _test_redis_host
os.environ["REDIS_DATABASE"] = str(_tasks_redis_database)
os.environ["REDIS_CACHE_DATABASE"] = str(_cache_redis_database)

from netbox import configuration as _netbox_configuration  # noqa: E402

_plugins = list(getattr(_netbox_configuration, "PLUGINS", []))
if "netbox_kea" not in _plugins:
    _netbox_configuration.PLUGINS = [*_plugins, "netbox_kea"]
_plugins_config = dict(getattr(_netbox_configuration, "PLUGINS_CONFIG", {}))
_plugins_config.setdefault("netbox_kea", {"kea_timeout": 30})
_netbox_configuration.PLUGINS_CONFIG = _plugins_config
_netbox_configuration.API_TOKEN_PEPPERS = {0: "a" * 64}

from netbox.settings import *  # noqa: E402, F403

DATABASES["default"].setdefault("TEST", {})["NAME"] = _test_database_name  # noqa: F405
