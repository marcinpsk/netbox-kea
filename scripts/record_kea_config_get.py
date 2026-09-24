#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Record config-get and subnet-list replies from a real Kea for the parser fixture tests.

Starts each Kea daemon image with the coverage configuration in
netbox_kea/tests/kea_recordings/, then writes the replies to dhcp4.json and dhcp6.json.
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
HOST_PORT = {4: 18101, 6: 18103}
# The recorded Kea is local; a proxy from the environment must not see the request.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _docker(*args: str, check: bool = True) -> None:
    executable = shutil.which("docker")
    if executable is None:
        raise SystemExit("docker is required to record Kea replies")
    subprocess.run([executable, *args], check=check, capture_output=True)  # noqa: S603 - fixed argv, no shell


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


def main() -> None:
    """Record both families from the harness Kea version."""
    version = _harness_kea_version()
    for family in (4, 6):
        _record(family, version)


if __name__ == "__main__":
    main()
