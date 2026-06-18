# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Pure transforms: a Kea ``DhcpN`` config block → normalized DHCP-plugin intent.

This module is deliberately free of Django, the ORM, and ``netbox_dhcp``: it
only reshapes the dicts that :meth:`KeaClient.command` returns for ``config-get``
into small dataclasses, plus the match keys used to upsert ``netbox_dhcp`` rows
idempotently.  Keeping it pure means it is fully unit-testable in CI, where the
optional DHCP plugin is not installed.

Input shape (the ``Dhcp4``/``Dhcp6`` object from ``config-get``)::

    {
        "subnet4": [{"id": 1, "subnet": "10.0.0.0/24",
                     "pools": [{"pool": "10.0.0.10-10.0.0.200"}],
                     "option-data": [...],
                     "reservations": [{"hw-address": "...", "ip-address": "...",
                                       "hostname": "...", "option-data": [...]}]}],
        "shared-networks": [{"name": "office", "subnet4": [ ...same shape... ]}],
    }

DHCPv6 uses ``subnet6`` and reservations carry ``ip-addresses``/``prefixes``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Kea host-reservation identifier types, in the priority order Kea itself documents
# (host-reservation-identifiers). The first present on a reservation is its identity.
RESERVATION_IDENTIFIER_TYPES: tuple[str, ...] = (
    "hw-address",
    "duid",
    "circuit-id",
    "client-id",
    "flex-id",
)

# Scalar Kea config keys that map onto netbox_dhcp tuning fields (lifetimes, timers,
# lease/DDNS/BOOTP/network settings, and server-level globals).  ``config-get``
# returns these fully defaulted+inherited at both the global and subnet scope; the
# adapter decides which apply to which model and suppresses values inherited from
# the parent.  Captured verbatim here (the Kea→sys4 field mapping lives in the adapter).
KEA_SETTINGS_KEYS: tuple[str, ...] = (
    # lifetimes / timers
    "valid-lifetime",
    "min-valid-lifetime",
    "max-valid-lifetime",
    "preferred-lifetime",
    "min-preferred-lifetime",
    "max-preferred-lifetime",
    "offer-lifetime",
    "renew-timer",
    "rebind-timer",
    # lease behaviour
    "match-client-id",
    "authoritative",
    "reservations-global",
    "reservations-out-of-pool",
    "reservations-in-subnet",
    "calculate-tee-times",
    "t1-percent",
    "t2-percent",
    "cache-threshold",
    "cache-max-age",
    "store-extended-info",
    "allocator",
    "pd-allocator",
    # DDNS
    "ddns-send-updates",
    "ddns-override-no-update",
    "ddns-override-client-update",
    "ddns-replace-client-name",
    "ddns-generated-prefix",
    "ddns-qualifying-suffix",
    "ddns-update-on-renew",
    "ddns-conflict-resolution-mode",
    "ddns-ttl-percent",
    "ddns-ttl",
    "ddns-ttl-min",
    "ddns-ttl-max",
    "hostname-char-set",
    "hostname-char-replacement",
    # BOOTP
    "next-server",
    "server-hostname",
    "boot-file-name",
    # network (subnet)
    "relay",
    "interface-id",
    "rapid-commit",
    # server-level globals
    "decline-probation-period",
    "host-reservation-identifiers",
    "echo-client-id",
    "relay-supplied-options",
    "server-id",
)


def _settings(raw: dict) -> dict:
    """Extract the subset of :data:`KEA_SETTINGS_KEYS` present in a Kea config dict."""
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in KEA_SETTINGS_KEYS if k in raw}


@dataclass(frozen=True)
class OptionIntent:
    """A single Kea ``option-data`` entry, normalized."""

    code: int | None
    name: str | None
    space: str | None
    data: str
    csv_format: bool | None
    always_send: bool | None
    never_send: bool | None = None

    @property
    def match_key(self) -> tuple:
        """Identity within a parent: (space, code) — or (space, name) when code is absent."""
        return (self.space, self.code if self.code is not None else self.name)


@dataclass(frozen=True)
class OptionDefIntent:
    """A Kea custom ``option-def`` entry (defines a non-standard option code)."""

    code: int | None
    name: str | None
    space: str | None
    type: str | None
    array: bool | None
    record_types: tuple[str, ...]
    encapsulate: str | None

    @property
    def match_key(self) -> tuple:
        """Identity: (space, code) — or (space, name) when code is absent."""
        return (self.space, self.code if self.code is not None else self.name)


@dataclass(frozen=True)
class ReservationIntent:
    """A Kea host reservation, normalized to a single identifier + its addresses."""

    family: int
    identifier_type: str | None
    identifier: str | None
    ip_address: str | None
    ip_addresses: tuple[str, ...]
    prefixes: tuple[str, ...]
    hostname: str
    options: tuple[OptionIntent, ...]

    @property
    def match_key(self) -> tuple:
        """Identity within a parent subnet: (identifier_type, identifier)."""
        return (self.identifier_type, self.identifier)

    @property
    def all_addresses(self) -> tuple[str, ...]:
        """Every host IP (v4 single + v6 list), de-duplicated, order-preserving."""
        seen: dict[str, None] = {}
        if self.ip_address:
            seen.setdefault(self.ip_address, None)
        for addr in self.ip_addresses:
            if addr:
                seen.setdefault(addr, None)
        return tuple(seen)


@dataclass(frozen=True)
class PoolIntent:
    """A Kea pool string (``start-end`` range or ``CIDR``) within a subnet."""

    pool: str
    options: tuple[OptionIntent, ...] = ()

    @property
    def match_key(self) -> str:
        """Identity within a subnet: the normalized pool string."""
        return self.pool.strip()


@dataclass(frozen=True)
class SubnetIntent:
    """A Kea subnet, with its parent shared-network (if any) and children."""

    kea_subnet_id: int | None
    cidr: str
    family: int
    shared_network: str | None
    pools: tuple[PoolIntent, ...]
    reservations: tuple[ReservationIntent, ...]
    options: tuple[OptionIntent, ...]
    settings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SharedNetworkIntent:
    """A Kea shared-network (groups subnets)."""

    name: str
    family: int
    options: tuple[OptionIntent, ...]


@dataclass(frozen=True)
class ClientClassIntent:
    """A Kea client-class (classification rule + per-class settings/options)."""

    name: str
    family: int
    test: str
    template_test: str
    only_in_additional_list: bool | None
    options: tuple[OptionIntent, ...]
    settings: dict = field(default_factory=dict)


@dataclass
class ServerConfigIntent:
    """Everything importable from one ``(server, family)`` Kea config block."""

    family: int
    shared_networks: list[SharedNetworkIntent] = field(default_factory=list)
    subnets: list[SubnetIntent] = field(default_factory=list)
    client_classes: list[ClientClassIntent] = field(default_factory=list)
    global_options: tuple[OptionIntent, ...] = ()
    option_defs: tuple[OptionDefIntent, ...] = ()
    global_settings: dict = field(default_factory=dict)


def _option_intent(raw: dict) -> OptionIntent | None:
    """Normalize one ``option-data`` dict; return ``None`` if it is not a dict."""
    if not isinstance(raw, dict):
        return None
    code = raw.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return OptionIntent(
        code=code,
        name=raw.get("name"),
        space=raw.get("space"),
        data=raw.get("data", "") or "",
        csv_format=raw.get("csv-format"),
        always_send=raw.get("always-send"),
        never_send=raw.get("never-send"),
    )


def _options(raw_list) -> tuple[OptionIntent, ...]:
    """Normalize an ``option-data`` list, dropping non-dict entries."""
    if not isinstance(raw_list, list):
        return ()
    out = [_option_intent(o) for o in raw_list]
    return tuple(o for o in out if o is not None)


def _option_def_intent(raw: dict) -> OptionDefIntent | None:
    """Normalize one Kea ``option-def`` dict; return ``None`` if not a dict."""
    if not isinstance(raw, dict):
        return None
    code = raw.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    record_types = raw.get("record-types")
    record_types = tuple(str(r) for r in record_types) if isinstance(record_types, list) else ()
    return OptionDefIntent(
        code=code,
        name=raw.get("name"),
        space=raw.get("space"),
        type=raw.get("type"),
        array=raw.get("array"),
        record_types=record_types,
        encapsulate=raw.get("encapsulate") or None,
    )


def _option_defs(raw_list) -> tuple[OptionDefIntent, ...]:
    """Normalize an ``option-def`` list, dropping non-dict entries."""
    if not isinstance(raw_list, list):
        return ()
    out = [_option_def_intent(o) for o in raw_list]
    return tuple(o for o in out if o is not None)


def _reservation_identifier(raw: dict) -> tuple[str | None, str | None]:
    """Return ``(identifier_type, identifier)`` using Kea's priority order."""
    for id_type in RESERVATION_IDENTIFIER_TYPES:
        value = raw.get(id_type)
        if value:
            return id_type, str(value)
    return None, None


def _reservation_intent(raw: dict, family: int) -> ReservationIntent | None:
    """Normalize one Kea reservation dict; return ``None`` if not a dict."""
    if not isinstance(raw, dict):
        return None
    id_type, identifier = _reservation_identifier(raw)
    ip_addresses = raw.get("ip-addresses")
    ip_addresses = tuple(a for a in ip_addresses if a) if isinstance(ip_addresses, list) else ()
    prefixes = raw.get("prefixes")
    prefixes = tuple(p for p in prefixes if p) if isinstance(prefixes, list) else ()
    return ReservationIntent(
        family=family,
        identifier_type=id_type,
        identifier=identifier,
        ip_address=raw.get("ip-address") or None,
        ip_addresses=ip_addresses,
        prefixes=prefixes,
        hostname=raw.get("hostname", "") or "",
        options=_options(raw.get("option-data")),
    )


def _pools(raw_list) -> tuple[PoolIntent, ...]:
    """Normalize a subnet's ``pools`` list to non-empty pool strings (with options)."""
    if not isinstance(raw_list, list):
        return ()
    out: list[PoolIntent] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        pool = entry.get("pool")
        if pool:
            out.append(PoolIntent(pool=pool, options=_options(entry.get("option-data"))))
    return tuple(out)


def _subnet_intent(raw: dict, family: int, shared_network: str | None) -> SubnetIntent | None:
    """Normalize one Kea subnet dict; return ``None`` when it has no CIDR."""
    if not isinstance(raw, dict):
        return None
    cidr = raw.get("subnet")
    if not cidr:
        return None
    sid = raw.get("id")
    try:
        sid = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        sid = None
    reservations = tuple(
        r for r in (_reservation_intent(x, family) for x in raw.get("reservations") or []) if r is not None
    )
    return SubnetIntent(
        kea_subnet_id=sid,
        cidr=cidr,
        family=family,
        shared_network=shared_network,
        pools=_pools(raw.get("pools")),
        reservations=reservations,
        options=_options(raw.get("option-data")),
        settings=_settings(raw),
    )


def _client_class_intent(raw: dict, family: int) -> ClientClassIntent | None:
    """Normalize one Kea ``client-classes`` entry; return ``None`` if unusable."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not name:
        return None
    # Kea renamed ``only-if-required`` → ``only-in-additional-list`` (2.7.4); accept either.
    only = raw.get("only-in-additional-list")
    if only is None:
        only = raw.get("only-if-required")
    return ClientClassIntent(
        name=name,
        family=family,
        test=raw.get("test", "") or "",
        template_test=raw.get("template-test", "") or "",
        only_in_additional_list=only,
        options=_options(raw.get("option-data")),
        settings=_settings(raw),
    )


def parse_dhcp_config(conf: dict, version: int) -> ServerConfigIntent:
    """Parse a Kea ``Dhcp4``/``Dhcp6`` config block into a :class:`ServerConfigIntent`.

    Standalone subnets (under ``subnetN``) carry ``shared_network=None``; subnets
    nested in a ``shared-networks`` entry carry that network's name.  Malformed
    entries (non-dicts, subnets without a CIDR) are skipped rather than raising,
    so a partially-malformed live config still imports what it can.
    """
    if version not in (4, 6):
        raise ValueError(f"version must be 4 or 6, got {version!r}")
    family = version
    subnet_key = f"subnet{version}"
    result = ServerConfigIntent(family=family)

    if not isinstance(conf, dict):
        return result

    result.global_options = _options(conf.get("option-data"))
    result.option_defs = _option_defs(conf.get("option-def"))
    result.global_settings = _settings(conf)

    for raw_cc in conf.get("client-classes") or []:
        cc = _client_class_intent(raw_cc, family)
        if cc is not None:
            result.client_classes.append(cc)

    for raw in conf.get(subnet_key) or []:
        subnet = _subnet_intent(raw, family, shared_network=None)
        if subnet is not None:
            result.subnets.append(subnet)

    for raw_net in conf.get("shared-networks") or []:
        if not isinstance(raw_net, dict):
            continue
        name = raw_net.get("name")
        if not name:
            continue
        result.shared_networks.append(
            SharedNetworkIntent(name=name, family=family, options=_options(raw_net.get("option-data")))
        )
        for raw in raw_net.get(subnet_key) or []:
            subnet = _subnet_intent(raw, family, shared_network=name)
            if subnet is not None:
                result.subnets.append(subnet)

    return result
