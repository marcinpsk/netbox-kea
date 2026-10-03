# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-sync-except-request-exception-not-oserror. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
import requests

from netbox_kea.kea import KeaException


def bad_tuple(client, report):
    # ruleid: kea-sync-except-request-exception-not-oserror
    try:
        return client.lease_get_all(version=4)
    except (KeaException, requests.RequestException, ValueError, RuntimeError) as exc:
        report.fail(exc)
        return None


def bad_single(client):
    # ruleid: kea-sync-except-request-exception-not-oserror
    try:
        return client.lease_get_all(version=4)
    except requests.RequestException:
        return None


def good_oserror(client, report):
    # ok: kea-sync-except-request-exception-not-oserror
    try:
        return client.lease_get_all(version=4)
    except (KeaException, OSError, ValueError, RuntimeError) as exc:
        report.fail(exc)
        return None
