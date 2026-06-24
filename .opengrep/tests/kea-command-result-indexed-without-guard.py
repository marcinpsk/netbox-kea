# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-command-result-indexed-without-guard. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).


def bad_chained(client):
    # ruleid: kea-command-result-indexed-without-guard
    args = client.command("lease4-get-all", service=["dhcp4"])[0]["arguments"]
    return args


def bad_simple(client):
    # ruleid: kea-command-result-indexed-without-guard
    first = client.command("status-get")[0]
    return first


def ok_guarded(client):
    resp = client.command("status-get")
    if not resp or not isinstance(resp[0], dict):
        raise RuntimeError("malformed Kea response")
    # ok: kea-command-result-indexed-without-guard
    return resp[0]["arguments"]
