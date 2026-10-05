# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-http-call-outside-client. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
import requests

from netbox_kea.kea import KeaCommand


def bad_session(url, body):
    # ruleid: kea-http-call-outside-client
    session = requests.Session()
    return session.post(url, json=body, timeout=5)


def bad_lowercase_session(url, body):
    # ruleid: kea-http-call-outside-client
    return requests.session().post(url, json=body, timeout=5)


def bad_post(url, body):
    # ruleid: kea-http-call-outside-client
    return requests.post(url, json=body, timeout=5)


def bad_get(url):
    # ruleid: kea-http-call-outside-client
    return requests.get(url, timeout=5)


def bad_request(url, body):
    # ruleid: kea-http-call-outside-client
    return requests.request("POST", url, json=body, timeout=5)


def good_client(server, family):
    # ok: kea-http-call-outside-client
    return server.get_client(version=family).command(KeaCommand.VERSION_GET, family)


def good_exception_handler(server, family):
    try:
        return server.get_client(version=family).command(KeaCommand.VERSION_GET, family)
    # ok: kea-http-call-outside-client
    except requests.RequestException:
        return None
