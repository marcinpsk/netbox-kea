# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-reservation-command-outside-adapter. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).


def bad_reads_a_reservation_page(client):
    # ruleid: kea-reservation-command-outside-adapter
    resp = client.command("reservation-get-page", service=["dhcp4"], arguments={"subnet-id": 1})
    return resp


def bad_writes_a_reservation(client, arguments):
    # ruleid: kea-reservation-command-outside-adapter
    client.command("reservation-add", service=["dhcp6"], arguments=arguments)


def bad_reads_one_reservation_by_hostname(client):
    # ruleid: kea-reservation-command-outside-adapter
    return client.command("reservation-get-by-hostname", service=["dhcp4"])


def bad_names_the_command_by_keyword(client):
    # ruleid: kea-reservation-command-outside-adapter
    return client.command(command="reservation-get", service=["dhcp4"])


def ok_reads_the_configuration(client):
    # ok: kea-reservation-command-outside-adapter
    return client.command("config-get", service=["dhcp4"])


def ok_reads_leases(client):
    # ok: kea-reservation-command-outside-adapter
    return client.command("lease4-get-all", service=["dhcp4"])


def ok_consumes_the_typed_adapter(client, family, scope, catalogue):
    # ok: kea-reservation-command-outside-adapter
    return client.reservation_snapshot(family, scope, catalogue)
