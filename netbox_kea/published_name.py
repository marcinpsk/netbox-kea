# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The published name: the hostname that Kea gives a client for a reserved hostname and a DDNS qualifying suffix.

This module is the one owner of Kea's rule. The Reservation forms, the preview and the IPAM synchronization use it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .reservations import InSubnetReservationScope

if TYPE_CHECKING:
    from .reservations import Reservation
    from .subnet_catalogue import CatalogueSnapshot


def _suffix_present(name: str, suffix: str) -> bool:
    """Return whether *name* ends in *suffix* at a label boundary, compared as Kea compares them."""
    core = suffix.removesuffix(".")
    if not name.endswith(core):
        return False
    rest = name[: len(name) - len(core)]
    return not rest or rest.endswith(".")


def published_name(stored: str, suffix: str) -> str:
    """Return the name that Kea publishes for the reserved hostname *stored* under the qualifying *suffix*.

    Kea does not qualify a name that ends in a dot or that already ends in the suffix. It lowers the name, and
    NetBox gets it without a trailing dot. An empty *stored* hostname publishes no name.
    """
    if not stored:
        return ""
    name = stored
    if suffix and not stored.endswith(".") and not _suffix_present(stored, suffix):
        name = f"{stored}.{suffix}"
    return name.removesuffix(".").lower()


def stored_hostname(name: str, suffix: str) -> str:
    """Return the hostname to store so that Kea publishes the entered *name* under the qualifying *suffix*.

    A name that ends in the suffix is stored as its labels without the suffix. A single label is stored as it is, so
    Kea qualifies it. Any other name gets a trailing dot, so Kea publishes it unchanged.
    The suffix match ignores case, because Kea publishes the name in lower case.
    """
    name = name.removesuffix(".")
    if not name or not suffix:
        return name
    core = suffix.removesuffix(".")
    if name.lower().endswith(f".{core.lower()}"):
        labels = name[: -len(core) - 1]
        if published_name(labels, suffix) == name.lower():
            return labels
    if "." not in name:
        return name
    return f"{name}."


def lease_published_name(hostname: str) -> str:
    """Return the published name of a lease: Kea stores the published name, with a trailing dot from an FQDN."""
    return hostname.removesuffix(".")


def reservation_published_name(reservation: Reservation, catalogue: CatalogueSnapshot) -> str:
    """Return the published name of *reservation* under the effective suffix of its scope in *catalogue*.

    The first address selects the Pool suffix. A Global Reservation takes the suffix of the Subnet that contains its
    first address, else the global suffix.

    Raises:
        CatalogueUnavailable: When *catalogue* does not show the suffix of a Reservation that has a hostname.

    """
    if not reservation.hostname:
        return ""
    address = reservation.addresses[0] if reservation.addresses else None
    if isinstance(reservation.scope, InSubnetReservationScope):
        suffix = catalogue.subnet_qualifying_suffix(reservation.scope.subnet, address)
    else:
        suffix = catalogue.address_qualifying_suffix(address)
    return published_name(reservation.hostname, suffix)
