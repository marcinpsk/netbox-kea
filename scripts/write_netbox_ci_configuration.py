#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Write the shared NetBox configuration used by CI jobs."""

from __future__ import annotations

import argparse
from pathlib import Path

_COMMON_CONFIGURATION = """import os

ALLOWED_HOSTS = ['*']
DATABASE = {
    'NAME': 'netbox',
    'USER': 'netbox',
    'PASSWORD': 'netbox',
    'HOST': 'localhost',
    'PORT': '',
    'CONN_MAX_AGE': 300,
    'ENGINE': 'django.db.backends.postgresql'
}
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


def main() -> None:
    """Write a NetBox configuration with the requested plugin list."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plugin", action="append", dest="plugins", required=True)
    arguments = parser.parse_args()

    arguments.output.write_text(
        f"{_COMMON_CONFIGURATION}PLUGINS = {arguments.plugins!r}\n"
        "PLUGINS_CONFIG = {'netbox_kea': {'kea_timeout': 30, 'lease_query_max_unpaged_leases': 1000}}\n"
    )


if __name__ == "__main__":
    main()
