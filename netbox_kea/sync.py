"""IPAM synchronisation helpers for netbox-kea-ng.

Bridges Kea DHCP lease/reservation data to NetBox ``IPAddress`` objects.
All database imports are deferred to function bodies so this module can
be imported without a fully initialised Django application (e.g., during
schema migration).

Phase 3 / Phase 4 implementation:
- Phase 3a: create/update NetBox IPAddress from a lease (status="active")
- Phase 3b: create/update NetBox IPAddress from a reservation (status="reserved")
- Phase 4: dns_name is always set from the Kea hostname → netbox-dns picks it up
  automatically via IPAMDNSsync if installed.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ipam.models import IPAddress as NbIPAddress

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def netbox_dns_available() -> bool:
    """Return True if the netbox-dns plugin is installed."""
    return importlib.util.find_spec("netbox_dns") is not None


def find_prefix_length(ip_str: str) -> int:
    """Return the prefix length of the longest containing NetBox Prefix.

    Uses PostgreSQL's ``net_contains`` lookup when available, and falls back
    to a Python-side scan of all prefixes for SQLite (test environments).
    Returns ``32`` for IPv4 or ``128`` for IPv6 when no prefix is found.
    """
    from django.core.exceptions import FieldError
    from django.db.utils import OperationalError, ProgrammingError
    from ipam.models import Prefix
    from netaddr import IPAddress as NetAddr
    from netaddr import IPNetwork

    default = 32 if ":" not in ip_str else 128
    ip = NetAddr(ip_str)

    # Try PostgreSQL-native lookup first (O(log n) via GiST index)
    try:
        prefix = Prefix.objects.filter(prefix__net_contains=ip_str).order_by("-prefix__prefixlen").first()
        if prefix is not None:
            return int(str(prefix.prefix).split("/")[1])
    except (ProgrammingError, OperationalError, FieldError):
        pass

    # SQLite fallback: load all prefixes and filter in Python
    best_len = -1
    for prefix in Prefix.objects.all():
        try:
            net = IPNetwork(str(prefix.prefix))
            if ip in net and net.prefixlen > best_len:
                best_len = net.prefixlen
        except Exception:  # noqa: BLE001, PERF203
            continue

    return best_len if best_len >= 0 else default


def get_netbox_ip(ip_str: str) -> NbIPAddress | None:
    """Return the first NetBox IPAddress whose host portion matches *ip_str*.

    Returns ``None`` when no matching address exists.
    """
    from ipam.models import IPAddress as NbIP

    return NbIP.objects.filter(address__startswith=f"{ip_str}/").first()


def bulk_fetch_netbox_ips(ip_list: list[str]) -> dict[str, NbIPAddress]:
    """Fetch NetBox IPAddress objects for a list of host IP strings.

    Returns a ``{ip_str: NbIPAddress}`` mapping containing only the IPs that
    are present in the NetBox database.  One database query is issued.
    """
    from django.db.models import Q
    from ipam.models import IPAddress as NbIP

    if not ip_list:
        return {}

    query = Q()
    for ip in ip_list:
        query |= Q(address__startswith=f"{ip}/")

    result: dict[str, NbIPAddress] = {}
    for nb_ip in NbIP.objects.filter(query):
        # address is stored as "ip/prefix"; extract the host part
        host = str(nb_ip.address).split("/")[0]
        result[host] = nb_ip
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Core sync functions
# ─────────────────────────────────────────────────────────────────────────────


def _apply_ip_fields(
    ip_obj: NbIPAddress,
    status: str,
    hostname: str,
    description: str,
) -> bool:
    """Apply *status*, *hostname* (dns_name), and *description* to *ip_obj*.

    Returns ``True`` when any field was changed and the object should be saved.
    """
    changed = False

    if ip_obj.status != status:
        ip_obj.status = status
        changed = True

    # Only update dns_name when the caller provides a non-empty hostname;
    # this prevents overwriting a manually maintained dns_name.
    if hostname and ip_obj.dns_name != hostname:
        ip_obj.dns_name = hostname
        changed = True

    if not ip_obj.description:
        ip_obj.description = description
        changed = True

    return changed


def sync_lease_to_netbox(lease: dict) -> tuple[NbIPAddress, bool]:
    """Create or update a NetBox IPAddress from a Kea lease dictionary.

    The ``status`` is set to ``"active"`` and ``dns_name`` to the lease
    hostname.  When a matching NetBox prefix exists the correct prefix length
    is used; otherwise ``/32`` (IPv4) or ``/128`` (IPv6) is used.

    Returns ``(ip_object, created)`` where *created* is ``True`` on the first
    call for a given IP.
    """
    from ipam.models import IPAddress as NbIP

    ip_str: str = lease["ip-address"]
    hostname: str = lease.get("hostname", "")

    ip_obj = get_netbox_ip(ip_str)
    if ip_obj is None:
        prefix_len = find_prefix_length(ip_str)
        ip_obj = NbIP(address=f"{ip_str}/{prefix_len}")
        created = True
    else:
        created = False

    changed = _apply_ip_fields(
        ip_obj,
        status="active",
        hostname=hostname,
        description="Synced from Kea DHCP lease",
    )

    if created or changed:
        ip_obj.save()

    return ip_obj, created


def sync_reservation_to_netbox(reservation: dict) -> tuple[NbIPAddress, bool]:
    """Create or update a NetBox IPAddress from a Kea reservation dictionary.

    The ``status`` is set to ``"reserved"`` and ``dns_name`` to the
    reservation hostname.

    For DHCPv6 reservations with multiple ``ip-addresses``, all addresses are
    synced.  The first address is returned as the primary ``(ip_object, created)``
    result.

    Raises ``ValueError`` when the reservation contains no IP address.

    Returns ``(ip_object, created)`` where *created* is ``True`` if any address
    was created for the first time.
    """
    from ipam.models import IPAddress as NbIP

    primary_ip: str = reservation.get("ip-address") or ((reservation.get("ip-addresses") or [""])[0])
    if not primary_ip:
        raise ValueError("Reservation has no ip-address or ip-addresses field.")

    hostname: str = reservation.get("hostname", "")
    all_ips: list[str] = [primary_ip]
    if "ip-addresses" in reservation and len(reservation["ip-addresses"]) > 1:
        all_ips = reservation["ip-addresses"]

    primary_obj: NbIPAddress | None = None
    any_created = False

    for ip_str in all_ips:
        ip_obj = get_netbox_ip(ip_str)
        if ip_obj is None:
            prefix_len = find_prefix_length(ip_str)
            ip_obj = NbIP(address=f"{ip_str}/{prefix_len}")
            created = True
        else:
            created = False

        changed = _apply_ip_fields(
            ip_obj,
            status="reserved",
            hostname=hostname,
            description="Synced from Kea DHCP reservation",
        )

        if created or changed:
            ip_obj.save()

        if primary_obj is None:
            primary_obj = ip_obj
            any_created = created

    return primary_obj, any_created  # type: ignore[return-value]
