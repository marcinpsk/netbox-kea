# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Read-only IPAM synchronization badges and DCIM MAC synchronization."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .ipam_marker import parse_marker
from .reservations import GlobalReservationScope, Reservation, ReservationSynchronizationState

if TYPE_CHECKING:
    from ipam.models import IPAddress as NbIPAddress

logger = logging.getLogger(__name__)


def bulk_fetch_netbox_ips(ip_list: list[str]) -> dict[str, NbIPAddress]:
    """Fetch NetBox IPAddress objects for a list of host IP strings.

    Returns a ``{ip_str: NbIPAddress}`` mapping containing only the IPs that
    are present in the NetBox database.  Chunked into batches of 500 to avoid
    hitting PostgreSQL expression depth limits on large result sets.
    """
    from django.db.models import Q
    from ipam.models import IPAddress as NbIP

    if not ip_list:
        return {}

    _CHUNK = 500
    result: dict[str, NbIPAddress] = {}
    for i in range(0, len(ip_list), _CHUNK):
        chunk = ip_list[i : i + _CHUNK]
        query = Q()
        for ip in chunk:
            query |= Q(address__net_host=ip)
        for nb_ip in NbIP.objects.filter(query):
            host = str(nb_ip.address).split("/")[0]
            result[host] = nb_ip
    return result


def _update_mac_description(mac_obj: object, hostname: str) -> bool:
    """Annotate a MACAddress object's description with a ``dhcp_hostname:`` token.

    Behaviour:
    - If the MAC has no ``assigned_object`` (no interface): replace the entire
      description with ``dhcp_hostname: {hostname}``.
    - If the MAC has an ``assigned_object``: append/replace only the
      ``dhcp_hostname:`` portion, preserving any manual description text.

    Returns ``True`` when the description was changed.
    """
    TOKEN = "dhcp_hostname: "  # noqa: S105  description-label prefix, not a credential
    new_value = f"{TOKEN}{hostname}"
    desc = mac_obj.description or ""
    has_interface = getattr(mac_obj, "assigned_object", None) is not None

    if not has_interface:
        new_desc = new_value
    elif TOKEN in desc:
        # Replace just the dhcp_hostname: token value, preserve surrounding text.
        before, rest = desc.split(TOKEN, 1)
        parts = rest.split(" | ", 1)
        remainder = f" | {parts[1]}" if len(parts) > 1 else ""
        before_clean = before.rstrip(" |")
        sep = " | " if before_clean else ""
        new_desc = f"{before_clean}{sep}{new_value}{remainder}".strip(" |")
    elif desc:
        new_desc = f"{desc} | {new_value}"
    else:
        new_desc = new_value

    new_desc = new_desc[:200]
    if new_desc != mac_obj.description:
        mac_obj.description = new_desc
        return True
    return False


def sync_mac_address(hw_address: str, hostname: str = ""):
    """Create or update a NetBox ``MACAddress`` entry for *hw_address* and return it.

    When *hostname* is provided the ``description`` field is annotated with
    a ``dhcp_hostname: {hostname}`` token (smart append/replace that preserves
    any existing manual description text when the MAC has an assigned interface).

    Returns ``None`` when no row can exist: NetBox older than 4.1 has no
    ``dcim.MACAddress`` model, and a malformed address or a database error is caught
    and logged at DEBUG level so MAC sync failures never surface to the user.
    """
    try:
        from dcim.models import MACAddress
    except ImportError:
        return None  # NetBox < 4.1 — MACAddress model not available
    try:
        from netaddr import EUI, AddrFormatError, mac_unix_expanded
    except ImportError:
        logger.debug("netaddr not available — skipping MAC sync")
        return None
    try:
        from django.db.utils import IntegrityError, OperationalError, ProgrammingError

        mac_str = str(EUI(hw_address, dialect=mac_unix_expanded))
        mac_obj, _ = MACAddress.objects.get_or_create(mac_address=mac_str)
        if hostname:
            mac_obj.snapshot()
            if _update_mac_description(mac_obj, hostname):
                mac_obj.save()
    # The exception text can repeat the MAC address, so only its type goes to the log.
    except (ProgrammingError, OperationalError, IntegrityError) as exc:
        logger.debug("DB error while syncing a MAC address to NetBox DCIM: %s", type(exc).__name__)
    except AddrFormatError:
        logger.debug("Invalid MAC address format — skipping DCIM MAC sync")
    except Exception as exc:  # noqa: BLE001 — a MAC sync failure must not stop the IP address sync
        logger.debug("Failed to sync a MAC address to NetBox DCIM: %s", type(exc).__name__)
    else:
        return mac_obj
    return None


def is_kea_managed_ip(ip_obj: NbIPAddress) -> bool:
    """Return whether the IP address has a valid ownership marker."""
    return parse_marker(getattr(ip_obj, "description", "") or "") is not None


def reservation_synchronization_state(
    reservation: Reservation,
    synchronized_addresses: frozenset[str] | None = None,
) -> ReservationSynchronizationState:
    """Observe one aggregate NetBox synchronization state for a Reservation."""
    if isinstance(reservation.scope, GlobalReservationScope):
        return ReservationSynchronizationState.not_applicable(
            "Global Reservations are not synchronized to NetBox IPAM."
        )
    if not reservation.addresses:
        return ReservationSynchronizationState.not_applicable(
            "The Reservation has no allocation address to synchronize."
        )
    from django.db import DatabaseError

    try:
        addresses = [str(address) for address in reservation.addresses]
        if synchronized_addresses is None:
            found = bulk_fetch_netbox_ips(addresses)
            synchronized_addresses = frozenset(
                address for address in addresses if address in found and is_kea_managed_ip(found[address])
            )
        synchronized = sum(1 for address in addresses if address in synchronized_addresses)
    except DatabaseError:
        logger.exception("Could not determine the Reservation synchronization state")
        return ReservationSynchronizationState.unknown(
            len(reservation.addresses),
            "NetBox IPAM state could not be read.",
        )
    return ReservationSynchronizationState.from_counts(synchronized, len(addresses))
