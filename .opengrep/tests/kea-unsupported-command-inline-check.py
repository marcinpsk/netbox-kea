# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-unsupported-command-inline-check. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
from netbox_kea.kea import KeaException


def bad_get(client):
    try:
        client.command("subnet4-list")
    except KeaException as exc:
        # ruleid: kea-unsupported-command-inline-check
        if exc.response.get("result") == 2:
            return None
        raise


def bad_index(exc):
    # ruleid: kea-unsupported-command-inline-check
    return exc.response["result"] == 2


def bad_isinstance_guard(exc):
    # ruleid: kea-unsupported-command-inline-check
    return isinstance(exc.response, dict) and exc.response.get("result") == 2


def bad_inverse(exc, suffix):
    # ruleid: kea-unsupported-command-inline-check
    if suffix != "-by-state" or exc.response.get("result") != 2:
        raise exc


def bad_local_variable(exc):
    result = exc.response.get("result")
    # ruleid: kea-unsupported-command-inline-check
    if result == 2:
        return None
    raise exc


def bad_local_variable_index(exc):
    result = exc.response["result"]
    # ruleid: kea-unsupported-command-inline-check
    return result == 2


def ok_local_variable_reassigned(exc, other):
    result = exc.response.get("result")
    result = other()
    # ok: kea-unsupported-command-inline-check
    return result == 2


def ok_local_variable_other_code(exc):
    result = exc.response.get("result")
    # ok: kea-unsupported-command-inline-check
    return result == 3


def ok_property(client):
    try:
        client.command("subnet4-list")
    except KeaException as exc:
        # ok: kea-unsupported-command-inline-check
        if exc.unsupported_command:
            return None
        raise


def ok_other_result(exc):
    # ok: kea-unsupported-command-inline-check
    return exc.response.get("result") == 3


def ok_raw_reply(resp):
    # ok: kea-unsupported-command-inline-check
    return resp[0].get("result") == 2


class KeaException(Exception):
    @property
    def unsupported_command(self):
        # ok: kea-unsupported-command-inline-check
        return self.response.get("result") == 2
