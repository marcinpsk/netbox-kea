# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Adapter to the optional NetBox DHCP plugin (``netbox_dhcp``, sys4).

This is the **only** module that touches ``netbox_dhcp`` models, and it does so
lazily inside functions — never at import time — so the rest of netbox-kea (and
its CI, which does not install the plugin) imports cleanly whether or not the
plugin is present.  Call :func:`is_available` before any other entry point.

v1 scope (import + diff, read-only against Kea):

* Imports the **data tier** of a Kea ``config-get`` into ``netbox_dhcp`` rows —
  ``DHCPServer``, ``Subnet``, ``Pool``, ``HostReservation`` — reusing netbox-kea's
  existing IPAM helpers so the DHCP-plugin rows **share** the same
  ``ipam.Prefix``/``IPRange``/``IPAddress`` and ``dcim.MACAddress`` objects the
  IPAM sync maintains.
* Subnet identity is tracked in :class:`netbox_kea.models.KeaDhcpLink` keyed by
  ``(server, family, kea_subnet_id)`` — Kea's subnet-id is unique only per
  ``(server, protocol)`` and cannot live in the plugin's globally-unique
  ``Subnet.subnet_id``.  Pools and reservations match structurally within their
  resolved parent subnet.
* **Deferred** (reported, not imported): shared-network grouping (the plugin's
  ``SharedNetwork`` requires a prefix Kea does not model — member subnets are
  flattened onto the ``DHCPServer``) and per-object DHCP options.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.apps import apps

from ..mappers.kea_to_dhcp import ReservationIntent, ServerConfigIntent, SubnetIntent

logger = logging.getLogger(__name__)

PLUGIN_APP_LABEL = "netbox_dhcp"


def is_available() -> bool:
    """Return ``True`` when the optional NetBox DHCP plugin is installed."""
    return apps.is_installed(PLUGIN_APP_LABEL)


@dataclass
class ImportSummary:
    """Counters and warnings accumulated over one server-config import."""

    subnets_created: int = 0
    subnets_updated: int = 0
    pools_created: int = 0
    reservations_created: int = 0
    reservations_updated: int = 0
    shared_networks_deferred: int = 0
    options_deferred: int = 0
    errors: int = 0
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        """Record a non-fatal warning and log it."""
        self.warnings.append(message)
        logger.warning("DHCP-plugin import: %s", message)


# ─────────────────────────────────────────────────────────────────────────────
# Lazy model access
# ─────────────────────────────────────────────────────────────────────────────


def _model(name: str):
    """Return a ``netbox_dhcp`` model class by name (lazy; plugin must be installed)."""
    return apps.get_model(PLUGIN_APP_LABEL, name)


def _link_model():
    from ..models import KeaDhcpLink

    return KeaDhcpLink


# ─────────────────────────────────────────────────────────────────────────────
# IPAM / DCIM resolution (reuse netbox-kea sync helpers — share the same rows)
# ─────────────────────────────────────────────────────────────────────────────


def _ensure_prefix(cidr: str, vrf):
    """Get/create the shared ``ipam.Prefix`` for *cidr* via the IPAM sync helper.

    Refreshes the instance from the DB so ``.prefix`` is a ``netaddr.IPNetwork`` and
    not the raw string assigned on create — ``netbox_dhcp`` validators (e.g.
    ``Pool.clean``/``Subnet.clean``) do geometric containment checks that require it.
    """
    from ..sync import sync_subnet_to_netbox_prefix

    prefix_obj, _created, _updated = sync_subnet_to_netbox_prefix(cidr, vrf=vrf)
    prefix_obj.refresh_from_db()
    return prefix_obj


def _ensure_ip_range(pool_str: str, subnet_cidr: str, vrf):
    """Get/create the shared ``ipam.IPRange`` for a Kea pool, or ``None`` if unusable.

    Refreshed from the DB so ``.range``/address fields are netaddr objects for
    ``netbox_dhcp``'s containment validators.
    """
    from ..sync import _POOL_TOO_LARGE, sync_pool_to_netbox_ip_range

    result = sync_pool_to_netbox_ip_range(pool_str, subnet_cidr, vrf=vrf)
    if result is None or result is _POOL_TOO_LARGE:
        return None
    range_obj, _created, _updated = result
    range_obj.refresh_from_db()
    return range_obj


def _ensure_reservation_addresses(intent: ReservationIntent, kea_subnet_id: int | None, subnet_cidr: str):
    """Ensure the reservation's IPAM rows exist (status=reserved) and return them.

    Reuses :func:`sync_reservation_to_netbox` so the DHCP-plugin reservation shares
    the very same ``ipam.IPAddress`` rows the reservation-sync owns (decision: one
    row per address).  ``cleanup=False`` keeps the import from deleting unrelated IPs.

    Returns ``(ipv4_ip, ipv6_ips, mac_obj)``.
    """
    from netaddr import IPNetwork

    from ..sync import get_netbox_ip, sync_reservation_to_netbox

    kea_res: dict = {"hostname": intent.hostname}
    if kea_subnet_id is not None:
        kea_res["subnet-id"] = kea_subnet_id
    if intent.ip_address:
        kea_res["ip-address"] = intent.ip_address
    if intent.ip_addresses:
        kea_res["ip-addresses"] = list(intent.ip_addresses)
    if intent.identifier_type == "hw-address" and intent.identifier:
        kea_res["hw-address"] = intent.identifier

    try:
        prefix_len = IPNetwork(subnet_cidr).prefixlen
    except Exception:  # noqa: BLE001 — fall back to host mask if subnet CIDR is odd
        prefix_len = 128 if ":" in subnet_cidr else 32
    spm = {kea_subnet_id: prefix_len} if kea_subnet_id is not None else None

    try:
        sync_reservation_to_netbox(kea_res, cleanup=False, force=True, subnet_prefix_map=spm)
    except ValueError:
        # Reservation has no IP (e.g. options-only) — nothing to resolve.
        pass

    ipv4_ip = None
    ipv6_ips = []
    for addr in intent.all_addresses:
        ip_obj = get_netbox_ip(addr)
        if ip_obj is None:
            continue
        if ":" in addr:
            ipv6_ips.append(ip_obj)
        else:
            ipv4_ip = ip_obj

    mac_obj = _resolve_mac(intent.identifier) if intent.identifier_type == "hw-address" else None
    return ipv4_ip, ipv6_ips, mac_obj


def _resolve_mac(hw_address: str | None):
    """Return the ``dcim.MACAddress`` row for *hw_address* (created by the sync helper)."""
    if not hw_address:
        return None
    try:
        from dcim.models import MACAddress
        from netaddr import EUI, AddrFormatError, mac_unix_expanded
    except ImportError:
        return None
    try:
        mac_str = str(EUI(hw_address, dialect=mac_unix_expanded))
    except AddrFormatError:
        return None
    return MACAddress.objects.filter(mac_address=mac_str).first()


# ─────────────────────────────────────────────────────────────────────────────
# Upserts
# ─────────────────────────────────────────────────────────────────────────────


def upsert_dhcp_server(server):
    """Get/create the ``netbox_dhcp.DHCPServer`` mirroring this Kea *server* (match by name)."""
    DHCPServer = _model("DHCPServer")
    obj, _created = DHCPServer.objects.get_or_create(
        name=server.name,
        defaults={"description": "Imported from Kea by netbox-kea"},
    )
    return obj


def _linked_subnet(server, family: int, kea_subnet_id: int):
    """Return the DHCP-plugin Subnet previously linked for this Kea identity, or ``None``."""
    KeaDhcpLink = _link_model()
    link = KeaDhcpLink.objects.filter(server=server, family=family, kea_subnet_id=kea_subnet_id).first()
    return link.sys4_object if link is not None else None


def _subnet_name(server, intent: SubnetIntent) -> str:
    """Build a globally-unique name (NetBoxDHCPModelMixin requires unique ``name``)."""
    if intent.kea_subnet_id is not None:
        return f"{server.name} DHCPv{intent.family} subnet {intent.kea_subnet_id}"[:255]
    return f"{server.name} DHCPv{intent.family} {intent.cidr}"[:255]


def _pool_name(subnet_obj, pool_intent) -> str:
    """Build a unique pool name scoped to its parent subnet's (unique) name."""
    return f"{subnet_obj.name} pool {pool_intent.pool}"[:255]


def _reservation_name(subnet_obj, res: ReservationIntent) -> str:
    """Build a unique reservation name scoped to its parent subnet's (unique) name."""
    return f"{subnet_obj.name} {res.identifier_type}:{res.identifier}"[:255]


def upsert_subnet(server, dhcp_server, intent: SubnetIntent, summary: ImportSummary):
    """Get/create the DHCP-plugin ``Subnet`` for *intent*, tracked via ``KeaDhcpLink``.

    Returns the ``netbox_dhcp.Subnet`` instance, or ``None`` on error.
    """
    from django.contrib.contenttypes.models import ContentType

    Subnet = _model("Subnet")
    KeaDhcpLink = _link_model()

    prefix_obj = _ensure_prefix(intent.cidr, server.sync_vrf)

    existing = None
    if intent.kea_subnet_id is not None:
        existing = _linked_subnet(server, intent.family, intent.kea_subnet_id)

    try:
        if existing is not None:
            changed = False
            if existing.prefix_id != prefix_obj.pk:
                existing.prefix = prefix_obj
                changed = True
            if existing.dhcp_server_id != dhcp_server.pk or existing.shared_network_id is not None:
                existing.dhcp_server = dhcp_server
                existing.shared_network = None
                changed = True
            if changed:
                existing.save()
                summary.subnets_updated += 1
            subnet_obj = existing
        else:
            # Let the plugin auto-allocate its own (global) subnet_id; never write Kea's.
            subnet_obj = Subnet(
                name=_subnet_name(server, intent),
                prefix=prefix_obj,
                dhcp_server=dhcp_server,
                shared_network=None,
            )
            subnet_obj.save()
            summary.subnets_created += 1
            if intent.kea_subnet_id is not None:
                KeaDhcpLink.objects.update_or_create(
                    object_type=ContentType.objects.get_for_model(Subnet),
                    object_id=subnet_obj.pk,
                    defaults={
                        "server": server,
                        "family": intent.family,
                        "kea_subnet_id": intent.kea_subnet_id,
                    },
                )
    except Exception as exc:  # noqa: BLE001 — one bad subnet must not abort the import
        summary.errors += 1
        summary.warn(f"subnet {intent.cidr} (id={intent.kea_subnet_id}): {exc}")
        return None

    if intent.shared_network is not None:
        summary.shared_networks_deferred += 1
    if intent.options:
        summary.options_deferred += len(intent.options)

    return subnet_obj


def upsert_pools(subnet_obj, intent: SubnetIntent, server, summary: ImportSummary):
    """Get/create DHCP-plugin ``Pool`` rows for each Kea pool in *intent*."""
    Pool = _model("Pool")
    for pool_intent in intent.pools:
        range_obj = _ensure_ip_range(pool_intent.pool, intent.cidr, server.sync_vrf)
        if range_obj is None:
            summary.warn(f"pool {pool_intent.pool} in {intent.cidr}: unusable range, skipped")
            continue
        try:
            _obj, created = Pool.objects.get_or_create(
                subnet=subnet_obj,
                ip_range=range_obj,
                defaults={"name": _pool_name(subnet_obj, pool_intent)},
            )
            if created:
                summary.pools_created += 1
        except Exception as exc:  # noqa: BLE001
            summary.errors += 1
            summary.warn(f"pool {pool_intent.pool} in {intent.cidr}: {exc}")


def _find_reservation(HostReservation, subnet_obj, intent: ReservationIntent, mac_obj):
    """Find an existing DHCP-plugin reservation in *subnet_obj* matching *intent*'s identifier."""
    base = HostReservation.objects.filter(subnet=subnet_obj)
    id_type = intent.identifier_type
    if id_type == "hw-address":
        return base.filter(hw_address=mac_obj).first() if mac_obj is not None else None
    if id_type == "duid":
        return base.filter(duid=intent.identifier).first()
    if id_type == "circuit-id":
        return base.filter(circuit_id=intent.identifier).first()
    if id_type == "client-id":
        return base.filter(client_id=intent.identifier).first()
    if id_type == "flex-id":
        return base.filter(flex_id=intent.identifier).first()
    return None


def upsert_reservations(subnet_obj, intent: SubnetIntent, summary: ImportSummary):
    """Get/create DHCP-plugin ``HostReservation`` rows for each Kea reservation in *intent*."""
    HostReservation = _model("HostReservation")

    for res in intent.reservations:
        if res.identifier_type is None:
            summary.warn(f"reservation in {intent.cidr} has no identifier — skipped")
            continue
        if res.options:
            summary.options_deferred += len(res.options)

        ipv4_ip, ipv6_ips, mac_obj = _ensure_reservation_addresses(res, intent.kea_subnet_id, intent.cidr)

        try:
            obj = _find_reservation(HostReservation, subnet_obj, res, mac_obj)
            created = obj is None
            if obj is None:
                obj = HostReservation(subnet=subnet_obj, dhcp_server=None, name=_reservation_name(subnet_obj, res))
            obj.hostname = res.hostname or None
            _apply_reservation_identifier(obj, res, mac_obj)
            if res.family == 4:
                obj.ipv4_address = ipv4_ip
            obj.save()
            if res.family == 6 and ipv6_ips:
                obj.ipv6_addresses.set(ipv6_ips)
            if created:
                summary.reservations_created += 1
            else:
                summary.reservations_updated += 1
        except Exception as exc:  # noqa: BLE001
            summary.errors += 1
            summary.warn(f"reservation {res.identifier} in {intent.cidr}: {exc}")


def _apply_reservation_identifier(obj, res: ReservationIntent, mac_obj) -> None:
    """Set the single Kea identifier on a DHCP-plugin reservation (clearing the others)."""
    obj.hw_address = mac_obj if res.identifier_type == "hw-address" else None
    obj.duid = res.identifier if res.identifier_type == "duid" else None
    obj.circuit_id = res.identifier if res.identifier_type == "circuit-id" else None
    obj.client_id = res.identifier if res.identifier_type == "client-id" else None
    obj.flex_id = res.identifier if res.identifier_type == "flex-id" else None


def import_server_config(server, config: ServerConfigIntent) -> ImportSummary:
    """Import one parsed ``(server, family)`` Kea config into the DHCP plugin.

    Idempotent: re-running updates the same rows (subnets via ``KeaDhcpLink``,
    pools/reservations matched structurally) rather than duplicating them.
    """
    summary = ImportSummary()
    dhcp_server = upsert_dhcp_server(server)
    for subnet_intent in config.subnets:
        subnet_obj = upsert_subnet(server, dhcp_server, subnet_intent, summary)
        if subnet_obj is None:
            continue
        upsert_pools(subnet_obj, subnet_intent, server, summary)
        upsert_reservations(subnet_obj, subnet_intent, summary)
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Stale-cleanup exclusion (so the IPAM sync never GCs rows the plugin references)
# ─────────────────────────────────────────────────────────────────────────────


def sys4_referenced_ip_ids() -> set[int]:
    """PKs of ``ipam.IPAddress`` rows referenced by any DHCP-plugin reservation."""
    if not is_available():
        return set()
    HostReservation = _model("HostReservation")
    ids: set[int] = set()
    ids.update(HostReservation.objects.exclude(ipv4_address__isnull=True).values_list("ipv4_address_id", flat=True))
    ids.update(HostReservation.objects.values_list("ipv6_addresses__id", flat=True))
    ids.discard(None)
    return ids


def sys4_referenced_prefix_ids() -> set[int]:
    """PKs of ``ipam.Prefix`` rows referenced by DHCP-plugin subnets/shared-networks/reservations."""
    if not is_available():
        return set()
    Subnet = _model("Subnet")
    SharedNetwork = _model("SharedNetwork")
    HostReservation = _model("HostReservation")
    ids: set[int] = set()
    ids.update(Subnet.objects.values_list("prefix_id", flat=True))
    ids.update(SharedNetwork.objects.values_list("prefix_id", flat=True))
    ids.update(HostReservation.objects.values_list("ipv6_prefixes__id", flat=True))
    ids.update(HostReservation.objects.values_list("excluded_ipv6_prefixes__id", flat=True))
    ids.discard(None)
    return ids


def sys4_referenced_iprange_ids() -> set[int]:
    """PKs of ``ipam.IPRange`` rows referenced by any DHCP-plugin pool."""
    if not is_available():
        return set()
    Pool = _model("Pool")
    ids = set(Pool.objects.values_list("ip_range_id", flat=True))
    ids.discard(None)
    return ids
