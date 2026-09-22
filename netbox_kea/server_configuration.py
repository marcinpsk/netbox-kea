from __future__ import annotations

import ipaddress
import logging
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from . import constants
from .constants import Family, IPAddressValue, IPNetworkValue
from .dhcp_options import DHCPOption, parse_dhcp_option
from .kea import KeaException
from .models import Server
from .utilities import kea_error_hint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Diagnostic:
    """One safe explanation for omitted or incomplete catalogue facts."""

    code: str
    message: str
    source: str
    path: str = ""


@dataclass(frozen=True)
class Pool:
    """One normalized inclusive allocation range within a Subnet."""

    start: IPAddressValue
    end: IPAddressValue

    @property
    def range(self) -> str:
        """Return the normalized explicit range text."""
        return f"{self.start}-{self.end}"


@dataclass(frozen=True)
class SubnetSettings:
    """Effective typed DHCP settings that the repository currently consumes."""

    valid_lifetime: int | None = None
    min_valid_lifetime: int | None = None
    max_valid_lifetime: int | None = None
    preferred_lifetime: int | None = None
    min_preferred_lifetime: int | None = None
    max_preferred_lifetime: int | None = None
    offer_lifetime: int | None = None
    renew_timer: int | None = None
    rebind_timer: int | None = None
    allocator: str | None = None
    pd_allocator: str | None = None
    ddns_qualifying_suffix: str | None = None
    interface_id: str | None = None
    relay_addresses: tuple[IPAddressValue, ...] = ()
    client_classes: tuple[str, ...] = ()
    require_client_classes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubnetConfiguration:
    """Validated full configuration facts for one Subnet."""

    pools: tuple[Pool, ...]
    options: tuple[DHCPOption, ...]
    settings: SubnetSettings


@dataclass(frozen=True)
class DeclaredSubnet:
    """One configuration declaration without verified Subnet identity."""

    declared_cidr: str
    declared_subnet_id: int | None
    configuration: SubnetConfiguration
    shared_network_name: str | None


@dataclass(frozen=True)
class SharedNetwork:
    """One named Shared Network, including networks with no members."""

    name: str
    description: str | None
    interface: str | None
    relay_addresses: tuple[IPAddressValue, ...]
    options: tuple[DHCPOption, ...]
    member_cidrs: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class OptionDefinition:
    """One validated custom DHCP Option definition."""

    code: int
    name: str
    space: str
    type: str
    array: bool
    encapsulate: str | None
    record_types: tuple[str, ...]


@dataclass(frozen=True)
class ServerConfigurationSnapshot:
    """Immutable configuration facts observed for one Server and family."""

    server_id: int
    family: Family
    observed_at: datetime
    subnets: tuple[DeclaredSubnet, ...]
    shared_networks: tuple[SharedNetwork, ...]
    global_options: tuple[DHCPOption, ...]
    option_definitions: tuple[OptionDefinition, ...]
    diagnostics: tuple[Diagnostic, ...]
    configuration_hash: str | None
    available: bool
    complete: bool
    _membership_complete: tuple[bool, ...] = ()
    # Every server-global option-data entry parsed.
    global_options_complete: bool = False


def _validate_family(family: int) -> Family:
    # Return the literals themselves; mypy cannot narrow int through `in (4, 6)`.
    if family == 4:
        return 4
    if family == 6:
        return 6
    raise ValueError(f"family must be 4 or 6, got {family!r}")


def _network(value: Any, family: Family) -> IPNetworkValue:
    if not isinstance(value, str) or not value:
        raise ValueError("Subnet CIDR must be a non-empty string.")
    network_class = ipaddress.IPv4Network if family == 4 else ipaddress.IPv6Network
    return network_class(value, strict=True)


def _generation_key(server: Server, family: Family) -> str:
    return f"netbox_kea:server_configuration:v1:{_require_persisted_server(server)}:{family}:generation"


def _cache_generation(server: Server, family: Family) -> str:
    key = _generation_key(server, family)
    generation = cache.get(key)
    if isinstance(generation, str):
        return generation
    candidate = secrets.token_hex(16)
    cache.add(key, candidate, timeout=None)
    generation = cache.get(key)
    return generation if isinstance(generation, str) else candidate


def _cache_key(server: Server, family: Family, generation: str | None = None) -> str:
    generation = generation or _cache_generation(server, family)
    return f"netbox_kea:server_configuration:v1:{_require_persisted_server(server)}:{family}:snapshot:{generation}"


def _require_persisted_server(server: Server) -> int:
    if server.pk is None:
        raise ValueError("The Subnet Catalogue requires a persisted Server.")
    return server.pk


def invalidate(server: Server, family: int) -> None:
    """Rotate the shared configuration and Catalogue cache generation."""
    validated_family = _validate_family(family)
    cache.set(_generation_key(server, validated_family), secrets.token_hex(16), timeout=None)


def _diagnostic(code: str, message: str, source: str, path: str = "") -> Diagnostic:
    return Diagnostic(code=code, message=message, source=source, path=path)


def _unavailable(server: Server, family: Family, code: str, message: str) -> ServerConfigurationSnapshot:
    return ServerConfigurationSnapshot(
        server_id=server.pk,
        family=family,
        observed_at=timezone.now(),
        subnets=(),
        shared_networks=(),
        global_options=(),
        option_definitions=(),
        diagnostics=(_diagnostic(code, message, "configuration"),),
        configuration_hash=None,
        available=False,
        complete=False,
    )


def _read_live(server: Server, family: Family) -> ServerConfigurationSnapshot:
    try:
        client = server.get_client(version=family)
        response = client.command("config-get", service=[f"dhcp{family}"])
    except KeaException as exc:
        logger.warning("Subnet configuration read failed for DHCPv%s", family, exc_info=True)
        return _unavailable(
            server,
            family,
            "configuration-unavailable",
            f"Kea Subnet configuration facts are unavailable. {kea_error_hint(exc)}",
        )
    except (OSError, ValueError, RuntimeError):
        logger.warning("Subnet configuration read failed for DHCPv%s", family, exc_info=True)
        return _unavailable(
            server,
            family,
            "configuration-unavailable",
            "Kea Subnet configuration facts are unavailable.",
        )

    if not response or not isinstance(response[0], dict):
        return _unavailable(
            server,
            family,
            "malformed-configuration-response",
            "Kea returned a malformed configuration response.",
        )
    arguments = response[0].get("arguments")
    if not isinstance(arguments, dict):
        return _unavailable(
            server,
            family,
            "malformed-configuration-response",
            "Kea returned malformed configuration arguments.",
        )
    configuration = arguments.get(f"Dhcp{family}")
    if not isinstance(configuration, dict):
        return _unavailable(
            server,
            family,
            "malformed-configuration-response",
            f"Kea did not return a Dhcp{family} configuration object.",
        )
    configuration_hash = arguments.get("hash")
    if not isinstance(configuration_hash, str) or not configuration_hash:
        configuration_hash = None
    return _parse_configuration(server, configuration, family, configuration_hash)


def _parse_configuration(
    server: Server,
    configuration: dict[str, Any],
    family: Family,
    configuration_hash: str | None,
) -> ServerConfigurationSnapshot:
    diagnostics: list[Diagnostic] = []
    facts: list[DeclaredSubnet] = []
    membership_complete: list[bool] = []
    networks: list[SharedNetwork] = []
    subnet_key = f"subnet{family}"

    standalone = configuration.get(subnet_key, [])
    if not isinstance(standalone, list):
        diagnostics.append(
            _diagnostic(
                "invalid-subnet-collection",
                f"Kea returned a non-list {subnet_key} collection.",
                "configuration",
                subnet_key,
            )
        )
        standalone = []
    for index, entry in enumerate(standalone):
        fact = _parse_configured_fact(
            entry,
            family,
            None,
            f"{subnet_key}[{index}]",
            diagnostics,
        )
        if fact is not None:
            facts.append(fact)
            membership_complete.append(True)

    shared_networks = configuration.get("shared-networks", [])
    if not isinstance(shared_networks, list):
        diagnostics.append(
            _diagnostic(
                "invalid-shared-network-collection",
                "Kea returned a non-list Shared Network collection.",
                "configuration",
                "shared-networks",
            )
        )
        shared_networks = []
    shared_names = _shared_network_names(shared_networks)
    for index, shared_network in enumerate(shared_networks):
        path = f"shared-networks[{index}]"
        if not isinstance(shared_network, dict):
            diagnostics.append(
                _diagnostic(
                    "invalid-shared-network", "Kea returned a non-object Shared Network.", "configuration", path
                )
            )
            continue
        name = shared_network.get("name")
        valid_name = isinstance(name, str) and bool(name) and shared_names.get(name) == 1
        if not valid_name:
            diagnostics.append(
                _diagnostic(
                    "invalid-shared-network",
                    "Kea returned an invalid or duplicate Shared Network name.",
                    "configuration",
                    f"{path}.name",
                )
            )
            name = None
        members = shared_network.get(subnet_key, [])
        if not isinstance(members, list):
            diagnostics.append(
                _diagnostic(
                    "invalid-subnet-collection",
                    f"Kea returned a non-list {subnet_key} collection.",
                    "configuration",
                    f"{path}.{subnet_key}",
                )
            )
            members = []
        member_cidrs: list[str] = []
        for member_index, entry in enumerate(members):
            fact = _parse_configured_fact(
                entry,
                family,
                name,
                f"{path}.{subnet_key}[{member_index}]",
                diagnostics,
            )
            if fact is not None:
                facts.append(fact)
                membership_complete.append(valid_name)
                member_cidrs.append(fact.declared_cidr)
        if valid_name and isinstance(name, str):
            network_diagnostics = len(diagnostics)
            description = _optional_string(shared_network, "description", path, diagnostics, allow_empty=True)
            interface = _optional_string(shared_network, "interface", path, diagnostics)
            relay_addresses = _relay_addresses(shared_network.get("relay"), family, path, diagnostics)
            network_options = _parse_options(shared_network.get("option-data", []), path, diagnostics)
            networks.append(
                SharedNetwork(
                    name=name,
                    description=description,
                    interface=interface,
                    relay_addresses=relay_addresses,
                    options=network_options,
                    member_cidrs=tuple(member_cidrs),
                    complete=len(diagnostics) == network_diagnostics,
                )
            )

    global_diagnostics = len(diagnostics)
    options = _parse_options(configuration.get("option-data", []), f"Dhcp{family}", diagnostics)
    global_options_complete = len(diagnostics) == global_diagnostics
    definitions = _parse_definitions(configuration.get("option-def", []), family, diagnostics)
    return ServerConfigurationSnapshot(
        server_id=server.pk,
        family=family,
        observed_at=timezone.now(),
        subnets=tuple(facts),
        shared_networks=tuple(networks),
        global_options=options,
        option_definitions=definitions,
        diagnostics=tuple(diagnostics),
        available=True,
        complete=not diagnostics,
        configuration_hash=configuration_hash,
        _membership_complete=tuple(membership_complete),
        global_options_complete=global_options_complete,
    )


def _shared_network_names(entries: list[Any]) -> dict[str, int]:
    names: dict[str, int] = defaultdict(int)
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
            names[entry["name"]] += 1
    return names


def _parse_configured_fact(
    entry: Any,
    family: Family,
    shared_network_name: str | None,
    path: str,
    diagnostics: list[Diagnostic],
) -> DeclaredSubnet | None:
    if not isinstance(entry, dict):
        diagnostics.append(_diagnostic("invalid-subnet", "Kea returned a non-object Subnet.", "configuration", path))
        return None
    try:
        network = _network(entry.get("subnet"), family)
    except ValueError:
        diagnostics.append(
            _diagnostic("invalid-subnet-cidr", "Kea returned an invalid Subnet CIDR.", "configuration", path)
        )
        return None
    subnet_id = entry.get("id")
    if subnet_id is not None and (
        isinstance(subnet_id, bool) or not isinstance(subnet_id, int) or not 1 <= subnet_id <= 4_294_967_294
    ):
        diagnostics.append(
            _diagnostic("invalid-subnet-id", "Kea returned an invalid subnet ID.", "configuration", f"{path}.id")
        )
        subnet_id = None
    return DeclaredSubnet(
        declared_cidr=str(network),
        declared_subnet_id=subnet_id,
        shared_network_name=shared_network_name,
        configuration=SubnetConfiguration(
            pools=_parse_pools(entry.get("pools", []), network, path, diagnostics),
            options=_parse_options(entry.get("option-data", []), path, diagnostics),
            settings=_parse_settings(entry, family, path, diagnostics),
        ),
    )


def _parse_pools(
    entries: Any,
    subnet: IPNetworkValue,
    path: str,
    diagnostics: list[Diagnostic],
) -> tuple[Pool, ...]:
    if not isinstance(entries, list):
        diagnostics.append(
            _diagnostic("invalid-pool-collection", "Kea returned a non-list Pool collection.", "configuration", path)
        )
        return ()
    pools: list[Pool] = []
    for index, entry in enumerate(entries):
        pool_path = f"{path}.pools[{index}]"
        raw_pool = entry.get("pool") if isinstance(entry, dict) else None
        try:
            pool = _parse_pool(raw_pool, subnet)
        except (TypeError, ValueError):
            diagnostics.append(_diagnostic("invalid-pool", "Kea returned an invalid Pool.", "configuration", pool_path))
            continue
        pools.append(pool)
    return tuple(pools)


def _parse_pool(value: Any, subnet: IPNetworkValue) -> Pool:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Pool must be a non-empty string.")
    value = value.strip()
    if "/" in value:
        pool_network = ipaddress.ip_network(value, strict=True)
        if pool_network.version != subnet.version or not (
            int(pool_network.network_address) >= int(subnet.network_address)
            and int(pool_network.broadcast_address) <= int(subnet.broadcast_address)
        ):
            raise ValueError("Pool prefix is outside its Subnet.")
        return Pool(start=pool_network.network_address, end=pool_network.broadcast_address)
    parts = [part.strip() for part in value.split("-")]
    if len(parts) != 2 or not all(parts):
        raise ValueError("Pool range must have two endpoints.")
    start = ipaddress.ip_address(parts[0])
    end = ipaddress.ip_address(parts[1])
    if start.version != subnet.version or end.version != subnet.version:
        raise ValueError("Pool address family does not match its Subnet.")
    if int(start) > int(end) or start not in subnet or end not in subnet:
        raise ValueError("Pool range is outside its Subnet.")
    return Pool(start=start, end=end)


def _parse_options(entries: Any, path: str, diagnostics: list[Diagnostic]) -> tuple[DHCPOption, ...]:
    if not isinstance(entries, list):
        diagnostics.append(
            _diagnostic(
                "invalid-option-collection", "Kea returned a non-list option collection.", "configuration", path
            )
        )
        return ()
    options: list[DHCPOption] = []
    for index, entry in enumerate(entries):
        option_path = f"{path}.option-data[{index}]"
        try:
            option = parse_dhcp_option(entry)
        except ValueError:
            diagnostics.append(
                _diagnostic("invalid-option", "Kea returned an invalid Subnet option.", "configuration", option_path)
            )
            continue
        options.append(option)
    return tuple(options)


def _parse_settings(
    entry: dict[str, Any],
    family: Family,
    path: str,
    diagnostics: list[Diagnostic],
) -> SubnetSettings:
    return SubnetSettings(
        valid_lifetime=_optional_nonnegative_int(entry, "valid-lifetime", path, diagnostics),
        min_valid_lifetime=_optional_nonnegative_int(entry, "min-valid-lifetime", path, diagnostics),
        max_valid_lifetime=_optional_nonnegative_int(entry, "max-valid-lifetime", path, diagnostics),
        preferred_lifetime=_optional_nonnegative_int(entry, "preferred-lifetime", path, diagnostics),
        min_preferred_lifetime=_optional_nonnegative_int(entry, "min-preferred-lifetime", path, diagnostics),
        max_preferred_lifetime=_optional_nonnegative_int(entry, "max-preferred-lifetime", path, diagnostics),
        offer_lifetime=_optional_nonnegative_int(entry, "offer-lifetime", path, diagnostics),
        renew_timer=_optional_nonnegative_int(entry, "renew-timer", path, diagnostics),
        rebind_timer=_optional_nonnegative_int(entry, "rebind-timer", path, diagnostics),
        allocator=_optional_string(entry, "allocator", path, diagnostics),
        pd_allocator=_optional_string(entry, "pd-allocator", path, diagnostics),
        ddns_qualifying_suffix=_optional_string(entry, "ddns-qualifying-suffix", path, diagnostics, allow_empty=True),
        interface_id=_optional_string(entry, "interface-id", path, diagnostics),
        relay_addresses=_relay_addresses(entry.get("relay"), family, path, diagnostics),
        client_classes=_client_classes(entry, path, diagnostics),
        require_client_classes=_additional_classes(entry, path, diagnostics),
    )


def _optional_nonnegative_int(
    entry: dict[str, Any],
    key: str,
    path: str,
    diagnostics: list[Diagnostic],
) -> int | None:
    if key not in entry:
        return None
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        diagnostics.append(
            _diagnostic("invalid-setting", f"Kea returned an invalid {key} setting.", "configuration", f"{path}.{key}")
        )
        return None
    return value


def _optional_string(
    entry: dict[str, Any],
    key: str,
    path: str,
    diagnostics: list[Diagnostic],
    *,
    allow_empty: bool = False,
) -> str | None:
    if key not in entry:
        return None
    value = entry[key]
    if not isinstance(value, str) or (not allow_empty and not value):
        diagnostics.append(
            _diagnostic("invalid-setting", f"Kea returned an invalid {key} setting.", "configuration", f"{path}.{key}")
        )
        return None
    return value


def _relay_addresses(
    value: Any,
    family: Family,
    path: str,
    diagnostics: list[Diagnostic],
) -> tuple[IPAddressValue, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or not isinstance(value.get("ip-addresses"), list):
        diagnostics.append(
            _diagnostic("invalid-setting", "Kea returned invalid relay addresses.", "configuration", f"{path}.relay")
        )
        return ()
    addresses: list[IPAddressValue] = []
    for address in value["ip-addresses"]:
        if not isinstance(address, str):
            diagnostics.append(
                _diagnostic(
                    "invalid-setting",
                    "Kea returned an invalid relay address.",
                    "configuration",
                    f"{path}.relay.ip-addresses",
                )
            )
            continue
        try:
            parsed = ipaddress.ip_address(address)
        except (TypeError, ValueError):
            diagnostics.append(
                _diagnostic(
                    "invalid-setting",
                    "Kea returned an invalid relay address.",
                    "configuration",
                    f"{path}.relay.ip-addresses",
                )
            )
            continue
        if parsed.version != family:
            diagnostics.append(
                _diagnostic(
                    "invalid-setting",
                    "Kea returned a relay address for the wrong family.",
                    "configuration",
                    f"{path}.relay.ip-addresses",
                )
            )
            continue
        addresses.append(parsed)
    return tuple(addresses)


def _additional_classes(
    entry: dict[str, Any],
    path: str,
    diagnostics: list[Diagnostic],
) -> tuple[str, ...]:
    """Read the additional-class list, preferring the current Kea key.

    Kea 3.0 renamed ``require-client-classes`` to ``evaluate-additional-classes`` and
    refuses a configuration that sets both, so ``config-get`` returns exactly one of
    them. Kea before 3.0 returns only the old name.
    """
    if "evaluate-additional-classes" in entry:
        return _string_tuple(entry, "evaluate-additional-classes", path, diagnostics)
    return _string_tuple(entry, "require-client-classes", path, diagnostics)


def _client_classes(
    entry: dict[str, Any],
    path: str,
    diagnostics: list[Diagnostic],
) -> tuple[str, ...]:
    """Read client restrictions, preferring the current list-valued Kea key."""
    if "client-classes" in entry:
        return _string_tuple(entry, "client-classes", path, diagnostics)
    legacy = _optional_string(entry, "client-class", path, diagnostics)
    return (legacy,) if legacy is not None else ()


def _string_tuple(
    entry: dict[str, Any],
    key: str,
    path: str,
    diagnostics: list[Diagnostic],
) -> tuple[str, ...]:
    if key not in entry:
        return ()
    value = entry[key]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        diagnostics.append(
            _diagnostic("invalid-setting", f"Kea returned an invalid {key} setting.", "configuration", f"{path}.{key}")
        )
        return ()
    return tuple(value)


def _parse_definition(entry: Any, family: Family) -> OptionDefinition:
    if not isinstance(entry, dict):
        raise ValueError("An Option Definition must be an object.")
    code = entry.get("code")
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 65_535:
        raise ValueError("An Option Definition requires a valid code.")
    name, space, kind = entry.get("name"), entry.get("space", f"dhcp{family}"), entry.get("type")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(space, str)
        or not space
        or not isinstance(kind, str)
        or not kind
    ):
        raise ValueError("An Option Definition requires name, space and type strings.")
    array = entry.get("array", False)
    encapsulate = entry.get("encapsulate", "")
    record_types = entry.get("record-types", "")
    if not isinstance(array, bool) or not isinstance(encapsulate, str) or not isinstance(record_types, str):
        raise ValueError("An Option Definition has invalid field types.")
    records = tuple(value.strip() for value in record_types.split(",")) if record_types else ()
    if any(not value for value in records):
        raise ValueError("An Option Definition has an empty record type.")
    return OptionDefinition(code, name, space, kind, array, encapsulate or None, records)


def _parse_definitions(entries: Any, family: Family, diagnostics: list[Diagnostic]) -> tuple[OptionDefinition, ...]:
    if not isinstance(entries, list):
        diagnostics.append(
            _diagnostic(
                "invalid-option-definition-collection",
                "Kea returned a non-list Option Definition collection.",
                "configuration",
                "option-def",
            )
        )
        return ()
    definitions: list[OptionDefinition] = []
    for index, entry in enumerate(entries):
        try:
            definitions.append(_parse_definition(entry, family))
        except ValueError:  # noqa: PERF203
            diagnostics.append(
                _diagnostic(
                    "invalid-option-definition",
                    "Kea returned an invalid Option Definition.",
                    "configuration",
                    f"option-def[{index}]",
                )
            )
    return tuple(definitions)


def display(server: Server, family: int) -> ServerConfigurationSnapshot:
    """Return cached configuration facts for interactive presentation."""
    validated_family = _validate_family(family)
    key = _cache_key(server, validated_family)
    cached = cache.get(key)
    if isinstance(cached, ServerConfigurationSnapshot):
        return cached
    snapshot = _read_live(server, validated_family)
    if snapshot.available:
        cache.set(key, snapshot, constants.SUBNET_CHOICES_TTL)
    return snapshot


def for_verification(server: Server, family: int) -> ServerConfigurationSnapshot:
    """Read live configuration facts without consulting or filling either cache."""
    validated_family = _validate_family(family)
    _require_persisted_server(server)
    return _read_live(server, validated_family)
