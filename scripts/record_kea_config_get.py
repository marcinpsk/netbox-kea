#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Record config-get and subnet-list replies from a real Kea for the parser fixture tests.

Starts each Kea daemon image with the coverage configuration in
netbox_kea/tests/kea_recordings/, then writes the replies to dhcp4.json and dhcp6.json.
The list-commands reply is the set of command names the tests accept, so the coverage
configurations load the same hook libraries as the Compose harness.
It also reads the keyword tables that Kea's config-test and config-set check in the same
release, and writes the keys Kea accepts on a Shared Network and on a Subnet to
accepted-keys.json.
The same daemons record lease replies: every lease state, an infinite lifetime, IPv6
delegated prefixes, exact lookups, pages, and the lease changes that Kea refuses.
The Kea version is the Compose harness default, so both use the same Kea.

Kea 3.2 has no Control Agent, so control-agent.json comes from the last Kea release series
that has one. The script installs that agent from ISC's signed package repository into the
daemon image of the same build, points it at control sockets with no daemon behind them, and
records its forwarding failures.

Usage: scripts/record_kea_config_get.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = REPO_ROOT / "netbox_kea" / "tests" / "kea_recordings"
COMPOSE_OVERRIDE = REPO_ROOT / "tests" / "docker" / "docker-compose.override.yml"
IMAGE = "docker.cloudsmith.io/isc/docker/kea-dhcp{family}:{version}"
KEYWORD_TABLES = (
    "https://raw.githubusercontent.com/isc-projects/kea/Kea-{version}/src/lib/dhcpsrv/parsers/simple_parser{family}.cc"
)
HOST_PORT = {4: 18101, 6: 18103}
CONTROL_AGENT_VERSION = "3.0.3"
CONTROL_AGENT_PORT = 18105
# A server that accepts the connection and then stops sending must not block the recording.
DOWNLOAD_SECONDS = 300
# Longer than DOWNLOAD_SECONDS, so curl ends a stalled download first; `docker run` can pull an image.
COMMAND_SECONDS = 600
CONTROL_AGENT_REPOSITORY = "https://dl.cloudsmith.io/public/isc/kea-3-0/alpine/v{alpine}/main/x86_64/"
# The recorded Kea is local; a proxy from the environment must not see the request.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _constants():
    """Load the plugin constants without the plugin package, which needs NetBox."""
    spec = importlib.util.spec_from_file_location("netbox_kea_constants", REPO_ROOT / "netbox_kea" / "constants.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CONSTANTS = _constants()
INFINITE_LIFETIME = _CONSTANTS.INFINITE_LIFETIME
STATE = _CONSTANTS.LEASE_STATE_CODES
_NESTED_CONTEXT = {"site": {"rack": "r1", "slots": [1, 2]}}
_DUID = "00:01:00:01:2c:4f:00:01:aa:bb:cc:00:00:01"
# The Subnet with ID 10 in each coverage configuration holds every recorded lease.
LEASES: dict[int, list[dict]] = {
    4: [
        {
            "ip-address": "192.0.2.10", "subnet-id": 10, "pool-id": 1,
            "hw-address": "aa:bb:cc:00:00:10", "client-id": "01:aa:bb:cc:00:00:10",
            "valid-lft": 3600, "hostname": "host10.example.org", "fqdn-fwd": True, "fqdn-rev": True,
            "user-context": _NESTED_CONTEXT,
        },
        {"ip-address": "192.0.2.11", "subnet-id": 10, "hw-address": "aa:bb:cc:00:00:11", "valid-lft": INFINITE_LIFETIME},
        {"ip-address": "192.0.2.12", "subnet-id": 10, "hw-address": "", "valid-lft": 86400, "state": STATE["declined"]},
        {"ip-address": "192.0.2.13", "subnet-id": 10, "hw-address": "aa:bb:cc:00:00:13", "state": STATE["expired-reclaimed"]},
        {"ip-address": "192.0.2.14", "subnet-id": 10, "hw-address": "aa:bb:cc:00:00:14", "state": STATE["released"]},
        {"ip-address": "192.0.2.15", "subnet-id": 10, "hw-address": "", "client-id": "ff:00:00:00:15:00:02"},
    ],
    6: [
        {
            "ip-address": "2001:db8:1::10", "subnet-id": 10, "pool-id": 1, "duid": _DUID, "iaid": 1,
            "hw-address": "aa:bb:cc:00:00:01", "valid-lft": 3600, "preferred-lft": 1800,
            "hostname": "host10.example.org", "fqdn-fwd": True, "fqdn-rev": False, "user-context": _NESTED_CONTEXT,
        },
        {"ip-address": "2001:db8:1::11", "subnet-id": 10, "duid": _DUID, "iaid": 2, "state": STATE["registered"]},
        {"ip-address": "2001:db8:1::12", "subnet-id": 10, "duid": "00:00:00", "iaid": 0, "state": STATE["declined"], "preferred-lft": 0},
        {"ip-address": "2001:db8:1::13", "subnet-id": 10, "duid": _DUID, "iaid": 3, "state": STATE["expired-reclaimed"]},
        {"ip-address": "2001:db8:1::14", "subnet-id": 10, "duid": _DUID, "iaid": 4, "state": STATE["released"]},
        {
            "ip-address": "2001:db8:1::15", "subnet-id": 10, "duid": _DUID, "iaid": 5,
            "valid-lft": INFINITE_LIFETIME, "preferred-lft": INFINITE_LIFETIME,
        },
        {"ip-address": "2001:db8:100:100::", "type": "IA_PD", "prefix-len": 56, "subnet-id": 10, "duid": _DUID, "iaid": 6},
        {
            "ip-address": "2001:db8:100:200::", "type": "IA_PD", "prefix-len": 56, "subnet-id": 10,
            "duid": _DUID, "iaid": 7, "valid-lft": INFINITE_LIFETIME, "preferred-lft": INFINITE_LIFETIME,
        },
        {
            "ip-address": "2001:db8:100:300::", "type": "IA_PD", "prefix-len": 56, "subnet-id": 10,
            "duid": _DUID, "iaid": 8, "state": STATE["registered"],
        },
        {
            "ip-address": "2001:db8:100:400::", "type": "IA_PD", "prefix-len": 56, "subnet-id": 10,
            "duid": _DUID, "iaid": 9, "state": STATE["released"],
        },
    ],
}  # fmt: skip
# Six DHCPv4 leases end on an empty page; ten DHCPv6 leases end on a short page.
LEASE_PAGE_LIMIT = {4: 3, 6: 4}
# Each refusal is evidence for a lease value or edit rule; the key names the rule.
REFUSED_LEASE_CHANGES: dict[int, dict[str, tuple[str, dict]]] = {
    4: {
        "missing-hw-address": ("lease4-add", {"ip-address": "192.0.2.20", "subnet-id": 10}),
        "registered-state": (
            "lease4-update", {"ip-address": "192.0.2.10", "subnet-id": 10, "hw-address": "aa:bb:cc:00:00:10", "state": STATE["registered"]},
        ),
        "fqdn-without-hostname": (
            "lease4-update",
            {"ip-address": "192.0.2.10", "subnet-id": 10, "hw-address": "aa:bb:cc:00:00:10", "hostname": "", "fqdn-fwd": True},
        ),
    },
    6: {
        "temporary-address-kind": (
            "lease6-add", {"ip-address": "2001:db8:1::20", "type": "IA_TA", "subnet-id": 10, "duid": _DUID, "iaid": 20},
        ),
        "declined-delegated-prefix": (
            "lease6-add",
            {"ip-address": "2001:db8:100:500::", "type": "IA_PD", "prefix-len": 56, "subnet-id": 10,
             "duid": _DUID, "iaid": 21, "state": STATE["declined"]},
        ),
        "non-canonical-delegated-prefix": (
            "lease6-add",
            {"ip-address": "2001:db8:100:501::", "type": "IA_PD", "prefix-len": 56, "subnet-id": 10,
             "duid": _DUID, "iaid": 22},
        ),
    },
}  # fmt: skip


def _run(tool: str, *args: str, check: bool = True) -> str:
    executable = shutil.which(tool)
    if executable is None:
        raise SystemExit(f"{tool} is required to record Kea replies")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [executable, *args], check=check, capture_output=True, text=True, timeout=COMMAND_SECONDS
    )
    return result.stdout


def _docker(*args: str, check: bool = True) -> None:
    _run("docker", *args, check=check)


def _harness_kea_version() -> str:
    match = re.search(r"kea-dhcp4:\$\{KEA_VERSION:-([^}]+)\}", COMPOSE_OVERRIDE.read_text())
    if match is None:
        raise SystemExit(f"No KEA_VERSION default found in {COMPOSE_OVERRIDE}")
    return match.group(1)


def _command(
    port: int,
    name: str,
    arguments: dict | None = None,
    *,
    service: str | None = None,
    result: int | tuple[int, ...] = 0,
) -> dict:
    body: dict = {"command": name}
    if service is not None:
        body["service"] = [service]
    if arguments is not None:
        body["arguments"] = arguments
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with _OPENER.open(request, timeout=5) as response:
        (reply,) = json.load(response)
    if reply.get("result") not in (result if isinstance(result, tuple) else (result,)):
        raise SystemExit(f"{name} returned an unexpected result: {reply}")
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


def _lease_pages(port: int, family: int) -> list[dict]:
    pages = []
    cursor = "start"
    while True:
        page = _command(
            port, f"lease{family}-get-page", {"from": cursor, "limit": LEASE_PAGE_LIMIT[family]}, result=(0, 3)
        )
        pages.append(page)
        if page["result"] == 3 or page["arguments"]["count"] < LEASE_PAGE_LIMIT[family]:
            return pages
        cursor = page["arguments"]["leases"][-1]["ip-address"]


def _renewal(port: int, family: int) -> dict:
    """Record that an update without ``expire`` renews the lease, and that ``expire`` keeps its time."""
    if family == 6:
        return {}
    lease = {"ip-address": "192.0.2.30", "subnet-id": 10, "hw-address": "aa:bb:cc:00:00:30", "valid-lft": 3600}
    _command(port, "lease4-add", {**lease, "expire": 2_000_000_000})
    fresh = _command(port, "lease4-get", {"ip-address": "192.0.2.30"})
    recorded = {"before": fresh}
    with_expire = {**fresh["arguments"], "expire": fresh["arguments"]["cltt"] + fresh["arguments"]["valid-lft"]}
    _command(port, "lease4-update", with_expire)
    recorded["after-update-with-expire"] = _command(port, "lease4-get", {"ip-address": "192.0.2.30"})
    _command(port, "lease4-update", fresh["arguments"])
    recorded["after-update-without-expire"] = _command(port, "lease4-get", {"ip-address": "192.0.2.30"})
    return {"lease4-update": recorded}


def _record_leases(port: int, family: int) -> dict:
    for lease in LEASES[family]:
        _command(port, f"lease{family}-add", lease)
    present = LEASES[family][0]["ip-address"]
    absent = "192.0.2.99" if family == 4 else "2001:db8:1::99"
    exact = {
        "present": _command(port, f"lease{family}-get", {"ip-address": present}),
        "absent": _command(port, f"lease{family}-get", {"ip-address": absent}, result=3),
    }
    if family == 6:
        prefix = "2001:db8:100:100::"
        exact["delegated-prefix"] = _command(port, "lease6-get", {"ip-address": prefix, "type": "IA_PD"})
        exact["delegated-prefix-as-address"] = _command(port, "lease6-get", {"ip-address": prefix}, result=3)
    refused = {
        rule: _command(port, command, arguments, result=1)
        for rule, (command, arguments) in REFUSED_LEASE_CHANGES[family].items()
    }
    return {
        f"lease{family}-get-all": _command(port, f"lease{family}-get-all"),
        f"lease{family}-get-page": _lease_pages(port, family),
        f"lease{family}-get": exact,
        "refused": refused,
        **_renewal(port, family),
    }


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
        configuration = json.loads((RECORDINGS / f"kea-dhcp{family}.conf").read_text())
        network = configuration[f"Dhcp{family}"]["shared-networks"][0]["name"]
        recording = {
            "kea-version": version,
            "config-get": _command(port, "config-get"),
            "list-commands": _command(port, "list-commands"),
            f"subnet{family}-list": _command(port, f"subnet{family}-list"),
            f"network{family}-get": {
                "present": _command(port, f"network{family}-get", {"name": network}),
                "absent": _command(port, f"network{family}-get", {"name": "absent"}, result=3),
            },
            "leases": _record_leases(port, family),
        }
    finally:
        _docker("rm", "-f", container, check=False)
    (RECORDINGS / f"dhcp{family}.json").write_text(json.dumps(recording, indent=2, sort_keys=True) + "\n")


def _table_keys(source: str, table: str) -> list[str]:
    """Return the keys of one ``SimpleKeywords`` table in a Kea simple_parser source."""
    body = re.search(rf"::{table} = \{{(.*?)^\}};", source, re.MULTILINE | re.DOTALL)
    if body is None:
        raise SystemExit(f"No {table} table in the Kea source")
    keys = re.findall(r'^\s*\{\s*"([^"]+)",\s*Element::\w+\s*\},', body.group(1), re.MULTILINE)
    if not keys:
        raise SystemExit(f"No keys in the Kea {table} table")
    return sorted(keys)


def _accepted_keys(family: int, version: str) -> dict[str, list[str]]:
    source = _curl(KEYWORD_TABLES.format(version=version, family=family))
    return {
        "shared-networks": _table_keys(source, f"SHARED_NETWORK{family}_PARAMETERS"),
        f"subnet{family}": _table_keys(source, f"SUBNET{family}_PARAMETERS"),
    }


def _curl(url: str, target: Path | None = None) -> str:
    output = ("--output", str(target)) if target else ()
    return _run(
        "curl", "--fail", "--silent", "--show-error", "--location", "--max-time", str(DOWNLOAD_SECONDS), *output, url
    )


def _record_control_agent() -> None:
    """Record the replies of a real Control Agent that cannot reach its daemons."""
    image = IMAGE.format(family=4, version=CONTROL_AGENT_VERSION)
    facts = _run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        image,
        "-c",
        "cat /etc/alpine-release; apk list -I isc-kea-common",
    )
    match = re.fullmatch(r"(\d+\.\d+)\.\d+\nisc-kea-common-(\S+) .*\n", facts)
    if match is None:
        raise SystemExit(f"Unexpected release facts from {image}: {facts!r}")
    alpine, build = match.groups()
    container = "netbox-kea-record-control-agent"
    with tempfile.TemporaryDirectory() as workdir:
        repository = Path(workdir) / "repository" / "x86_64"
        repository.mkdir(parents=True)
        # apk trusts the package through the repository index, which the image's ISC key signs.
        base = CONTROL_AGENT_REPOSITORY.format(alpine=alpine)
        _curl(base + "APKINDEX.tar.gz", repository / "APKINDEX.tar.gz")
        _curl(base + f"isc-kea-ctrl-agent-{build}.apk", repository / f"isc-kea-ctrl-agent-{build}.apk")
        sockets = {
            f"dhcp{family}": {"socket-type": "unix", "socket-name": f"kea{family}-ctrl-socket"} for family in (4, 6)
        }
        # The agent listens inside the container; the published host port is loopback only.
        agent = {"Control-agent": {"http-host": "0.0.0.0", "http-port": 8000, "control-sockets": sockets}}  # noqa: S104
        (Path(workdir) / "kea-ctrl-agent.conf").write_text(json.dumps(agent))
        _docker("rm", "-f", container, check=False)
        _docker(
            "run", "-d", "--name", container,
            "-p", f"127.0.0.1:{CONTROL_AGENT_PORT}:8000",
            "-v", f"{Path(workdir) / 'repository'}:/repository:ro",
            "-v", f"{Path(workdir) / 'kea-ctrl-agent.conf'}:/etc/kea/kea-ctrl-agent.conf:ro",
            "--entrypoint", "sh", image, "-c",
            f"apk add --no-network --repository /repository isc-kea-ctrl-agent={build}"
            " && exec kea-ctrl-agent -c /etc/kea/kea-ctrl-agent.conf",
        )  # fmt: skip
        try:
            _wait_until_ready(CONTROL_AGENT_PORT)
            recording: dict[str, object] = {"kea-version": CONTROL_AGENT_VERSION}
            for family in (4, 6):
                commands = {
                    f"network{family}-add": {"shared-networks": [{"name": "recorded"}]},
                    f"network{family}-del": {"name": "recorded"},
                }
                for name, arguments in commands.items():
                    recording[name] = _command(CONTROL_AGENT_PORT, name, arguments, service=f"dhcp{family}", result=1)
        finally:
            _docker("rm", "-f", container, check=False)
    (RECORDINGS / "control-agent.json").write_text(json.dumps(recording, indent=2, sort_keys=True) + "\n")


def main() -> None:
    """Record both families from the harness Kea version, and the Control Agent replies."""
    version = _harness_kea_version()
    accepted: dict[str, object] = {"kea-version": version}
    for family in (4, 6):
        _record(family, version)
        accepted[f"dhcp{family}"] = _accepted_keys(family, version)
    (RECORDINGS / "accepted-keys.json").write_text(json.dumps(accepted, indent=2, sort_keys=True) + "\n")
    _record_control_agent()


if __name__ == "__main__":
    main()
