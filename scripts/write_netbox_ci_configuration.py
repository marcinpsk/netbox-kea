#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Write the shared NetBox configuration used by CI jobs."""

from __future__ import annotations

import argparse
from pathlib import Path

_DATABASE = """{
    'NAME': 'netbox',
    'USER': 'netbox',
    'PASSWORD': 'netbox',
    'HOST': 'localhost',
    'PORT': '',
    'CONN_MAX_AGE': 300,
    'ENGINE': 'django.db.backends.postgresql'
}"""

_COMMON_CONFIGURATION = """import os

ALLOWED_HOSTS = ['*']
REDIS = {
    'tasks': {
        'HOST': os.environ.get('REDIS_HOST', 'localhost'),
        'PORT': 6379,
        'DATABASE': int(os.environ.get('REDIS_DATABASE', '0')),
    },
    'caching': {
        'HOST': os.environ.get('REDIS_CACHE_HOST', 'localhost'),
        'PORT': 6379,
        'DATABASE': int(os.environ.get('REDIS_CACHE_DATABASE', '1')),
    },
}
SECRET_KEY = 'ci-test-secret-key-not-for-production-1234567890123456'
API_TOKEN_PEPPERS = {0: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}
"""

# netbox-branching refuses to start without these two settings, and NetBox refuses DATABASE beside DATABASES.
_BRANCHING_DATABASES = f"""from netbox_branching.utilities import DynamicSchemaDict

DATABASES = DynamicSchemaDict({{'default': {_DATABASE}}})
DATABASE_ROUTERS = ['netbox_branching.database.BranchAwareRouter']
"""

_BRANCHING_PLUGIN = "netbox_branching"


def main() -> None:
    """Write a NetBox configuration with the requested plugin list."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plugin", action="append", dest="plugins", required=True)
    parser.add_argument(
        "--branching",
        action="store_true",
        help=f"add {_BRANCHING_PLUGIN} as the last plugin, with the database settings it requires",
    )
    arguments = parser.parse_args()
    if _BRANCHING_PLUGIN in arguments.plugins:
        parser.error(f"use --branching instead of --plugin {_BRANCHING_PLUGIN}")

    databases, plugins = f"DATABASE = {_DATABASE}\n", arguments.plugins
    if arguments.branching:
        databases, plugins = _BRANCHING_DATABASES, [*plugins, _BRANCHING_PLUGIN]
    arguments.output.write_text(
        f"{_COMMON_CONFIGURATION}{databases}PLUGINS = {plugins!r}\n"
        "PLUGINS_CONFIG = {'netbox_kea': {'kea_timeout': 30, 'lease_query_max_unpaged_leases': 1000}}\n"
    )


if __name__ == "__main__":
    main()
