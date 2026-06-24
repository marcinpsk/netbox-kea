# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-get-client-missing-version. Intentionally contains
# rule-violating code; excluded from ruff (see pyproject [tool.ruff] exclude).


def fetch(server):
    # ruleid: kea-get-client-missing-version
    client = server.get_client()
    return client


def fetch_versioned(server):
    # ok: kea-get-client-missing-version
    client = server.get_client(version=4)
    return client


def fetch_versioned_var(server, version):
    # ok: kea-get-client-missing-version
    client = server.get_client(version=version)
    return client


def nested(server, helper):
    # ruleid: kea-get-client-missing-version
    return helper(server.get_client())
