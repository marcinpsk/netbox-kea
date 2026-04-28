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
            query |= Q(address__startswith=f"{ip}/")
        for nb_ip in NbIP.objects.filter(query):
            host = str(nb_ip.address).split("/")[0]
            result[host] = nb_ip
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Core sync functions
# ─────────────────────────────────────────────────────────────────────────────


def _compute_ip_status(
    desired_from: str,
    current_status: str | None,
    *,
    ip_str: str = "",
    other_source_ips: frozenset[str] | None = None,
) -> str:
    """Compute the correct NetBox IP status based on sync source and current state.

    Implements a semantic IP lifecycle:
    - ``dhcp``:     dynamic lease, no reservation  (ephemeral)
    - ``reserved``: reservation only, no active lease (admin intent)
    - ``active``:   both reservation AND active lease (planned + in use)

    Args:
        desired_from: ``"lease"`` or ``"reservation"``
        current_status: current NetBox IP status, or ``None`` when the IP is new.
        ip_str: IP address string for two-pass mode lookup.
        other_source_ips: When provided (not ``None``), enables **two-pass mode**
            where the set contains all IPs confirmed by the *other* sync source
            in this run.  For lease sync, pass the pre-fetched reservation IPs;
            for reservation sync, pass the lease IPs collected this run.
            Two-pass mode produces fully idempotent results — IPs with both a
            lease and a reservation converge to ``"active"`` in a single save
            instead of toggling through intermediate states on every run.
            Pass ``None`` (default) to use single-pass / legacy mode which
            falls back to ``current_status`` heuristics.

    """
    if desired_from == "lease":
        if other_source_ips is not None:
            # Two-pass mode: reservation set is authoritative for this run.
            return "active" if ip_str in other_source_ips else "dhcp"
        # Single-pass fallback: IP already reserved in NetBox → lease activates it.
        if current_status == "reserved":
            return "active"
        return "dhcp"
    # "reservation"
    if other_source_ips is not None:
        # Two-pass mode: lease set is authoritative for this run.
        return "active" if ip_str in other_source_ips else "reserved"
    # Single-pass fallback: IP already leased → reservation confirms active use.
    if current_status == "dhcp":
        return "active"
    return "reserved"


def _update_mac_description(mac_obj: object, hostname: str) -> bool:
    """Annotate a MACAddress object's description with a ``dhcp_hostname:`` token.

    Behaviour:
    - If the MAC has no ``assigned_object`` (no interface): replace the entire
      description with ``dhcp_hostname: {hostname}``.
    - If the MAC has an ``assigned_object``: append/replace only the
      ``dhcp_hostname:`` portion, preserving any manual description text.

    Returns ``True`` when the description was changed.
    """
    TOKEN = "dhcp_hostname: "
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


def _get_stale_cleanup_mode() -> str:
    """Return the configured stale IP cleanup mode from PLUGINS_CONFIG.

    Supported values: ``"remove"`` (default), ``"deprecate"``, ``"none"``.
    """
    from django.conf import settings

    config = getattr(settings, "PLUGINS_CONFIG", {}).get("netbox_kea", {})
    return config.get("stale_ip_cleanup", "remove")


def _cleanup_stale_ips(
    new_ip_str: str,
    hostname: str,
    *,
    mode: str = "remove",
    exclude_ips: frozenset[str] | None = None,
) -> int:
    """Remove or deprecate old Kea-synced IPs that have the same hostname but a different address.

    This handles the case where a device moves to a new IP: the old NetBox
    ``IPAddress`` entry would otherwise become a stale ghost.

    Matching criteria (all must be true):
    - ``dns_name`` matches *hostname* exactly
    - ``status`` is one of ``dhcp``, ``active``, or ``reserved``
    - ``description`` starts with ``"Synced from Kea DHCP"``
    - Address is NOT *new_ip_str* (the current IP is never touched)
    - Address is NOT in *exclude_ips* (allows protecting all IPs in a multi-address reservation)
    - Same IP family (IPv4 cleanup does not remove IPv6 entries)

    Args:
        new_ip_str:  The IP address currently being synced (excluded from cleanup).
        hostname:    The hostname/dns_name to match against.
        mode:        ``"remove"`` deletes matching IPs; ``"deprecate"`` sets their
                     status to ``deprecated``; ``"none"`` skips cleanup entirely.
        exclude_ips: Additional IP strings to exclude (e.g. all IPs in a multi-address
                     DHCPv6 reservation so sibling addresses are not cleaned up).

    Returns the number of IPs cleaned up.

    """
    if mode == "none" or not hostname:
        return 0

    from ipam.models import IPAddress as NbIP

    stale_qs = NbIP.objects.filter(
        dns_name=hostname,
        status__in=("dhcp", "active", "reserved"),
        description__startswith="Synced from Kea DHCP",
    ).exclude(address__startswith=f"{new_ip_str}/")

    # Also exclude sibling IPs (e.g. other addresses in the same DHCPv6 reservation).
    for exc_ip in exclude_ips or frozenset():
        stale_qs = stale_qs.exclude(address__startswith=f"{exc_ip}/")

    # Restrict to same IP family to avoid cross-family false positives.
    if ":" in new_ip_str:
        stale_qs = stale_qs.filter(address__contains=":")
    else:
        stale_qs = stale_qs.exclude(address__contains=":")

    count = stale_qs.count()
    if count == 0:
        return 0

    if mode == "remove":
        stale_qs.delete()
    elif mode == "deprecate":
        stale_qs.update(status="deprecated")
    else:
        logger.warning("Unknown stale_ip_cleanup mode %r — skipping cleanup", mode)
        return 0

    return count


def _sync_mac_address(hw_address: str, hostname: str = "") -> None:
    """Create or update a NetBox ``MACAddress`` entry for *hw_address*.

    When *hostname* is provided the ``description`` field is annotated with
    a ``dhcp_hostname: {hostname}`` token (smart append/replace that preserves
    any existing manual description text when the MAC has an assigned interface).

    Silently skipped on NetBox versions older than 4.1 where the
    ``dcim.MACAddress`` model does not exist.  All other errors are caught and
    logged at DEBUG level so MAC sync failures never surface to the user.
    """
    try:
        from dcim.models import MACAddress
    except ImportError:
        return  # NetBox < 4.1 — MACAddress model not available
    try:
        from netaddr import EUI, AddrFormatError, mac_unix_expanded
    except ImportError:
        logger.debug("netaddr not available — skipping MAC sync for %s", hw_address)
        return
    try:
        from django.db.utils import IntegrityError, OperationalError, ProgrammingError

        mac_str = str(EUI(hw_address, dialect=mac_unix_expanded))
        mac_obj, _ = MACAddress.objects.get_or_create(mac_address=mac_str)
        if hostname and _update_mac_description(mac_obj, hostname):
            mac_obj.save()
    except (ProgrammingError, OperationalError, IntegrityError):
        logger.debug("DB error while syncing MAC address %s to NetBox DCIM", hw_address, exc_info=True)
    except AddrFormatError:
        logger.debug("Invalid MAC address format %r — skipping DCIM MAC sync", hw_address, exc_info=True)
    except Exception:  # noqa: BLE001 — unexpected errors from MACAddress model
        logger.debug("Failed to sync MAC address %s to NetBox DCIM", hw_address, exc_info=True)


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


def sync_lease_to_netbox(
    lease: dict,
    *,
    cleanup: bool = True,
    reservation_ips: frozenset[str] | None = None,
) -> tuple[NbIPAddress, bool, bool]:
    """Create or update a NetBox IPAddress from a Kea lease dictionary.

    The ``status`` is set to ``"active"`` and ``dns_name`` to the lease
    hostname.  When a matching NetBox prefix exists the correct prefix length
    is used; otherwise ``/32`` (IPv4) or ``/128`` (IPv6) is used.

    Also creates/updates a ``MACAddress`` entry in DCIM when the lease
    includes a ``hw-address`` field (NetBox ≥ 4.1 only; silently skipped on
    older versions).

    Args:
        lease:            Raw Kea lease dictionary.
        cleanup:          When ``True`` (default), call :func:`_cleanup_stale_ips` for
                          the hostname after syncing.  Set to ``False`` in batch
                          operations where the caller will perform a single cleanup pass
                          with the full keep-set via :func:`cleanup_stale_ips_batch`.
        reservation_ips:  Optional frozenset of all reservation IPs confirmed by the
                          reservation pre-fetch in this sync run.  When provided,
                          enables two-pass idempotent status computation — ``"active"``
                          if the IP also has a reservation, ``"dhcp"`` otherwise.
                          Pass ``None`` (default) to use single-pass fallback mode.

    Returns ``(ip_object, created, changed)`` where *created* is ``True`` on
    the first call for a given IP and *changed* is ``True`` when any field was
    modified (including on first creation).

    """
    from ipam.models import IPAddress as NbIP

    ip_str: str = lease["ip-address"]
    hostname: str = lease.get("hostname", "")

    ip_obj = get_netbox_ip(ip_str)
    if ip_obj is None:
        prefix_len = find_prefix_length(ip_str)
        ip_obj = NbIP(address=f"{ip_str}/{prefix_len}")
        created = True
        current_status = None
    else:
        created = False
        current_status = ip_obj.status

    status = _compute_ip_status("lease", current_status, ip_str=ip_str, other_source_ips=reservation_ips)
    changed = _apply_ip_fields(
        ip_obj,
        status=status,
        hostname=hostname,
        description="Synced from Kea DHCP lease",
    )

    if created or changed:
        ip_obj.save()

    # No exclude_ips needed: Kea assigns one active lease per hostname, so
    # there are no sibling IPs to protect (unlike reservations with multi-address).
    if cleanup and hostname:
        _cleanup_stale_ips(ip_str, hostname, mode=_get_stale_cleanup_mode())

    hw_address = lease.get("hw-address")
    if hw_address:
        _sync_mac_address(hw_address, hostname)

    return ip_obj, created, changed


def sync_reservation_to_netbox(
    reservation: dict,
    *,
    cleanup: bool = True,
    lease_ips: frozenset[str] | None = None,
) -> tuple[NbIPAddress, bool, bool]:
    """Create or update a NetBox IPAddress from a Kea reservation dictionary.

    The ``status`` is set to ``"reserved"`` and ``dns_name`` to the
    reservation hostname.

    For DHCPv6 reservations with multiple ``ip-addresses``, all addresses are
    synced.  The first address is returned as the primary ``(ip_object, created)``
    result.

    Args:
        reservation: Raw Kea reservation dictionary.
        cleanup:     When ``True`` (default), call :func:`_cleanup_stale_ips`
                     for the hostname after syncing.  Set to ``False`` in batch
                     operations where the caller will perform a single cleanup
                     pass with the full keep-set via :func:`cleanup_stale_ips_batch`.
        lease_ips:   Optional frozenset of all lease IPs confirmed by the lease
                     sync in this run.  When provided, enables two-pass idempotent
                     status computation — ``"active"`` if the IP also has a lease,
                     ``"reserved"`` otherwise.  Pass ``None`` (default) to use
                     single-pass fallback mode.

    Raises ``ValueError`` when the reservation contains no IP address.

    Returns ``(ip_object, created, changed)`` where *created* is ``True`` if
    any address was created for the first time and *changed* is ``True`` when
    any address was saved (created or modified).

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
    any_changed = False

    for ip_str in all_ips:
        ip_obj = get_netbox_ip(ip_str)
        if ip_obj is None:
            prefix_len = find_prefix_length(ip_str)
            ip_obj = NbIP(address=f"{ip_str}/{prefix_len}")
            created = True
            current_status = None
        else:
            created = False
            current_status = ip_obj.status

        status = _compute_ip_status("reservation", current_status, ip_str=ip_str, other_source_ips=lease_ips)
        changed = _apply_ip_fields(
            ip_obj,
            status=status,
            hostname=hostname,
            description="Synced from Kea DHCP reservation",
        )

        if created or changed:
            ip_obj.save()

        if primary_obj is None:
            primary_obj = ip_obj
        any_created = any_created or created
        any_changed = any_changed or changed or created

    # Cleanup stale IPs outside the loop — exclude ALL IPs in this reservation
    # so sibling addresses (DHCPv6 multi-address) are never treated as stale.
    if cleanup and hostname:
        _cleanup_stale_ips(
            primary_ip,
            hostname,
            mode=_get_stale_cleanup_mode(),
            exclude_ips=frozenset(all_ips),
        )

    hw_address = reservation.get("hw-address")
    if hw_address:
        _sync_mac_address(hw_address, hostname)

    return primary_obj, any_created, any_changed  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────────────
# Batch stale-IP cleanup
# ─────────────────────────────────────────────────────────────────────────────


def cleanup_stale_ips_batch(synced_records: list[dict]) -> int:
    """Run stale-IP cleanup once per hostname using the full keep-set.

    When multiple records share a hostname (e.g., two leases assigned to the
    same device), calling :func:`_cleanup_stale_ips` per-record would
    incorrectly mark sibling IPs as stale.  This function accumulates **all**
    synced IPs per hostname first, then calls :func:`_cleanup_stale_ips` once
    with the complete ``exclude_ips`` set so no sibling is removed.

    Args:
        synced_records: A list of raw Kea dicts (leases or reservations) that
            were synced in this batch.  Each dict must have an ``"ip-address"``
            (or ``"ip-addresses"`` for multi-address reservations) and
            optionally a ``"hostname"`` field.

    Returns the total number of stale IPs cleaned up across all hostnames.

    """
    mode = _get_stale_cleanup_mode()
    if mode == "none":
        return 0

    # Build (hostname, family) → {IPs} mapping so each address family
    # is cleaned independently (prevents wrong family filter).
    hostname_ips: dict[tuple[str, int], set[str]] = {}
    for record in synced_records:
        hostname = record.get("hostname", "")
        if not hostname:
            continue
        ips: set[str] = set()
        if "ip-address" in record and record["ip-address"]:
            ips.add(record["ip-address"])
        for addr in record.get("ip-addresses", []):
            if addr:
                ips.add(addr)
        for ip in ips:
            family = 6 if ":" in ip else 4
            hostname_ips.setdefault((hostname, family), set()).add(ip)

    total_cleaned = 0
    for (hostname, _family), all_ips in hostname_ips.items():
        primary_ip = next(iter(all_ips))
        total_cleaned += _cleanup_stale_ips(
            primary_ip,
            hostname,
            mode=mode,
            exclude_ips=frozenset(all_ips),
        )

    return total_cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Prefix and IP Range sync (Kea subnets → NetBox IP Prefixes / pools → Ranges)
# ─────────────────────────────────────────────────────────────────────────────


def sync_subnet_to_netbox_prefix(subnet_cidr: str, vrf=None) -> tuple:
    """Create or update a NetBox Prefix from a Kea subnet CIDR string.

    Behaviour:
    - If a Prefix with this CIDR already exists (in *vrf*), it is returned
      as-is (idempotent).  The description is set only when the existing
      object has an empty description, to avoid overwriting operator notes.
    - Otherwise a new active Prefix is created with description
      ``"Synced from Kea DHCP subnet"``.

    Args:
        subnet_cidr: CIDR notation, e.g. ``"192.168.10.0/24"`` or ``"2001:db8::/48"``.
        vrf: NetBox VRF instance to assign the prefix to.  ``None`` means the global VRF.

    Returns ``(prefix_object, created, did_update)`` where *created* is ``True`` for new
    objects and *did_update* is ``True`` when an existing object's description was set.

    """
    from ipam.models import Prefix

    prefix_obj, created = Prefix.objects.get_or_create(
        prefix=subnet_cidr,
        vrf=vrf,
        defaults={"status": "active", "description": "Synced from Kea DHCP subnet"},
    )
    did_update = False
    if not created and not prefix_obj.description:
        prefix_obj.description = "Synced from Kea DHCP subnet"
        prefix_obj.save(update_fields=["description"])
        did_update = True
    return prefix_obj, created, did_update


def _parse_pool_range(pool_str: str, subnet_prefix_len: int) -> tuple[str, str] | None:
    """Parse a Kea pool string and return ``(start_address, end_address)`` in CIDR form.

    Handles:
    - Range format ``"192.168.10.50-192.168.10.100"`` → host IPs tagged with the
      parent subnet prefix length.
    - CIDR format ``"192.168.10.128/25"`` → network/broadcast addresses with the
      pool's own prefix length.

    Returns ``None`` when the format is unrecognised or parsing fails.
    """
    from netaddr import AddrFormatError, IPNetwork
    from netaddr import IPAddress as NetaddrIP

    pool_str = pool_str.strip()
    try:
        if "-" in pool_str and "/" not in pool_str:
            parts = pool_str.split("-", 1)
            start_ip = str(NetaddrIP(parts[0].strip()))
            end_ip = str(NetaddrIP(parts[1].strip()))
            return f"{start_ip}/{subnet_prefix_len}", f"{end_ip}/{subnet_prefix_len}"
        if "/" in pool_str:
            net = IPNetwork(pool_str)
            return f"{net.network}/{net.prefixlen}", f"{net[-1]}/{net.prefixlen}"
    except (AddrFormatError, ValueError, IndexError):
        logger.debug("Failed to parse pool range %r", pool_str)
    return None


def sync_pool_to_netbox_ip_range(pool_str: str, subnet_cidr: str, vrf=None) -> tuple | None:
    """Create or update a NetBox IPRange from a Kea pool definition.

    Args:
        pool_str:    Kea pool string, e.g. ``"192.168.10.50-192.168.10.100"`` or
                     ``"192.168.10.128/25"``.
        subnet_cidr: Parent subnet CIDR (e.g. ``"192.168.10.0/24"``) used to derive
                     the prefix length for range-format pools.
        vrf: NetBox VRF instance to assign the IP range to.  ``None`` means the global VRF.

    Returns ``(ip_range_object, created, did_update)`` or ``None`` when the pool string
    cannot be parsed.

    """
    from ipam.models import IPRange
    from netaddr import AddrFormatError, IPNetwork

    try:
        subnet_prefix_len = IPNetwork(subnet_cidr).prefixlen
    except (AddrFormatError, ValueError):
        subnet_prefix_len = 32 if ":" not in subnet_cidr else 128

    addresses = _parse_pool_range(pool_str, subnet_prefix_len)
    if addresses is None:
        return None

    start_addr_str, end_addr_str = addresses
    start_addr = IPNetwork(start_addr_str)
    end_addr = IPNetwork(end_addr_str)

    # Guard against IPv6 ranges that overflow PostgreSQL bigint (max 2^63-1).
    # An IPRange row stores a `size` column; ranges spanning 2^63+ addresses cannot be persisted.
    _PG_BIGINT_MAX = 9_223_372_036_854_775_807
    if int(end_addr.ip - start_addr.ip) + 1 > _PG_BIGINT_MAX:
        logger.debug("Skipping pool %r: range too large to store as NetBox IPRange", pool_str)
        return None

    range_obj, created = IPRange.objects.get_or_create(
        start_address=start_addr,
        end_address=end_addr,
        vrf=vrf,
        defaults={"status": "active", "description": "Synced from Kea DHCP pool"},
    )
    did_update = False
    if not created and not range_obj.description:
        range_obj.description = "Synced from Kea DHCP pool"
        range_obj.save(update_fields=["description"])
        did_update = True
    return range_obj, created, did_update
