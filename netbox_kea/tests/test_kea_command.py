# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Kea transport takes a KeaCommand member and a target family, never a string (guard 3, ADR 0007)."""

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from django.test import override_settings

from netbox_kea.branching import BranchActive, BranchBinding
from netbox_kea.kea import KeaClient, KeaCommand
from netbox_kea.tests.kea_stub import kea_client, stub_kea
from netbox_kea.tests.kea_wire_discipline import WIRE_COMMANDS
from netbox_kea.tests.utils import _PLUGINS_CONFIG

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

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
    assert {member.value for member in KeaCommand if not member.is_write} == READ_COMMANDS


def test_the_write_members_are_the_pinned_set():
    assert {member.value for member in KeaCommand if member.is_write} == WRITE_COMMANDS


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
    client = kea_client(url="https://kea.example.invalid/", send_service=send_service)
    with stub_kea({"config-get": {"result": 0}}) as kea:
        client.command(KeaCommand.CONFIG_GET, target, arguments={"a": 1})

    assert kea.requests == [body]


def test_a_command_without_arguments_sends_no_arguments_key():
    client = kea_client(url="https://kea.example.invalid/")
    with stub_kea({"status-get": {"result": 0}}) as kea:
        client.command(KeaCommand.STATUS_GET, None)

    assert kea.requests == [{"command": "status-get"}]


def test_a_string_command_is_a_type_error_before_any_send():
    client = kea_client(url="https://kea.example.invalid/")
    with stub_kea({"config-get": {"result": 0}}) as kea, pytest.raises(TypeError, match="KeaCommand"):
        client.command("config-get", 4)  # type: ignore[arg-type]

    assert kea.requests == []


@pytest.mark.parametrize("target", [0, 5, "dhcp4", True])
def test_a_target_that_is_not_a_family_is_refused_before_any_send(target):
    client = kea_client(url="https://kea.example.invalid/")
    with stub_kea({"config-get": {"result": 0}}) as kea, pytest.raises(ValueError, match="target"):
        client.command(KeaCommand.CONFIG_GET, target)

    assert kea.requests == []


# The binding keeps what active_branch() returned; the refusal only needs it to be set.
_BRANCH = "a branch"
_WRITES = [member for member in KeaCommand if member.is_write]


def _bound_client() -> KeaClient:
    return KeaClient(url="https://kea.example.invalid/", write_guard=BranchBinding(_BRANCH))


@pytest.mark.parametrize("command", _WRITES, ids=lambda member: member.value)
def test_a_branch_bound_client_refuses_every_write_before_any_send(command):
    with stub_kea({}) as kea, pytest.raises(BranchActive) as refused:
        _bound_client().command(command, 4)

    assert refused.value.branch == _BRANCH
    assert kea.requests == []


@pytest.mark.parametrize("command", _WRITES, ids=lambda member: member.value)
def test_a_clone_of_a_branch_bound_client_refuses_every_write_in_a_worker_thread(command):
    clone = _bound_client().clone()
    with stub_kea({}) as kea, ThreadPoolExecutor(max_workers=1) as pool:
        refused = pool.submit(clone.command, command, 4).exception()

    assert isinstance(refused, BranchActive)
    assert kea.requests == []


def test_a_branch_bound_client_sends_a_read():
    with stub_kea({"config-get": {"result": 0}}) as kea:
        _bound_client().command(KeaCommand.CONFIG_GET, 4)

    assert kea.commands() == ["config-get"]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
def test_get_client_on_main_binds_no_branch_and_sends_a_write():
    from netbox_kea.models import Server

    client = Server(name="main", ca_url="https://kea.example.invalid/").get_client(version=4)
    with stub_kea({"lease4-del": {"result": 0}}) as kea:
        client.command(KeaCommand.LEASE4_DEL, 4, arguments={"ip-address": "192.0.2.1"})

    assert client.write_guard == BranchBinding(None)
    assert client.clone().write_guard is client.write_guard
    assert kea.commands() == ["lease4-del"]


# Guard 3: the only HTTP send, and the only Kea client construction, of the runtime package.

_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request", "send"})
_OTHER_HTTP_MODULES = ("http.client", "httpx", "aiohttp", "pycurl", "socket", "urllib.request", "urllib3")


def _runtime_modules() -> list[tuple[str, ast.Module]]:
    return [
        (path.relative_to(PACKAGE_ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if path.relative_to(PACKAGE_ROOT).parts[0] not in {"tests", "migrations"}
    ]


def _scoped_calls(tree: ast.Module) -> list[tuple[str, ast.Call]]:
    """Return each call in *tree* with the qualified name of the function or class that holds it."""
    found: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, (*scope, child.name))
                continue
            if isinstance(child, ast.Call):
                found.append((".".join(scope) or "<module>", child))
            visit(child, scope)

    visit(tree, ())
    return found


def _is_name(node: ast.expr, name: str) -> bool:
    return (isinstance(node, ast.Name) and node.id == name) or (isinstance(node, ast.Attribute) and node.attr == name)


def test_the_runtime_package_has_one_http_send_in_kea_client_command():
    sends, requests_calls = [], []
    for rel, tree in _runtime_modules():
        for scope, call in _scoped_calls(tree):
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr in _HTTP_VERBS and _is_name(func.value, "_session"):
                sends.append(f"{rel}::{scope}")
            if isinstance(func.value, ast.Name) and func.value.id == "requests":
                requests_calls.append(f"{rel}::{scope}: requests.{func.attr}")

    assert sends == ["kea.py::KeaClient.command"]
    assert requests_calls == [
        "kea.py::KeaClient.__init__: requests.Session",
        "kea.py::KeaClient.clone: requests.Session",
    ]


def test_the_runtime_package_imports_no_other_http_client():
    imported = [
        f"{rel}: {name}"
        for rel, tree in _runtime_modules()
        for node in ast.walk(tree)
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom) and node.level == 0
            else []
        )
        if any(name == module or name.startswith(f"{module}.") for module in _OTHER_HTTP_MODULES)
    ]

    assert imported == ["config_write.py: urllib3.exceptions"]


def _kea_client_builds(modules: list[tuple[str, ast.Module]]) -> dict[str, str]:
    """Return each KeaClient construction site, with the write guard expression it passes, or "" for none."""
    return {
        f"{rel}::{scope}": next(
            (ast.unparse(keyword.value) for keyword in call.keywords if keyword.arg == "write_guard"), ""
        )
        for rel, tree in modules
        for scope, call in _scoped_calls(tree)
        if _is_name(call.func, "KeaClient")
    }


def test_only_server_get_client_builds_a_kea_client_and_it_passes_the_branch_binding():
    assert _kea_client_builds(_runtime_modules()) == {"models.py::Server.get_client": "branching.bind()"}


@pytest.mark.parametrize("guard", ["write_guard=None", "write_guard=no_guard()", "**options"])
def test_the_build_site_scan_sees_a_guard_other_than_the_branch_binding(guard):
    source = f"class Server:\n    def get_client(self):\n        return kea_client(url, {guard})\n"

    assert _kea_client_builds([("models.py", ast.parse(source))]) != {
        "models.py::Server.get_client": "branching.bind()"
    }


def test_a_kea_client_without_a_write_guard_is_a_type_error():
    with pytest.raises(TypeError, match="write_guard"):
        KeaClient(url="https://kea.example.invalid/")  # type: ignore[call-arg]
