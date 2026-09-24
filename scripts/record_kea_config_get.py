#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Record config-get and subnet-list replies from a real Kea for the parser fixture tests.

Starts each Kea daemon image with the coverage configuration in
netbox_kea/tests/kea_recordings/, then writes the replies to dhcp4.json and dhcp6.json.
It also reads the Bison grammar of the same Kea release and writes the keys Kea accepts
on a Shared Network and on a Subnet to accepted-keys.json.
The Kea version is the Compose harness default, so both use the same Kea.

Usage: scripts/record_kea_config_get.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = REPO_ROOT / "netbox_kea" / "tests" / "kea_recordings"
COMPOSE_OVERRIDE = REPO_ROOT / "tests" / "docker" / "docker-compose.override.yml"
IMAGE = "docker.cloudsmith.io/isc/docker/kea-dhcp{family}:{version}"
GRAMMAR = "https://raw.githubusercontent.com/isc-projects/kea/Kea-{version}/src/bin/dhcp{family}/dhcp{family}_parser.yy"
HOST_PORT = {4: 18101, 6: 18103}
# The recorded Kea is local; a proxy from the environment must not see the request.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _run(tool: str, *args: str, check: bool = True) -> str:
    executable = shutil.which(tool)
    if executable is None:
        raise SystemExit(f"{tool} is required to record Kea replies")
    result = subprocess.run([executable, *args], check=check, capture_output=True, text=True)  # noqa: S603 - fixed argv, no shell
    return result.stdout


def _docker(*args: str, check: bool = True) -> None:
    _run("docker", *args, check=check)


def _harness_kea_version() -> str:
    match = re.search(r"kea-dhcp4:\$\{KEA_VERSION:-([^}]+)\}", COMPOSE_OVERRIDE.read_text())
    if match is None:
        raise SystemExit(f"No KEA_VERSION default found in {COMPOSE_OVERRIDE}")
    return match.group(1)


def _command(port: int, name: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=json.dumps({"command": name}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with _OPENER.open(request, timeout=5) as response:
        (reply,) = json.load(response)
    if reply.get("result") != 0:
        raise SystemExit(f"{name} failed: {reply}")
    return reply


def _ready(port: int) -> bool:
    try:
        _command(port, "version-get")
    except OSError:
        return False
    return True


def _wait_until_ready(port: int) -> None:
    deadline = time.monotonic() + 30
    while not _ready(port):
        if time.monotonic() > deadline:
            raise SystemExit(f"Kea on port {port} did not answer within 30 seconds")
        time.sleep(0.5)


def _record(family: int, version: str) -> None:
    port = HOST_PORT[family]
    container = f"netbox-kea-record-dhcp{family}"
    _docker("rm", "-f", container, check=False)
    _docker(
        "run", "-d", "--name", container,
        "-p", f"127.0.0.1:{port}:8000",
        "-v", f"{RECORDINGS / f'kea-dhcp{family}.conf'}:/etc/kea/kea-dhcp{family}.conf:ro",
        IMAGE.format(family=family, version=version),
        f"kea-dhcp{family}", "-X", "-c", f"/etc/kea/kea-dhcp{family}.conf",
    )  # fmt: skip
    try:
        _wait_until_ready(port)
        recording = {
            "kea-version": version,
            "config-get": _command(port, "config-get"),
            f"subnet{family}-list": _command(port, f"subnet{family}-list"),
        }
    finally:
        _docker("rm", "-f", container, check=False)
    (RECORDINGS / f"dhcp{family}.json").write_text(json.dumps(recording, indent=2, sort_keys=True) + "\n")


def _rule_keys(grammar: str, tokens: dict[str, str], rule: str) -> list[str]:
    """Return the key of every alternative of one ``*_param`` rule, except unknown_map_entry."""
    body = re.search(rf"^{rule}\s*:(.*?)^\s*;", grammar, re.MULTILINE | re.DOTALL)
    if body is None:
        raise SystemExit(f"No {rule} rule in the Kea grammar")
    keys = []
    for alternative in re.findall(r"[a-z0-9_]+", re.sub(r"//.*", "", body.group(1))):
        if alternative == "unknown_map_entry":
            continue
        token = re.search(rf"^{alternative}\s*:[^A-Z]*?([A-Z][A-Z0-9_]*)", grammar, re.MULTILINE | re.DOTALL)
        if token is None or token.group(1) not in tokens:
            raise SystemExit(f"Cannot map {rule} alternative {alternative} to a Kea key")
        keys.append(tokens[token.group(1)])
    return sorted(keys)


def _accepted_keys(family: int, version: str) -> dict[str, list[str]]:
    grammar = _run(
        "curl", "--fail", "--silent", "--show-error", "--location", GRAMMAR.format(version=version, family=family)
    )
    tokens = dict(re.findall(r'^\s+([A-Z0-9_]+)\s+"([^"]+)"\s*$', grammar, re.MULTILINE))
    return {
        "shared-networks": _rule_keys(grammar, tokens, "shared_network_param"),
        f"subnet{family}": _rule_keys(grammar, tokens, f"subnet{family}_param"),
    }


def main() -> None:
    """Record both families from the harness Kea version."""
    version = _harness_kea_version()
    accepted: dict[str, object] = {"kea-version": version}
    for family in (4, 6):
        _record(family, version)
        accepted[f"dhcp{family}"] = _accepted_keys(family, version)
    (RECORDINGS / "accepted-keys.json").write_text(json.dumps(accepted, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
