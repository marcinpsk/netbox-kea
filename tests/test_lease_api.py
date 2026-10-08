# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Lease REST actions publish the normalized observation of the real Kea 3.2.0 daemons."""

import uuid
from collections.abc import Iterator
from typing import Any

import pynetbox
import pytest
import requests

from .conftest import NoBranchGuard
from .kea import KeaClient, KeaCommand

SUBNET_ID = 1
PD_ADDRESS = "2001:db8:8::"
PD_DUID = "01:02:03:04:05:06:07:a2"
ADDRESS4 = "192.0.2.1"
MALFORMED_ADDRESS4 = "192.0.2.77"
# Kea 3.2.0 accepts this expiration, and reads back a transaction time past the last datetime timestamp.
UNREPRESENTABLE_EXPIRE = 300_000_000_000


@pytest.fixture
def kea_clients(kea_control_urls: dict[int, str]) -> Iterator[dict[int, KeaClient]]:
    with (
        KeaClient(kea_control_urls[4], timeout=30, max_unpaged_leases=1000, write_guard=NoBranchGuard()) as dhcp4,
        KeaClient(kea_control_urls[6], timeout=30, max_unpaged_leases=1000, write_guard=NoBranchGuard()) as dhcp6,
    ):
        clients = {4: dhcp4, 6: dhcp6}
        wipe = {4: KeaCommand.LEASE4_WIPE, 6: KeaCommand.LEASE6_WIPE}
        for family, client in clients.items():
            client.command(wipe[family], family, check=(0, 3))
        yield clients
        for family, client in clients.items():
            client.command(wipe[family], family, check=(0, 3))


@pytest.fixture
def server(nb_api: pynetbox.api, kea_server_kwargs: dict) -> Iterator[Any]:
    created = nb_api.plugins.kea.servers.create(name=f"lease-api-{uuid.uuid4().hex[:8]}", **kea_server_kwargs)
    try:
        yield created
    finally:
        if nb_api.plugins.kea.servers.get(created.id) is not None:
            created.delete()


def _keys(value: Any) -> Iterator[str]:
    """Yield every mapping key in *value*, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _leases(nb_http: requests.Session, netbox_url: str, server_id: int, family: int) -> dict[str, Any]:
    response = nb_http.get(
        f"{netbox_url}/api/plugins/kea/servers/{server_id}/leases{family}/", params={"subnet_id": SUBNET_ID}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_lease6_api_publishes_a_delegated_prefix_without_raw_kea_values(
    nb_http: requests.Session, netbox_url: str, server, kea_clients: dict[int, KeaClient]
) -> None:
    kea_clients[6].command(
        KeaCommand.LEASE6_ADD,
        6,
        arguments={
            "ip-address": PD_ADDRESS,
            "type": "IA_PD",
            "prefix-len": 64,
            "subnet-id": SUBNET_ID,
            "duid": PD_DUID,
            "iaid": 9,
            "valid-lft": 3600,
            "preferred-lft": 1800,
            "hostname": "pd-api",
            "user-context": {"private-note": "never published"},
        },
    )

    data = _leases(nb_http, netbox_url, server.id, 6)

    assert data["complete"] is True
    assert data["coverage"] == "exhaustive"
    assert data["diagnostics"] == []
    assert data["count"] == 1
    (record,) = data["results"]
    assert record["family"] == 6
    assert record["kind"] == "delegated-prefix"
    assert record["address"] == PD_ADDRESS
    assert record["prefix_length"] == 64
    assert record["subnet_id"] == SUBNET_ID
    assert record["state"] == "assigned"
    assert record["current"] is True
    assert record["binding"] == {"duid": PD_DUID, "iaid": 9}
    assert record["hostname"] == "pd-api"
    assert record["expiration"]["infinite"] is False
    keys = set(_keys(data))
    assert not [key for key in keys if "-" in key]
    assert "user-context" not in keys
    assert "never published" not in str(data)


def test_the_lease4_api_reports_an_unreadable_lease_as_an_incomplete_observation(
    nb_http: requests.Session, netbox_url: str, server, kea_clients: dict[int, KeaClient]
) -> None:
    kea_clients[4].command(
        KeaCommand.LEASE4_ADD, 4, arguments={"ip-address": ADDRESS4, "hw-address": "08:08:08:08:08:08"}
    )
    kea_clients[4].command(
        KeaCommand.LEASE4_ADD,
        4,
        arguments={
            "ip-address": MALFORMED_ADDRESS4,
            "hw-address": "08:00:00:00:00:77",
            "valid-lft": 3600,
            "expire": UNREPRESENTABLE_EXPIRE,
        },
    )

    data = _leases(nb_http, netbox_url, server.id, 4)

    assert data["complete"] is False
    assert [record["address"] for record in data["results"]] == [ADDRESS4]
    assert data["count"] == 1
    assert [(diagnostic["code"], diagnostic["field"]) for diagnostic in data["diagnostics"]] == [
        ("out-of-range", "cltt")
    ]
    assert MALFORMED_ADDRESS4 not in str(data["diagnostics"])
