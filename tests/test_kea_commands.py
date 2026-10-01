# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Check that the harness Kea lists exactly the commands the unit suite accepts."""

import json
from pathlib import Path

import pytest

from .conftest import NoBranchGuard
from .kea import KeaClient, KeaCommand

_RECORDINGS = Path(__file__).resolve().parents[1] / "netbox_kea" / "tests" / "kea_recordings"


@pytest.mark.parametrize("family", [4, 6])
def test_the_harness_kea_lists_the_recorded_commands(family: int, kea_control_urls: dict[int, str]) -> None:
    recorded = json.loads((_RECORDINGS / f"dhcp{family}.json").read_text())["list-commands"]["arguments"]
    with KeaClient(
        kea_control_urls[family], timeout=30, max_unpaged_leases=1000, write_guard=NoBranchGuard()
    ) as client:
        (reply,) = client.command(KeaCommand.LIST_COMMANDS, family)
    live = set(reply["arguments"])
    assert live == set(recorded), (
        f"The harness kea-dhcp{family} lists {sorted(live - set(recorded))} that the recording does not, and "
        f"does not list {sorted(set(recorded) - live)}. Run scripts/record_kea_config_get.py, which records "
        "list-commands from the coverage configurations in netbox_kea/tests/kea_recordings/. Those "
        "configurations must load the same hook libraries as tests/docker/kea_configs/."
    )
