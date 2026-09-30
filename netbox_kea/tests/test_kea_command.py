# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Kea transport takes a KeaCommand member and a target family, never a string (guard 3, ADR 0007)."""

import pytest

from netbox_kea.kea import KeaClient, KeaCommand
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.kea_wire_discipline import WIRE_COMMANDS

# Written here, independent of the enum: a change to either set needs a review of the branch refusal.
READ_COMMANDS = frozenset(
    {
        "config-get",
        "config-test",
        "list-commands",
        "status-get",
        "version-get",
        "reservation-get",
        "reservation-get-by-hostname",
        "reservation-get-page",
        "lease4-get",
        "lease6-get",
        "lease4-get-all",
        "lease6-get-all",
        "lease4-get-by-client-id",
        "lease4-get-by-hostname",
        "lease6-get-by-hostname",
        "lease4-get-by-hw-address",
        "lease6-get-by-duid",
        "lease4-get-by-state",
        "lease6-get-by-state",
        "lease4-get-page",
        "lease6-get-page",
        "stat-lease4-get",
        "stat-lease6-get",
        "subnet4-get",
        "subnet6-get",
        "subnet4-list",
        "subnet6-list",
        "network4-get",
        "network6-get",
    }
)
WRITE_COMMANDS = frozenset(
    {
        "config-set",
        "config-write",
        "dhcp-disable",
        "dhcp-enable",
        "reservation-add",
        "reservation-del",
        "reservation-update",
        "lease4-add",
        "lease6-add",
        "lease4-del",
        "lease6-del",
        "lease4-update",
        "lease6-update",
        "lease4-wipe",
        "lease6-wipe",
        "subnet4-add",
        "subnet6-add",
        "subnet4-del",
        "subnet6-del",
        "subnet4-update",
        "subnet6-update",
        "subnet4-delta-add",
        "subnet6-delta-add",
        "subnet4-delta-del",
        "subnet6-delta-del",
        "network4-add",
        "network6-add",
        "network4-del",
        "network6-del",
        "network4-subnet-add",
        "network6-subnet-add",
        "network4-subnet-del",
        "network6-subnet-del",
    }
)


def test_the_read_members_are_the_pinned_set():
    assert {member.value for member in KeaCommand if member.kind == "read"} == READ_COMMANDS


def test_the_write_members_are_the_pinned_set():
    assert {member.value for member in KeaCommand if member.kind == "write"} == WRITE_COMMANDS


def test_every_member_is_a_read_or_a_write():
    assert {member.kind for member in KeaCommand} == {"read", "write"}


def test_every_member_is_a_command_the_harness_kea_lists():
    assert {member.value for member in KeaCommand} <= WIRE_COMMANDS


@pytest.mark.parametrize(
    ("target", "send_service", "body"),
    [
        (4, True, {"command": "config-get", "service": ["dhcp4"], "arguments": {"a": 1}}),
        (6, True, {"command": "config-get", "service": ["dhcp6"], "arguments": {"a": 1}}),
        (4, False, {"command": "config-get", "arguments": {"a": 1}}),
        (6, False, {"command": "config-get", "arguments": {"a": 1}}),
        (None, True, {"command": "config-get", "arguments": {"a": 1}}),
        (None, False, {"command": "config-get", "arguments": {"a": 1}}),
    ],
)
def test_the_wire_payload_of_each_target(target, send_service, body):
    client = KeaClient(url="https://kea.example.invalid/", send_service=send_service)
    with stub_kea({"config-get": {"result": 0}}) as kea:
        client.command(KeaCommand.CONFIG_GET, target, arguments={"a": 1})

    assert kea.requests == [body]


def test_a_command_without_arguments_sends_no_arguments_key():
    client = KeaClient(url="https://kea.example.invalid/")
    with stub_kea({"status-get": {"result": 0}}) as kea:
        client.command(KeaCommand.STATUS_GET, None)

    assert kea.requests == [{"command": "status-get"}]


def test_a_string_command_is_a_type_error_before_any_send():
    client = KeaClient(url="https://kea.example.invalid/")
    with stub_kea({"config-get": {"result": 0}}) as kea, pytest.raises(TypeError, match="KeaCommand"):
        client.command("config-get", 4)  # type: ignore[arg-type]

    assert kea.requests == []


@pytest.mark.parametrize("target", [0, 5, "dhcp4", True])
def test_a_target_that_is_not_a_family_is_refused_before_any_send(target):
    client = KeaClient(url="https://kea.example.invalid/")
    with stub_kea({"config-get": {"result": 0}}) as kea, pytest.raises(ValueError, match="target"):
        client.command(KeaCommand.CONFIG_GET, target)

    assert kea.requests == []
