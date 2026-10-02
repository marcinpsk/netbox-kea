# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Adapter to the optional NetBox DHCP plugin (``netbox_dhcp``, sys4).

This is the **only** module that touches ``netbox_dhcp`` models, and it does so
lazily inside functions — never at import time — so the rest of netbox-kea (and
its CI, which does not install the plugin) imports cleanly whether or not the
plugin is present.  Call :func:`is_available` before any other entry point.

v1 scope (import + diff, read-only against Kea):

* Imports the **data tier** of a Kea ``config-get`` into ``netbox_dhcp`` rows —
  ``DHCPServer``, ``Subnet``, ``Pool``, ``HostReservation`` — reusing netbox-kea's
  IPAM ownership claims so the DHCP-plugin rows **share** the same
  ``ipam.Prefix``/``IPRange``/``IPAddress`` and ``dcim.MACAddress`` objects the
  IPAM sync maintains.
* **Host reservations** come from the shared typed Reservation Snapshot. The
  adapter never parses raw ``reservation-get-page`` records. Valid records import
  even when the snapshot quarantines other records. A Global Reservation skips the
  IPAM sync, so the import creates no address rows for it and reports every reserved
  address it could not attach.  Delegated prefixes carry their own length, so they
  import as shared ``ipam.Prefix`` rows for every Scope.
* Subnet identity is tracked in :class:`netbox_kea.models.KeaDhcpLink` keyed by
  ``(server, family, kea_subnet_id)`` — Kea's subnet-id is unique only per
  ``(server, protocol)`` and cannot live in the plugin's globally-unique
  ``Subnet.subnet_id``.  Pools and In-Subnet reservations match structurally within
  their resolved parent subnet.  A Global Reservation has no parent subnet to carry a
  family, so its link is keyed by ``(server, family, kea_identity)`` instead.
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
* **Client classes** (``client-classes``) are imported as ``ClientClass`` rows
  (test/template-test, additional-list flag, BOOTP + lifetime settings, options),
  named ``"<server>: <kea-name>"`` since the plugin requires a globally-unique name.
* **Deferred** (reported, not imported): shared-network grouping (the plugin's
  ``SharedNetwork`` requires a prefix Kea does not model — member subnets are
  flattened onto the ``DHCPServer``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ipam_reconciliation import ClaimResult, ReservationObservation, SyncReport

from django.apps import apps
from django.db import transaction

from ..constants import IPNetworkValue
from ..dhcp_options import DHCPOption
from ..kea import subnet_network
from ..mappers.kea_to_dhcp import (
    ClientClassIntent,
    OptionDefIntent,
    ServerConfigIntent,
    SubnetIntent,
)
from ..pools import parse_pool
from ..reservations import (
    TRAVERSAL_DIAGNOSTIC_CODES,
    InSubnetReservationScope,
    Reservation,
)

logger = logging.getLogger(__name__)

PLUGIN_APP_LABEL = "netbox_dhcp"


def is_available() -> bool:
    """Return ``True`` when the optional NetBox DHCP plugin is installed."""
    return apps.is_installed(PLUGIN_APP_LABEL)


def _ownership_report() -> SyncReport:
    from ..ipam_reconciliation import SyncReport

    return SyncReport()


@dataclass
class ImportSummary:
    """Counters and warnings accumulated over one server-config import."""

    ownership: SyncReport = field(default_factory=_ownership_report)
    subnets_created: int = 0
    subnets_updated: int = 0
    pools_created: int = 0
    reservations_created: int = 0
    reservations_updated: int = 0
    reservations_quarantined: int = 0
    reservations_skipped: int = 0
    options_created: int = 0
    options_updated: int = 0
    options_skipped: int = 0
    option_defs_created: int = 0
    client_classes_created: int = 0
    client_classes_updated: int = 0
    shared_networks_deferred: int = 0
    foreign_addresses_skipped: int = 0
    owner_disagreements: int = 0
    # Reserved addresses with no NetBox IP to attach. A Global Reservation has no
    # Subnet to size an address from, so the import never creates one for it.
    addresses_unattached: int = 0
    # True when the Reservation Snapshot traversal could not read every record.
    reservations_unread: bool = False
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


def _reservation_addresses(reservation: Reservation, claims: ClaimResult, summary: ImportSummary):
    """Attach the address objects returned by the whole-snapshot ownership claim."""
    ipv4_ip = None
    ipv6_ips = []
    unattached = []
    for address in reservation.addresses:
        result = claims.addresses[str(address)]
        if result.outcome == "error":
            raise RuntimeError("The reserved address could not be claimed")
        if result.outcome == "conflict":
            summary.foreign_addresses_skipped += 1
            summary.warn(
                f"reservation {reservation.identity.value}: manually curated NetBox IP {address} left unchanged"
            )
        elif result.outcome == "disagreement":
            summary.warn(f"reservation {reservation.identity.value}: owner disagreement for {address}; IPAM unchanged")
        if result.ip is None:
            unattached.append(str(address))
        elif address.version == 4:
            ipv4_ip = result.ip
        else:
            ipv6_ips.append(result.ip)
    if unattached:
        summary.addresses_unattached += len(unattached)
        summary.warn(
            f"reservation {reservation.identity.value}: reserved address(es) "
            f"{', '.join(unattached)} have no NetBox IP and were not attached"
        )
    hardware = reservation.identity.hardware_address
    mac_obj = None
    if hardware:
        for address in reservation.addresses:
            mac_obj = claims.addresses[str(address)].resolved_macs.get((hardware, reservation.hostname))
            if mac_obj is not None:
                break
        if mac_obj is None:
            mac_obj = _resolve_mac(hardware, reservation.hostname)
    return ipv4_ip, ipv6_ips, mac_obj


def _resolve_mac(hw_address: str | None, hostname: str = ""):
    """Return the ``dcim.MACAddress`` row for *hw_address*, creating it when absent.

    Address claims return their resolved MAC rows. Global, addressless and curated
    Reservations can have no such result, so they resolve the DHCP identifier here.
    """
    if not hw_address:
        return None
    from ..sync import sync_mac_address

    return sync_mac_address(hw_address, hostname)


# ─────────────────────────────────────────────────────────────────────────────
# DHCP options (option-data → Option, binding to standard/custom OptionDefinition)
# ─────────────────────────────────────────────────────────────────────────────


def _default_space(family: int) -> str:
    """Return the Kea/sys4 default option space for a protocol family."""
    return "dhcp6" if family == 6 else "dhcp4"


def _send_option(opt: DHCPOption) -> str | None:
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
        with transaction.atomic():
            obj.save()
        summary.option_defs_created += 1
    except Exception as exc:  # noqa: BLE001 — a bad definition must not abort the import
        summary.warn(f"option-def code={def_intent.code}: {exc}")
        return None
    else:
        return obj


def _resolve_option_definition(opt: DHCPOption, family: int, dhcp_server, custom_defs, summary):
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
            with transaction.atomic():
                existing = Option.objects.filter(
                    assigned_object_type=ct, assigned_object_id=parent_obj.pk, definition=definition
                ).first()
                created = existing is None
                obj = existing or Option(
                    definition=definition, assigned_object_type=ct, assigned_object_id=parent_obj.pk
                )
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

_SUBNET_FIELDS: tuple[tuple[str, str, object], ...] = (
    *_COMMON_FIELDS,
    ("relay", "relay", _relay_to_str),
    ("interface-id", "interface_id", None),
    ("rapid-commit", "rapid_commit", None),
)

_SERVER_FIELDS: tuple[tuple[str, str, object], ...] = (
    *_COMMON_FIELDS,
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


def _apply_global_settings(dhcp_server, settings: dict, summary: ImportSummary, *, primary: bool) -> None:
    """Populate ``DHCPServer`` global tuning fields from a Kea config block.

    netbox_dhcp has a single ``DHCPServer`` row spanning both protocols, so one
    family is authoritative.  The *primary* family (DHCPv4, or whichever family is
    enabled on a single-stack server) **mirrors** its values — re-import re-syncs a
    changed global value.  The secondary family only **fills gaps** (e.g. the
    DHCPv6-only ``preferred-lifetime``/``pd-allocator``) so it never clobbers the
    primary family's shared fields and dual-stack re-imports do not thrash.

    Known limitation: a change to a *secondary-only* field (e.g. DHCPv6
    ``preferred-lifetime``) on a dual-stack server is not re-synced, since the
    secondary family is fill-only for an already-set field.
    """
    if not settings:
        return
    model_fields = {f.name for f in dhcp_server._meta.get_fields()}
    changed: list[str] = []
    for kea_key, attr, transform in _SERVER_FIELDS:
        if attr not in model_fields or kea_key not in settings:
            continue
        value = _transform_value(transform, settings[kea_key], kea_key, summary)
        if _is_unset(value):
            continue
        current = getattr(dhcp_server, attr, None)
        if primary:
            # This family owns the global config — mirror changed values on re-import.
            if current != value:
                setattr(dhcp_server, attr, value)
                changed.append(attr)
        elif _is_unset(current):
            # Secondary protocol only fills gaps the primary family did not set.
            setattr(dhcp_server, attr, value)
            changed.append(attr)
    if changed:
        try:
            with transaction.atomic():
                dhcp_server.save()
        except Exception as exc:  # noqa: BLE001
            summary.errors += 1
            summary.warn(f"DHCPServer settings: {exc}")
            # Children diff against this instance, so it must hold the persisted values.
            dhcp_server.refresh_from_db(fields=changed)


def _apply_inherited_settings(obj, parent, settings: dict, field_map, summary: ImportSummary) -> bool:
    """Mirror *obj*'s tuning fields to Kea, storing only genuine overrides.

    ``config-get`` returns every value fully inherited, so a field is stored on the
    child only when it is present and **differs** from the stored *parent*
    (parent-diff suppression).  A value equal to the parent — or one Kea no longer
    reports — is **cleared**, so a removed Kea override does not linger as stale data
    on re-import.  The ``model_fields`` guard skips fields *obj* does not have, so one
    field map serves models with different mixins (e.g. ``Subnet`` vs ``ClientClass``).
    Returns ``True`` if any field on *obj* changed.
    """
    model_fields = {f.name for f in obj._meta.get_fields()}
    changed = False
    for kea_key, attr, transform in field_map:
        if attr not in model_fields:
            continue
        value = _transform_value(transform, settings[kea_key], kea_key, summary) if kea_key in settings else None
        # Store only a genuine override (present and differing from the parent);
        # otherwise clear it so the child inherits (and stale overrides don't linger).
        if not _is_unset(value) and value != getattr(parent, attr, None):
            desired = value
        else:
            # "Cleared" value: None for nullable fields, the field's empty default
            # (e.g. "" for a non-null CharField like hostname_char_set) otherwise.
            db_field = obj._meta.get_field(attr)
            desired = None if db_field.null else db_field.get_default()
        if getattr(obj, attr, None) != desired:
            setattr(obj, attr, desired)
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


def _client_class_name(server, kea_name: str) -> str:
    """Namespace a Kea class name to this server (NetBoxDHCPModelMixin needs a unique name).

    Note: this namespaced name will NOT match the bare Kea class name referenced from
    a subnet's ``client-classes`` list or another class's ``test`` expression.  That is
    fine for the v1 import (those references are not imported), but a future push-back /
    reference-resolution phase must map the bare Kea name back to this namespaced row.
    """
    return f"{server.name}: {kea_name}"[:255]


def upsert_client_class(server, dhcp_server, intent: ClientClassIntent, custom_defs, summary: ImportSummary):
    """Get/create the DHCP-plugin ``ClientClass`` for *intent* (match by namespaced name)."""
    ClientClass = _model("ClientClass")
    cc_name = _client_class_name(server, intent.name)
    obj = ClientClass.objects.filter(name=cc_name).first()
    created = obj is None
    if obj is None:
        obj = ClientClass(name=cc_name, dhcp_server=dhcp_server)

    changed = created
    if obj.dhcp_server_id != dhcp_server.pk:
        obj.dhcp_server = dhcp_server
        changed = True
    if obj.test != (intent.test or ""):
        obj.test = intent.test or ""
        changed = True
    if obj.template_test != (intent.template_test or ""):
        obj.template_test = intent.template_test or ""
        changed = True
    if intent.only_in_additional_list is not None and obj.only_in_additional_list != intent.only_in_additional_list:
        obj.only_in_additional_list = intent.only_in_additional_list
        changed = True
    if _apply_inherited_settings(obj, dhcp_server, intent.settings, _COMMON_FIELDS, summary):
        changed = True

    try:
        if changed:
            with transaction.atomic():
                obj.save()
    except Exception as exc:  # noqa: BLE001 — one bad class must not abort the import
        summary.errors += 1
        summary.warn(f"client-class {intent.name}: {exc}")
        return None

    if created:
        summary.client_classes_created += 1
    elif changed:
        summary.client_classes_updated += 1

    upsert_options(obj, intent.options, intent.family, dhcp_server, custom_defs, summary)
    return obj


def _linked_subnet(server, family: int, kea_subnet_id: int):
    """Return the DHCP-plugin Subnet previously linked for this Kea identity, or ``None``."""
    KeaDhcpLink = _link_model()
    link = KeaDhcpLink.objects.filter(server=server, family=family, kea_subnet_id=kea_subnet_id).first()
    return link.sys4_object if link is not None else None


def _subnet_name(server, intent: SubnetIntent, network: IPNetworkValue) -> str:
    """Build a globally-unique name (NetBoxDHCPModelMixin requires unique ``name``)."""
    if intent.kea_subnet_id is not None:
        return f"{server.name} DHCPv{intent.family} subnet {intent.kea_subnet_id}"[:255]
    return f"{server.name} DHCPv{intent.family} {network}"[:255]


def _pool_name(subnet_obj, pool_intent) -> str:
    """Build a unique pool name scoped to its parent subnet's (unique) name."""
    return f"{subnet_obj.name} pool {pool_intent.pool}"[:255]


def _reservation_name(scope_name: str, reservation: Reservation) -> str:
    """Build a unique reservation name scoped to its parent's (unique) name."""
    identity = reservation.identity
    return f"{scope_name} {identity.identifier_type}:{identity.value}"[:255]


def upsert_subnet(server, dhcp_server, intent: SubnetIntent, summary: ImportSummary, claims: ClaimResult):
    """Get/create the DHCP-plugin ``Subnet`` for *intent*, tracked via ``KeaDhcpLink``.

    Returns the ``netbox_dhcp.Subnet`` instance, or ``None`` on error.
    """
    from django.contrib.contenttypes.models import ContentType

    Subnet = _model("Subnet")
    KeaDhcpLink = _link_model()

    existing = None
    if intent.kea_subnet_id is not None:
        existing = _linked_subnet(server, intent.family, intent.kea_subnet_id)

    changed = False
    try:
        with transaction.atomic():
            # Inside the try so one bad CIDR is counted as a per-subnet error, not fatal.
            network = subnet_network(intent.cidr, intent.family)
            result = claims.prefixes[str(network)]
            if result.prefix is None:
                raise RuntimeError("The Subnet Prefix could not be claimed")
            prefix_obj = result.prefix
            if result.outcome in {"conflict", "disagreement"}:
                summary.warn(f"subnet {network}: IPAM ownership {result.outcome}, Prefix left unchanged")
            if existing is not None:
                if existing.prefix_id != prefix_obj.pk:
                    existing.prefix = prefix_obj
                    changed = True
                if existing.dhcp_server_id != dhcp_server.pk or existing.shared_network_id is not None:
                    existing.dhcp_server = dhcp_server
                    existing.shared_network = None
                    changed = True
                if _apply_inherited_settings(existing, dhcp_server, intent.settings, _SUBNET_FIELDS, summary):
                    changed = True
                if changed:
                    existing.save()
                subnet_obj = existing
            else:
                # Let the plugin auto-allocate its own (global) subnet_id; never write Kea's.
                subnet_obj = Subnet(
                    name=_subnet_name(server, intent, network),
                    prefix=prefix_obj,
                    dhcp_server=dhcp_server,
                    shared_network=None,
                )
                _apply_inherited_settings(subnet_obj, dhcp_server, intent.settings, _SUBNET_FIELDS, summary)
                subnet_obj.save()
                if intent.kea_subnet_id is not None:
                    # Key on the authoritative Kea identity, not the sys4 object: a stale
                    # link (its subnet deleted out from under it) must be *relinked* to the
                    # new subnet, not collide with the keadhcplink_unique_subnet_identity
                    # constraint as a fresh (object_type, object_id) create would.
                    KeaDhcpLink.objects.update_or_create(
                        server=server,
                        family=intent.family,
                        kea_subnet_id=intent.kea_subnet_id,
                        defaults={
                            "kea_identity": None,
                            "object_type": ContentType.objects.get_for_model(Subnet),
                            "object_id": subnet_obj.pk,
                        },
                    )
    except Exception as exc:  # noqa: BLE001 — one bad subnet must not abort the import
        summary.errors += 1
        summary.warn(f"subnet {intent.cidr} (id={intent.kea_subnet_id}): {exc}")
        return None

    if existing is None:
        summary.subnets_created += 1
    elif changed:
        summary.subnets_updated += 1
    if intent.shared_network is not None:
        summary.shared_networks_deferred += 1

    return subnet_obj


def upsert_pools(subnet_obj, intent: SubnetIntent, summary: ImportSummary, dhcp_server, custom_defs, claims):
    """Get/create DHCP-plugin ``Pool`` rows (and their options) for each Kea pool in *intent*."""
    Pool = _model("Pool")
    # upsert_subnet already parsed this CIDR, so this cannot raise.
    network = subnet_network(intent.cidr, intent.family)
    for pool_intent in intent.pools:
        try:
            with transaction.atomic():
                try:
                    pool = parse_pool(pool_intent.pool, network)
                except ValueError:
                    pool = None
                result = claims.ranges.get(f"{pool.start} - {pool.end}") if pool is not None else None
                if result is not None and result.outcome == "error":
                    raise RuntimeError("The allocation Pool could not be claimed")
                range_obj = result.ip_range if result else None
                if result is not None and result.outcome in {"conflict", "disagreement"}:
                    summary.warn(f"pool {pool_intent.pool}: IPAM ownership {result.outcome}, IP Range left unchanged")
                if range_obj is not None:
                    pool_obj, created = Pool.objects.get_or_create(
                        subnet=subnet_obj,
                        ip_range=range_obj,
                        defaults={"name": _pool_name(subnet_obj, pool_intent)},
                    )
        except Exception as exc:  # noqa: BLE001 — one bad pool must not abort the import
            summary.errors += 1
            summary.warn(f"pool {pool_intent.pool} in {intent.cidr}: {exc}")
            continue
        if range_obj is None:
            summary.warn(f"pool {pool_intent.pool} in {intent.cidr}: unusable range, skipped")
            continue
        if created:
            summary.pools_created += 1
        upsert_options(pool_obj, pool_intent.options, intent.family, dhcp_server, custom_defs, summary)


def _reservation_link_identity(reservation: Reservation) -> str:
    """Return the Kea identity a Global Reservation link is keyed on.

    ``KeaDhcpLink.kea_identity`` is sized from ``MAX_IDENTITY_LENGTH``, so every
    identity the Reservation domain accepts fits.
    """
    identity = reservation.identity
    return f"{identity.identifier_type}:{identity.value}"


def _linked_reservation(server, reservation: Reservation):
    """Return the DHCP-plugin reservation linked to this Kea Global identity, or ``None``."""
    KeaDhcpLink = _link_model()
    link = KeaDhcpLink.objects.filter(
        server=server,
        family=reservation.family,
        kea_identity=_reservation_link_identity(reservation),
    ).first()
    return link.sys4_object if link is not None else None


def _link_reservation(server, reservation: Reservation, obj) -> None:
    """Record the ``(server, family, identity)`` link for one Global Reservation.

    Keyed on the Kea identity rather than the row, so a link whose row was deleted is
    relinked instead of colliding with ``keadhcplink_unique_sys4_object``.
    """
    from django.contrib.contenttypes.models import ContentType

    KeaDhcpLink = _link_model()
    KeaDhcpLink.objects.update_or_create(
        server=server,
        family=reservation.family,
        kea_identity=_reservation_link_identity(reservation),
        defaults={
            "object_type": ContentType.objects.get_for_model(obj.__class__),
            "object_id": obj.pk,
        },
    )


def _unlinked(base):
    """Restrict *base* to the rows no Kea link claims yet."""
    from django.contrib.contenttypes.models import ContentType

    KeaDhcpLink = _link_model()
    claimed = KeaDhcpLink.objects.filter(
        object_type=ContentType.objects.get_for_model(base.model),
    ).values_list("object_id", flat=True)
    return base.exclude(pk__in=claimed)


def _find_reservation(base, reservation: Reservation, mac_obj):
    """Find an existing reservation with the same typed identity in the parent scope."""
    id_type = reservation.identity.identifier_type
    identifier = reservation.identity.value
    if id_type == "hw-address":
        return base.filter(hw_address=mac_obj).first() if mac_obj is not None else None
    if id_type == "duid":
        return base.filter(duid=identifier).first()
    if id_type == "circuit-id":
        return base.filter(circuit_id=identifier).first()
    if id_type == "client-id":
        return base.filter(client_id=identifier).first()
    if id_type == "flex-id":
        return base.filter(flex_id=identifier).first()
    return None


def _upsert_reservation(reservation, subnet_obj, server, dhcp_server, custom_defs, summary, claims):
    """Upsert one typed Reservation while preserving its Global or In-Subnet Scope.

    An In-Subnet Reservation matches by identifier inside its Subnet, which already
    fixes the family.  A Global Reservation has no Subnet, and ``netbox_dhcp`` derives a
    reservation's family from one, so its family lives in the ``KeaDhcpLink`` identity
    instead.  Without it one identifier shares a single row between DHCPv4 and DHCPv6,
    merging ``ipv4_address`` and ``ipv6_addresses``.

    Consumes IPAM claim results so the plugin reservation shares the same
    ``ipam.IPAddress``/``dcim.MACAddress`` rows the lease/reservation sync owns.
    """
    HostReservation = _model("HostReservation")
    scope = reservation.scope.subnet.cidr if isinstance(reservation.scope, InSubnetReservationScope) else "global"

    if subnet_obj is not None:
        base = HostReservation.objects.filter(subnet=subnet_obj)
        scope_name = subnet_obj.name
    else:
        base = HostReservation.objects.filter(dhcp_server=dhcp_server, subnet__isnull=True)
        scope_name = f"{dhcp_server.name} DHCPv{reservation.family}"

    try:
        with transaction.atomic():
            # Inside the try so a resolver failure is counted per-reservation, not fatal.
            ipv4_ip, ipv6_ips, mac_obj = _reservation_addresses(reservation, claims, summary)
            if reservation.identity.identifier_type == "hw-address" and mac_obj is None:
                raise RuntimeError("The reservation hardware address could not be resolved.")
            linked = None if subnet_obj is not None else _linked_reservation(server, reservation)
            if linked is not None:
                obj = linked
            elif subnet_obj is not None:
                obj = _find_reservation(base, reservation, mac_obj)
            else:
                # Adopt a Global row imported before the link carried the family, so an
                # upgrade relinks that row instead of duplicating it.
                obj = _find_reservation(_unlinked(base), reservation, mac_obj)
            created = obj is None
            if obj is None:
                obj = HostReservation(
                    subnet=subnet_obj,
                    dhcp_server=None if subnet_obj is not None else dhcp_server,
                    name=_reservation_name(scope_name, reservation),
                )
            elif linked is None and subnet_obj is None:
                # Newly adopted: take the family-qualified name so the other family is free
                # to create its own row under the unique-name constraint.
                obj.name = _reservation_name(scope_name, reservation)
            obj.hostname = reservation.hostname or None
            _apply_reservation_identifier(obj, reservation, mac_obj)
            # One row holds one family. _reservation_addresses returns the other
            # family empty, which also splits a row an earlier import merged.
            obj.ipv4_address = ipv4_ip
            obj.save()
            # An empty address list clears the previous IPv6 relations.
            obj.ipv6_addresses.set(ipv6_ips)
            # Drop obsolete references before the delegated phase evaluates stale links.
            obj.ipv6_prefixes.remove(
                *obj.ipv6_prefixes.exclude(prefix__in=[str(prefix) for prefix in reservation.delegated_prefixes])
            )
            if subnet_obj is None:
                _link_reservation(server, reservation, obj)
        if created:
            summary.reservations_created += 1
        else:
            summary.reservations_updated += 1
    except Exception:
        summary.errors += 1
        logger.exception("Could not import reservation %s in %s", reservation.identity.value, scope)
        summary.warn(f"reservation {reservation.identity.value} in {scope} could not be imported. See server logs.")
        return None
    upsert_options(obj, reservation.options, reservation.family, dhcp_server, custom_defs, summary)
    return obj


def _record_address_claims(report: SyncReport, claims: ClaimResult) -> bool:
    """Keep ownership outcomes separate from the DHCP rows that consume them."""
    report.conflicts.update(claims.conflicts)
    complete = True
    for address, result in claims.addresses.items():
        if result.outcome == "error":
            complete = False
        elif result.outcome == "conflict":
            report.conflicts.add(address)
        elif result.outcome == "disagreement":
            report.disagreements.add(address)
    return complete


def import_reservation_snapshot(
    server,
    dhcp_server,
    observation: ReservationObservation | None,
    custom_defs,
    summary: ImportSummary,
) -> None:
    """Import valid typed records and report quarantined Snapshot records."""
    from ..ipam_reconciliation import DelegatedPrefixPhase, claim, reconcile

    if observation is None:
        summary.reservations_unread = True
        summary.ownership.incomplete.add("reservation")
        return
    snapshot = observation.snapshot
    if snapshot.traversal_truncated:
        summary.reservations_unread = True
    summary.reservations_quarantined += sum(
        diagnostic.code not in TRAVERSAL_DIAGNOSTIC_CODES for diagnostic in snapshot.diagnostics
    )
    for diagnostic in snapshot.diagnostics:
        summary.warn(f"reservation {diagnostic.source_position}: {diagnostic.message}")
    claims = claim(server, snapshot.family, snapshot.records, force=False)
    summary.owner_disagreements += sum(result.outcome == "disagreement" for result in claims.addresses.values())
    imported = []
    complete = snapshot.complete and not snapshot.diagnostics
    complete = _record_address_claims(summary.ownership, claims) and complete
    for reservation in snapshot.records:
        if isinstance(reservation.scope, InSubnetReservationScope):
            subnet_id = reservation.scope.subnet.subnet_id
            subnet_obj = _linked_subnet(server, reservation.family, subnet_id)
            if subnet_obj is None:
                complete = False
                summary.reservations_skipped += 1
                summary.warn(f"reservation for unknown subnet-id {subnet_id} skipped")
                continue
        else:
            subnet_obj = None
        obj = _upsert_reservation(reservation, subnet_obj, server, dhcp_server, custom_defs, summary, claims)
        if obj is None:
            complete = False
        else:
            imported.append((reservation, obj))
    try:
        with transaction.atomic():
            phase = DelegatedPrefixPhase([reservation for reservation, _ in imported], observation.cutoff, complete)
            report = reconcile(server, snapshot.family, [phase])
            for reservation, obj in imported:
                if any(report.prefixes[str(prefix)].outcome == "error" for prefix in reservation.delegated_prefixes):
                    summary.warn(
                        f"reservation {reservation.identity.value}: delegated Prefix claim failed; "
                        "existing reported Prefix attachments were retained"
                    )
                    continue
                prefixes = []
                for prefix in reservation.delegated_prefixes:
                    result = report.prefixes[str(prefix)]
                    if result.prefix is not None:
                        prefixes.append(result.prefix)
                obj.ipv6_prefixes.set(prefixes)
        summary.ownership.merge(report)
        if complete:
            summary.ownership.completed_sources.add("reservation")
        else:
            summary.ownership.incomplete.add("reservation")
        summary.errors += report.errors + report.prefix_errors
        summary.owner_disagreements += len(report.disagreements)
        for address in sorted(report.conflicts):
            summary.warn(f"delegated prefix {address}: IPAM ownership conflict, Prefix left unchanged")
    except Exception:
        summary.errors += 1
        summary.ownership.incomplete.add("delegated-prefix")
        logger.exception("Could not attach delegated Prefixes after DHCP import")
        summary.warn("Delegated Prefixes could not be attached. See server logs.")


def _apply_reservation_identifier(obj, reservation: Reservation, mac_obj) -> None:
    """Set the single Kea identifier on a DHCP-plugin reservation (clearing the others)."""
    identity = reservation.identity
    obj.hw_address = mac_obj if identity.identifier_type == "hw-address" else None
    obj.duid = identity.value if identity.identifier_type == "duid" else None
    obj.circuit_id = identity.value if identity.identifier_type == "circuit-id" else None
    obj.client_id = identity.value if identity.identifier_type == "client-id" else None
    obj.flex_id = identity.value if identity.identifier_type == "flex-id" else None


def _claim_config_networks(server, config):
    """Group every Subnet and Pool in the configuration before claiming either source."""
    from ..ipam_reconciliation import PoolClaim, SubnetClaim, claim

    subnets = []
    pools = []
    for intent in config.subnets:
        try:
            network = subnet_network(intent.cidr, intent.family)
        except ValueError:
            continue
        subnets.append(SubnetClaim(network))
        for pool in intent.pools:
            try:
                pools.append(PoolClaim(parse_pool(pool.pool, network), network))
            except ValueError:  # noqa: PERF203 - preserve valid sibling Pools
                continue
    return claim(server, config.family, subnets, force=False), claim(server, config.family, pools, force=False)


def import_server_config(
    server,
    config: ServerConfigIntent,
    reservation_observation: ReservationObservation | None = None,
) -> ImportSummary:
    """Import one parsed ``(server, family)`` Kea config into the DHCP plugin.

    Idempotent: re-running updates the same rows (subnets via ``KeaDhcpLink``,
    pools/reservations matched structurally) rather than duplicating them.
    """
    summary = ImportSummary()
    dhcp_server = upsert_dhcp_server(server)
    custom_defs = _custom_def_index(config)

    # Global (server-level) tuning fields + options + client classes. DHCPv4 (or the
    # only enabled family) is authoritative for the shared, single DHCPServer row.
    primary = config.family == 4 or not server.dhcp4
    _apply_global_settings(dhcp_server, config.global_settings, summary, primary=primary)
    upsert_options(dhcp_server, config.global_options, config.family, dhcp_server, custom_defs, summary)
    for cc_intent in config.client_classes:
        upsert_client_class(server, dhcp_server, cc_intent, custom_defs, summary)

    prefix_claims, pool_claims = _claim_config_networks(server, config)
    summary.owner_disagreements += sum(
        result.outcome == "disagreement" for result in (*prefix_claims.prefixes.values(), *pool_claims.ranges.values())
    )
    for subnet_intent in config.subnets:
        subnet_obj = upsert_subnet(server, dhcp_server, subnet_intent, summary, prefix_claims)
        if subnet_obj is None:
            continue
        upsert_options(subnet_obj, subnet_intent.options, config.family, dhcp_server, custom_defs, summary)
        upsert_pools(subnet_obj, subnet_intent, summary, dhcp_server, custom_defs, pool_claims)
    for source, outcomes in (("subnet", prefix_claims.prefixes), ("pool", pool_claims.ranges)):
        if any(result.outcome == "error" for result in outcomes.values()):
            summary.ownership.incomplete.add(source)
        else:
            summary.ownership.completed_sources.add(source)
        summary.ownership.conflicts.update(
            address for address, result in outcomes.items() if result.outcome == "conflict"
        )
        summary.ownership.disagreements.update(
            address for address, result in outcomes.items() if result.outcome == "disagreement"
        )
    import_reservation_snapshot(server, dhcp_server, reservation_observation, custom_defs, summary)
    if not config.configuration_complete:
        summary.ownership.incomplete.update({"subnet", "pool"})
    if (
        summary.errors
        or summary.reservations_skipped
        or summary.reservations_quarantined
        or summary.reservations_unread
    ):
        summary.ownership.incomplete.add("reservation")
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
