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
* DHCP **options** (``option-data``) are imported at every scope we model —
  ``DHCPServer`` (global), ``Subnet``, ``Pool``, ``HostReservation`` — binding to
  the sys4-shipped standard ``OptionDefinition`` (by space+code), or to a
  server-scoped custom definition created from a Kea ``option-def``.  Options
  whose definition cannot be resolved are skipped (counted), never fatal.
* **Tuning fields** (lifetimes, timers, lease/DDNS/BOOTP/network settings) are
  imported onto the ``DHCPServer`` (global) and ``Subnet``.  ``config-get`` returns
  these fully defaulted and inherited, so each subnet field is stored **only when
  it differs from the DHCPServer parent** (parent-diff suppression) — otherwise it
  is left blank to inherit, keeping the records minimal and faithful.
* **Deferred** (reported, not imported): shared-network grouping (the plugin's
  ``SharedNetwork`` requires a prefix Kea does not model — member subnets are
  flattened onto the ``DHCPServer``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.apps import apps

from ..mappers.kea_to_dhcp import (
    OptionDefIntent,
    OptionIntent,
    ReservationIntent,
    ServerConfigIntent,
    SubnetIntent,
)

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
    options_created: int = 0
    options_updated: int = 0
    options_skipped: int = 0
    option_defs_created: int = 0
    shared_networks_deferred: int = 0
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
# DHCP options (option-data → Option, binding to standard/custom OptionDefinition)
# ─────────────────────────────────────────────────────────────────────────────


def _default_space(family: int) -> str:
    """Return the Kea/sys4 default option space for a protocol family."""
    return "dhcp6" if family == 6 else "dhcp4"


def _send_option(opt: OptionIntent) -> str | None:
    """Map Kea ``always-send``/``never-send`` flags to the plugin's single choice."""
    if opt.always_send:
        return "always-send"
    if opt.never_send:
        return "never-send"
    return None


def _custom_def_index(config: ServerConfigIntent) -> dict[tuple, OptionDefIntent]:
    """Index a config's custom ``option-def`` entries by ``(space, code)`` for lookup."""
    index: dict[tuple, OptionDefIntent] = {}
    for d in config.option_defs:
        if d.code is None:
            continue
        index[(d.space or _default_space(config.family), d.code)] = d
    return index


def _create_custom_option_def(def_intent: OptionDefIntent, family: int, dhcp_server, summary: ImportSummary):
    """Create a non-standard ``OptionDefinition`` for a Kea custom option, scoped to the server."""
    OptionDefinition = _model("OptionDefinition")
    space = def_intent.space or _default_space(family)
    fam = 6 if space == "dhcp6" else 4
    try:
        obj = OptionDefinition(
            name=def_intent.name or f"option-{def_intent.code}",
            family=fam,
            space=space,
            code=def_intent.code,
            type=def_intent.type or "string",
            array=def_intent.array,
            record_types=list(def_intent.record_types) or None,
            encapsulate=def_intent.encapsulate,
            standard=False,
            dhcp_server=dhcp_server,
        )
        obj.save()
        summary.option_defs_created += 1
        return obj
    except Exception as exc:  # noqa: BLE001 — a bad definition must not abort the import
        summary.warn(f"option-def code={def_intent.code}: {exc}")
        return None


def _resolve_option_definition(opt: OptionIntent, family: int, dhcp_server, custom_defs, summary):
    """Find (or create) the ``OptionDefinition`` a Kea ``option-data`` entry refers to.

    Prefers the sys4-shipped **standard** definition (by space+code, else space+name);
    falls back to a server-scoped **custom** definition (existing, or created from a Kea
    ``option-def``).  Returns ``None`` when the option cannot be resolved.
    """
    OptionDefinition = _model("OptionDefinition")
    space = opt.space or _default_space(family)

    standard = OptionDefinition.objects.filter(standard=True, space=space)
    if opt.code is not None:
        found = standard.filter(code=opt.code).first()
    elif opt.name:
        found = standard.filter(name=opt.name).first()
    else:
        found = None
    if found is not None:
        return found

    custom = OptionDefinition.objects.filter(standard=False, dhcp_server=dhcp_server, space=space)
    if opt.code is not None:
        found = custom.filter(code=opt.code).first()
    elif opt.name:
        found = custom.filter(name=opt.name).first()
    if found is not None:
        return found

    if opt.code is not None:
        def_intent = custom_defs.get((space, opt.code))
        if def_intent is not None:
            return _create_custom_option_def(def_intent, family, dhcp_server, summary)
    return None


def upsert_options(parent_obj, options, family: int, dhcp_server, custom_defs, summary: ImportSummary) -> None:
    """Upsert DHCP-plugin ``Option`` rows for *options* assigned to *parent_obj*.

    Idempotent: one Option per ``(parent, definition)``.  Options whose definition
    cannot be resolved — or whose data fails the plugin's validators — are skipped
    with a warning rather than aborting the import.
    """
    if not options:
        return
    from django.contrib.contenttypes.models import ContentType

    Option = _model("Option")
    ct = ContentType.objects.get_for_model(type(parent_obj))
    for opt in options:
        definition = _resolve_option_definition(opt, family, dhcp_server, custom_defs, summary)
        if definition is None:
            summary.options_skipped += 1
            summary.warn(f"option {opt.match_key}: no matching definition, skipped")
            continue
        try:
            existing = Option.objects.filter(
                assigned_object_type=ct, assigned_object_id=parent_obj.pk, definition=definition
            ).first()
            created = existing is None
            obj = existing or Option(definition=definition, assigned_object_type=ct, assigned_object_id=parent_obj.pk)
            obj.data = opt.data or ""
            obj.csv_format = opt.csv_format
            obj.send_option = _send_option(opt)
            obj.save()
            if created:
                summary.options_created += 1
            else:
                summary.options_updated += 1
        except Exception as exc:  # noqa: BLE001 — one bad option must not abort the import
            summary.options_skipped += 1
            summary.warn(f"option {opt.match_key}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Tuning fields (lifetimes/timers/lease/DDNS/BOOTP/network) — Kea key → sys4 field
# ─────────────────────────────────────────────────────────────────────────────


def _decimal(value):
    """Coerce a Kea numeric to ``Decimal`` via ``str`` (avoids float-precision noise)."""
    from decimal import Decimal

    return None if value is None else Decimal(str(value))


def _norm_ddns_replace(value):
    """Map Kea ``ddns-replace-client-name`` (hyphenated) to the plugin's underscored choice."""
    return value.replace("-", "_") if isinstance(value, str) else value


def _relay_to_str(value):
    """Flatten a Kea ``relay`` (``{"ip-addresses": [...]}``) to the plugin's CSV string."""
    if isinstance(value, dict):
        addrs = value.get("ip-addresses") or []
    elif isinstance(value, list):
        addrs = value
    else:
        return value or None
    return ", ".join(addrs) if addrs else None


def _server_id_type(value):
    """Reduce a Kea ``server-id`` dict to its ``type`` (the plugin stores only the type)."""
    return value.get("type") if isinstance(value, dict) else value


def _hr_identifiers(value):
    """Keep only host-reservation identifier types the plugin's choice set knows."""
    if not isinstance(value, list):
        return None
    valid = {"circuit-id", "hw-address", "duid", "client-id"}
    return [x for x in value if x in valid] or None


# (kea_key, sys4_attr, transform). Fields shared by DHCPServer + Subnet.
_COMMON_FIELDS: tuple[tuple[str, str, object], ...] = (
    ("valid-lifetime", "valid_lifetime", None),
    ("min-valid-lifetime", "min_valid_lifetime", None),
    ("max-valid-lifetime", "max_valid_lifetime", None),
    ("preferred-lifetime", "preferred_lifetime", None),
    ("min-preferred-lifetime", "min_preferred_lifetime", None),
    ("max-preferred-lifetime", "max_preferred_lifetime", None),
    ("offer-lifetime", "offer_lifetime", None),
    ("renew-timer", "renew_timer", None),
    ("rebind-timer", "rebind_timer", None),
    ("match-client-id", "match_client_id", None),
    ("authoritative", "authoritative", None),
    ("reservations-global", "reservations_global", None),
    ("reservations-out-of-pool", "reservations_out_of_pool", None),
    ("reservations-in-subnet", "reservations_in_subnet", None),
    ("calculate-tee-times", "calculate_tee_times", None),
    ("t1-percent", "t1_percent", _decimal),
    ("t2-percent", "t2_percent", _decimal),
    ("cache-threshold", "cache_threshold", _decimal),
    ("cache-max-age", "cache_max_age", None),
    ("store-extended-info", "store_extended_info", None),
    ("allocator", "allocator", None),
    ("pd-allocator", "pd_allocator", None),
    ("ddns-send-updates", "ddns_send_updates", None),
    ("ddns-override-no-update", "ddns_override_no_update", None),
    ("ddns-override-client-update", "ddns_override_client_update", None),
    ("ddns-replace-client-name", "ddns_replace_client_name", _norm_ddns_replace),
    ("ddns-generated-prefix", "ddns_generated_prefix", None),
    ("ddns-qualifying-suffix", "ddns_qualifying_suffix", None),
    ("ddns-update-on-renew", "ddns_update_on_renew", None),
    ("ddns-conflict-resolution-mode", "ddns_conflict_resolution_mode", None),
    ("ddns-ttl-percent", "ddns_ttl_percent", _decimal),
    ("ddns-ttl", "ddns_ttl", None),
    ("ddns-ttl-min", "ddns_ttl_min", None),
    ("ddns-ttl-max", "ddns_ttl_max", None),
    ("hostname-char-set", "hostname_char_set", None),
    ("hostname-char-replacement", "hostname_char_replacement", None),
    ("next-server", "next_server", None),
    ("server-hostname", "server_hostname", None),
    ("boot-file-name", "boot_file_name", None),
)

_SUBNET_FIELDS: tuple[tuple[str, str, object], ...] = _COMMON_FIELDS + (
    ("relay", "relay", _relay_to_str),
    ("interface-id", "interface_id", None),
    ("rapid-commit", "rapid_commit", None),
)

_SERVER_FIELDS: tuple[tuple[str, str, object], ...] = _COMMON_FIELDS + (
    ("decline-probation-period", "decline_probation_period", None),
    ("host-reservation-identifiers", "host_reservation_identifiers", _hr_identifiers),
    ("echo-client-id", "echo_client_id", None),
    ("relay-supplied-options", "relay_supplied_options", None),
    ("server-id", "server_id", _server_id_type),
)


def _is_unset(value) -> bool:
    """Return ``True`` for values treated as 'not configured' (None / empty str / empty list)."""
    return value is None or value == "" or value == []


def _transform_value(transform, raw, kea_key: str, summary: ImportSummary):
    """Apply a field transform, returning ``None`` (and warning) if it raises."""
    if transform is None:
        return raw
    try:
        return transform(raw)
    except Exception as exc:  # noqa: BLE001 — a bad scalar must not abort the import
        summary.warn(f"setting {kea_key}: {exc}")
        return None


def _apply_global_settings(dhcp_server, settings: dict, summary: ImportSummary) -> None:
    """Populate ``DHCPServer`` global tuning fields from a Kea config block.

    Only fills fields that are currently unset, so a dual-stack server's first
    (DHCPv4) import wins shared fields and the DHCPv6 import fills the gaps (e.g.
    ``preferred-lifetime``, ``pd-allocator``) — netbox_dhcp has a single
    ``DHCPServer`` row spanning both protocols.
    """
    if not settings:
        return
    model_fields = {f.name for f in dhcp_server._meta.get_fields()}
    changed = False
    for kea_key, attr, transform in _SERVER_FIELDS:
        if attr not in model_fields or kea_key not in settings:
            continue
        if not _is_unset(getattr(dhcp_server, attr, None)):
            continue  # already set (e.g. by the other protocol) — don't clobber
        value = _transform_value(transform, settings[kea_key], kea_key, summary)
        if _is_unset(value):
            continue
        setattr(dhcp_server, attr, value)
        changed = True
    if changed:
        try:
            dhcp_server.save()
        except Exception as exc:  # noqa: BLE001
            summary.errors += 1
            summary.warn(f"DHCPServer settings: {exc}")


def _apply_subnet_settings(subnet_obj, dhcp_server, settings: dict, summary: ImportSummary) -> bool:
    """Set subnet tuning fields only where they differ from the stored DHCPServer parent.

    This is the parent-diff suppression: ``config-get`` returns every value fully
    inherited, so a subnet value equal to the server's is left blank (it inherits).
    Returns ``True`` if any field on *subnet_obj* changed.
    """
    model_fields = {f.name for f in subnet_obj._meta.get_fields()}
    changed = False
    for kea_key, attr, transform in _SUBNET_FIELDS:
        if attr not in model_fields or kea_key not in settings:
            continue
        value = _transform_value(transform, settings[kea_key], kea_key, summary)
        if _is_unset(value):
            continue
        if value == getattr(dhcp_server, attr, None):
            continue  # inherited from the DHCPServer parent — leave blank
        if getattr(subnet_obj, attr, None) != value:
            setattr(subnet_obj, attr, value)
            changed = True
    return changed


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
            if _apply_subnet_settings(existing, dhcp_server, intent.settings, summary):
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
            _apply_subnet_settings(subnet_obj, dhcp_server, intent.settings, summary)
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

    return subnet_obj


def upsert_pools(subnet_obj, intent: SubnetIntent, server, summary: ImportSummary, dhcp_server, custom_defs):
    """Get/create DHCP-plugin ``Pool`` rows (and their options) for each Kea pool in *intent*."""
    Pool = _model("Pool")
    for pool_intent in intent.pools:
        range_obj = _ensure_ip_range(pool_intent.pool, intent.cidr, server.sync_vrf)
        if range_obj is None:
            summary.warn(f"pool {pool_intent.pool} in {intent.cidr}: unusable range, skipped")
            continue
        try:
            pool_obj, created = Pool.objects.get_or_create(
                subnet=subnet_obj,
                ip_range=range_obj,
                defaults={"name": _pool_name(subnet_obj, pool_intent)},
            )
            if created:
                summary.pools_created += 1
        except Exception as exc:  # noqa: BLE001
            summary.errors += 1
            summary.warn(f"pool {pool_intent.pool} in {intent.cidr}: {exc}")
            continue
        upsert_options(pool_obj, pool_intent.options, intent.family, dhcp_server, custom_defs, summary)


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


def upsert_reservations(subnet_obj, intent: SubnetIntent, summary: ImportSummary, dhcp_server, custom_defs):
    """Get/create DHCP-plugin ``HostReservation`` rows (and their options) per Kea reservation."""
    HostReservation = _model("HostReservation")

    for res in intent.reservations:
        if res.identifier_type is None:
            summary.warn(f"reservation in {intent.cidr} has no identifier — skipped")
            continue

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
            continue
        upsert_options(obj, res.options, res.family, dhcp_server, custom_defs, summary)


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
    custom_defs = _custom_def_index(config)

    # Global (server-level) tuning fields + options.
    _apply_global_settings(dhcp_server, config.global_settings, summary)
    upsert_options(dhcp_server, config.global_options, config.family, dhcp_server, custom_defs, summary)

    for subnet_intent in config.subnets:
        subnet_obj = upsert_subnet(server, dhcp_server, subnet_intent, summary)
        if subnet_obj is None:
            continue
        upsert_options(subnet_obj, subnet_intent.options, config.family, dhcp_server, custom_defs, summary)
        upsert_pools(subnet_obj, subnet_intent, server, summary, dhcp_server, custom_defs)
        upsert_reservations(subnet_obj, subnet_intent, summary, dhcp_server, custom_defs)
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
