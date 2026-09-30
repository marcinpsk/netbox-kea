# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .constants import IPAddressValue, IPNetworkValue


@dataclass(frozen=True)
class Pool:
    """One normalized inclusive allocation range within a Subnet."""

    start: IPAddressValue
    end: IPAddressValue

    @property
    def range(self) -> str:
        """Return the normalized explicit range text."""
        return f"{self.start}-{self.end}"

    def contains(self, address: IPAddressValue) -> bool:
        """Return whether *address* lies inside this Pool."""
        return address.version == self.start.version and int(self.start) <= int(address) <= int(self.end)

    def overlaps(self, other: Pool) -> bool:
        """Return whether this Pool and *other* share at least one address."""
        return (
            other.start.version == self.start.version
            and int(self.start) <= int(other.end)
            and int(other.start) <= int(self.end)
        )


def parse_pool(value: Any, subnet: IPNetworkValue) -> Pool:
    """Parse one Pool, as a range (start-end) or a prefix, that must lie inside *subnet*.

    Raises:
        ValueError: With a message safe to show to the operator.

    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter a Pool as a range (start-end) or a prefix (CIDR).")
    value = value.strip()
    if "/" in value:
        try:
            pool_network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(f"Pool {value} is not a valid prefix: {exc}.") from exc
        start, end = pool_network.network_address, pool_network.broadcast_address
    else:
        parts = [part.strip() for part in value.split("-")]
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"Pool {value} must be a range (start-end) or a prefix (CIDR).")
        try:
            start, end = ipaddress.ip_address(parts[0]), ipaddress.ip_address(parts[1])
        except ValueError as exc:
            raise ValueError(f"Pool {value} has an invalid address: {exc}.") from exc
    if start.version != subnet.version or end.version != subnet.version:
        raise ValueError(f"Pool {value} is not an IPv{subnet.version} Pool.")
    if int(start) > int(end):
        raise ValueError(f"Pool {value} starts after it ends.")
    if start not in subnet or end not in subnet:
        raise ValueError(f"Pool {value} is outside Subnet {subnet}.")
    return Pool(start=start, end=end)


def addresses_in_pools(
    addresses: Iterable[IPAddressValue], pools: Iterable[Pool]
) -> tuple[tuple[IPAddressValue, Pool], ...]:
    """Return each address that lies inside a Pool, with the first Pool that contains it."""
    pools = tuple(pools)
    matches: list[tuple[IPAddressValue, Pool]] = []
    for address in addresses:
        pool = next((pool for pool in pools if pool.contains(address)), None)
        if pool is not None:
            matches.append((address, pool))
    return tuple(matches)
