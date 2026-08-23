from __future__ import annotations

import ipaddress
import logging
from collections import defaultdict
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar

import requests
from django.core.cache import cache
from django.utils import timezone

from . import constants
from .kea import KeaClient, KeaException
from .models import Server
from .utilities import kea_error_hint

logger = logging.getLogger(__name__)

Family = Literal[4, 6]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Kea requires subnet IDs greater than zero and less than UINT32_MAX.
MIN_SUBNET_ID = 1
MAX_SUBNET_ID = 4_294_967_294


class CatalogueUnavailable(RuntimeError):
    """Raised when a requested operation needs facts the catalogue cannot verify."""


class SubnetIdentityConflict(ValueError):
    """Raised when a proposed Subnet identity already exists."""


class SubnetIdExhausted(RuntimeError):
    """Raised when no valid Kea subnet ID remains for automatic allocation."""


@dataclass(frozen=True)
class Diagnostic:
    """One safe explanation for omitted or incomplete catalogue facts."""

    code: str
    message: str
    source: str
    path: str = ""


@dataclass(frozen=True)
class SubnetIdentity:
    """The canonical CIDR and Kea ID of one Subnet."""

    subnet_id: int
    network: IPNetwork

    @property
    def cidr(self) -> str:
        """Return the canonical CIDR text."""
        return str(self.network)


class _CollisionFact(Protocol):
    @property
    def identity(self) -> SubnetIdentity:
        """Return the fact's Subnet identity."""
        ...


_CollisionFactT = TypeVar("_CollisionFactT", bound=_CollisionFact)


@dataclass(frozen=True)
class NewSubnetIdentity:
    """A live-verified Subnet identity that is available for creation."""

    subnet_id: int
    network: IPNetwork

    @property
    def cidr(self) -> str:
        """Return the canonical CIDR text."""
        return str(self.network)


@dataclass(frozen=True)
class SharedNetworkMembership:
    """The named Kea Shared Network that contains a Subnet."""

    name: str


@dataclass(frozen=True)
class Pool:
    """One normalized inclusive allocation range within a Subnet."""

    start: IPAddress
    end: IPAddress

    @property
    def range(self) -> str:
        """Return the normalized explicit range text."""
        return f"{self.start}-{self.end}"


@dataclass(frozen=True)
class SubnetOption:
    """One validated option declared locally on a Subnet."""

    code: int | None
    name: str | None
    space: str | None
    data: str
    csv_format: bool | None
    always_send: bool | None
    never_send: bool | None


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
    relay_addresses: tuple[IPAddress, ...] = ()
    client_classes: tuple[str, ...] = ()
    require_client_classes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubnetConfiguration:
    """Validated full configuration facts for one Subnet."""

    pools: tuple[Pool, ...]
    options: tuple[SubnetOption, ...]
    settings: SubnetSettings


@dataclass(frozen=True)
class VerifiedSubnet:
    """A unique Subnet identity with optional full configuration facts."""

    identity: SubnetIdentity
    configuration: SubnetConfiguration | None
    shared_network: SharedNetworkMembership | None

    @property
    def subnet_id(self) -> int:
        """Return the Kea subnet ID."""
        return self.identity.subnet_id

    @property
    def network(self) -> IPNetwork:
        """Return the canonical IP network."""
        return self.identity.network

    @property
    def cidr(self) -> str:
        """Return the canonical CIDR text."""
        return self.identity.cidr


@dataclass(frozen=True)
class ConfiguredSubnet:
    """Validated configuration facts whose identity is not verified."""

    candidate_identity: SubnetIdentity
    configuration: SubnetConfiguration
    shared_network: SharedNetworkMembership | None


@dataclass(frozen=True)
class CatalogueSnapshot:
    """A time-bounded immutable observation of one Subnet Catalogue."""

    server_id: int
    family: Family
    observed_at: datetime
    subnets: tuple[VerifiedSubnet, ...]
    configured_subnets: tuple[ConfiguredSubnet, ...]
    diagnostics: tuple[Diagnostic, ...]
    identity_complete: bool
    configuration_complete: bool
    consistent: bool
    configuration_hash: str | None

    @property
    def subnet_choices(self) -> tuple[tuple[str, int], ...]:
        """Return verified identities in network order for user choices."""
        return tuple((subnet.cidr, subnet.subnet_id) for subnet in self.subnets)

    @property
    def unavailable(self) -> bool:
        """Return true when no safe fact from either source remains usable."""
        return (
            not self.subnets
            and not self.configured_subnets
            and (not (self.identity_complete or self.configuration_complete) or not self.consistent)
        )

    def find_by_id(self, subnet_id: int) -> VerifiedSubnet | None:
        """Return the verified Subnet with an exact Kea ID, if present."""
        return next((subnet for subnet in self.subnets if subnet.subnet_id == subnet_id), None)

    def find_by_cidr(self, cidr: str) -> VerifiedSubnet | None:
        """Return the verified Subnet with an exact canonical CIDR, if present."""
        network = _network(cidr, self.family)
        return next((subnet for subnet in self.subnets if subnet.network == network), None)


@dataclass(frozen=True)
class CompleteCatalogueSnapshot(CatalogueSnapshot):
    """A snapshot with complete, consistent identity and configuration facts."""


@dataclass(frozen=True)
class IncompleteCatalogueSnapshot(CatalogueSnapshot):
    """A snapshot that preserves safe facts and explains every omitted fact."""


@dataclass(frozen=True)
class IdentityOnlyCatalogueSnapshot(IncompleteCatalogueSnapshot):
    """An incomplete snapshot with verified identity facts but no configuration source."""


@dataclass(frozen=True)
class ConfigurationOnlyCatalogueSnapshot(IncompleteCatalogueSnapshot):
    """An incomplete snapshot with configuration facts but no identity authority."""


@dataclass(frozen=True)
class _IdentityFact:
    identity: SubnetIdentity
    shared_network_name: str | None
    membership_complete: bool


@dataclass(frozen=True)
class _ConfiguredFact:
    identity: SubnetIdentity
    configuration: SubnetConfiguration
    shared_network_name: str | None
    membership_complete: bool


@dataclass(frozen=True)
class _IdentityObservation:
    facts: tuple[_IdentityFact, ...]
    diagnostics: tuple[Diagnostic, ...]
    available: bool
    complete: bool
    quarantined_ids: frozenset[int] = frozenset()
    quarantined_networks: frozenset[IPNetwork] = frozenset()


@dataclass(frozen=True)
class _ConfigurationObservation:
    facts: tuple[_ConfiguredFact, ...]
    diagnostics: tuple[Diagnostic, ...]
    available: bool
    complete: bool
    configuration_hash: str | None = None
    quarantined_ids: frozenset[int] = frozenset()
    quarantined_networks: frozenset[IPNetwork] = frozenset()


def _validate_family(family: int) -> Family:
    if family not in (4, 6):
        raise ValueError(f"family must be 4 or 6, got {family!r}")
    return family


def _network(value: str, family: Family) -> IPNetwork:
    if not isinstance(value, str) or not value:
        raise ValueError("Subnet CIDR must be a non-empty string.")
    network_class = ipaddress.IPv4Network if family == 4 else ipaddress.IPv6Network
    return network_class(value, strict=True)


def _cache_key(server: Server, family: Family) -> str:
    return f"netbox_kea:subnet_catalogue:v1:{_require_persisted_server(server)}:{family}"


def _require_persisted_server(server: Server) -> int:
    if server.pk is None:
        raise ValueError("The Subnet Catalogue requires a persisted Server.")
    return server.pk


def invalidate(server: Server, family: int) -> None:
    """Discard the cached Complete snapshot after a configuration change."""
    cache.delete(_cache_key(server, _validate_family(family)))


def _diagnostic(code: str, message: str, source: str, path: str = "") -> Diagnostic:
    return Diagnostic(code=code, message=message, source=source, path=path)


def _unavailable_identity(code: str, message: str) -> _IdentityObservation:
    return _IdentityObservation(
        facts=(),
        diagnostics=(_diagnostic(code, message, "identity"),),
        available=False,
        complete=False,
    )


def _unavailable_configuration(code: str, message: str) -> _ConfigurationObservation:
    return _ConfigurationObservation(
        facts=(),
        diagnostics=(_diagnostic(code, message, "configuration"),),
        available=False,
        complete=False,
    )


def _read_identity(client: KeaClient, family: Family) -> _IdentityObservation:
    command = f"subnet{family}-list"
    try:
        response = client.command(command, service=[f"dhcp{family}"], check=(0, 3))
    except KeaException as exc:
        if exc.response.get("result") == 2:
            return _unavailable_identity(
                "identity-command-unavailable",
                f"Kea does not provide {command}.",
            )
        logger.warning("Subnet identity read failed for DHCPv%s", family, exc_info=True)
        return _unavailable_identity("identity-unavailable", "Kea subnet identity facts are unavailable.")
    except (requests.RequestException, ValueError, RuntimeError):
        logger.warning("Subnet identity read failed for DHCPv%s", family, exc_info=True)
        return _unavailable_identity("identity-unavailable", "Kea subnet identity facts are unavailable.")

    if not response or not isinstance(response[0], dict):
        return _unavailable_identity(
            "malformed-identity-response", "Kea returned a malformed subnet identity response."
        )
    if response[0].get("result") == 3:
        return _IdentityObservation(facts=(), diagnostics=(), available=True, complete=True)
    arguments = response[0].get("arguments")
    if not isinstance(arguments, dict) or not isinstance(arguments.get("subnets"), list):
        return _unavailable_identity("malformed-identity-response", "Kea returned malformed subnet identity arguments.")
    return _parse_identity_entries(arguments["subnets"], family)


def _parse_identity_entries(entries: list[Any], family: Family) -> _IdentityObservation:
    diagnostics: list[Diagnostic] = []
    facts: list[_IdentityFact] = []
    for index, entry in enumerate(entries):
        path = f"subnets[{index}]"
        identity = _parse_identity(entry, family, "identity", path, diagnostics)
        if identity is None:
            continue
        shared_network_name, membership_complete = _parse_identity_membership(entry, path, diagnostics)
        facts.append(
            _IdentityFact(
                identity=identity,
                shared_network_name=shared_network_name,
                membership_complete=membership_complete,
            )
        )

    kept, collision_diagnostics, quarantined_ids, quarantined_networks = _quarantine_collisions(
        facts,
        "identity",
    )
    diagnostics.extend(collision_diagnostics)
    return _IdentityObservation(
        facts=tuple(kept),
        diagnostics=tuple(diagnostics),
        available=True,
        complete=not diagnostics,
        quarantined_ids=frozenset(quarantined_ids),
        quarantined_networks=frozenset(quarantined_networks),
    )


def _parse_identity_membership(entry: Any, path: str, diagnostics: list[Diagnostic]) -> tuple[str | None, bool]:
    if not isinstance(entry, dict):
        return None, False
    value = entry.get("shared-network-name")
    if value is None:
        return None, True
    if isinstance(value, str) and value:
        return value, True
    diagnostics.append(
        _diagnostic(
            "invalid-shared-network-membership",
            "Kea returned an invalid Shared Network name.",
            "identity",
            f"{path}.shared-network-name",
        )
    )
    return None, False


def _parse_identity(
    entry: Any,
    family: Family,
    source: str,
    path: str,
    diagnostics: list[Diagnostic],
) -> SubnetIdentity | None:
    if not isinstance(entry, dict):
        diagnostics.append(_diagnostic("invalid-subnet", "Kea returned a non-object Subnet.", source, path))
        return None
    subnet_id = entry.get("id")
    if isinstance(subnet_id, bool) or not isinstance(subnet_id, int) or not MIN_SUBNET_ID <= subnet_id <= MAX_SUBNET_ID:
        diagnostics.append(_diagnostic("invalid-subnet-id", "Kea returned an invalid subnet ID.", source, f"{path}.id"))
        return None
    try:
        network = _network(entry.get("subnet"), family)
    except (TypeError, ValueError):
        diagnostics.append(
            _diagnostic("invalid-subnet-cidr", "Kea returned an invalid Subnet CIDR.", source, f"{path}.subnet")
        )
        return None
    return SubnetIdentity(subnet_id=subnet_id, network=network)


def _quarantine_collisions(
    facts: list[_CollisionFactT], source: str
) -> tuple[list[_CollisionFactT], list[Diagnostic], set[int], set[IPNetwork]]:
    ids: dict[int, list[_CollisionFactT]] = defaultdict(list)
    networks: dict[IPNetwork, list[_CollisionFactT]] = defaultdict(list)
    for fact in facts:
        ids[fact.identity.subnet_id].append(fact)
        networks[fact.identity.network].append(fact)

    quarantined_ids = {subnet_id for subnet_id, participants in ids.items() if len(participants) > 1}
    quarantined_networks = {network for network, participants in networks.items() if len(participants) > 1}
    kept = [
        fact
        for fact in facts
        if fact.identity.subnet_id not in quarantined_ids and fact.identity.network not in quarantined_networks
    ]
    diagnostics = [
        _diagnostic(
            f"{source}-collision",
            f"Multiple Subnets use subnet ID {subnet_id}; all participants were omitted.",
            source,
        )
        for subnet_id in sorted(quarantined_ids)
    ]
    diagnostics.extend(
        [
            _diagnostic(
                f"{source}-collision",
                f"Multiple Subnets use CIDR {network}; all participants were omitted.",
                source,
            )
            for network in sorted(quarantined_networks, key=_network_sort_key)
        ]
    )
    return kept, diagnostics, quarantined_ids, quarantined_networks


def _read_configuration(client: KeaClient, family: Family) -> _ConfigurationObservation:
    try:
        response = client.command("config-get", service=[f"dhcp{family}"])
    except KeaException as exc:
        logger.warning("Subnet configuration read failed for DHCPv%s", family, exc_info=True)
        return _unavailable_configuration(
            "configuration-unavailable",
            f"Kea Subnet configuration facts are unavailable. {kea_error_hint(exc)}",
        )
    except (requests.RequestException, ValueError, RuntimeError):
        logger.warning("Subnet configuration read failed for DHCPv%s", family, exc_info=True)
        return _unavailable_configuration(
            "configuration-unavailable",
            "Kea Subnet configuration facts are unavailable.",
        )

    if not response or not isinstance(response[0], dict):
        return _unavailable_configuration(
            "malformed-configuration-response",
            "Kea returned a malformed configuration response.",
        )
    arguments = response[0].get("arguments")
    if not isinstance(arguments, dict):
        return _unavailable_configuration(
            "malformed-configuration-response",
            "Kea returned malformed configuration arguments.",
        )
    configuration = arguments.get(f"Dhcp{family}")
    if not isinstance(configuration, dict):
        return _unavailable_configuration(
            "malformed-configuration-response",
            f"Kea did not return a Dhcp{family} configuration object.",
        )
    configuration_hash = arguments.get("hash")
    if not isinstance(configuration_hash, str) or not configuration_hash:
        configuration_hash = None
    return _parse_configuration(configuration, family, configuration_hash)


def _parse_configuration(
    configuration: dict[str, Any],
    family: Family,
    configuration_hash: str | None,
) -> _ConfigurationObservation:
    diagnostics: list[Diagnostic] = []
    facts: list[_ConfiguredFact] = []
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
            True,
            f"{subnet_key}[{index}]",
            diagnostics,
        )
        if fact is not None:
            facts.append(fact)

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
            continue
        for member_index, entry in enumerate(members):
            fact = _parse_configured_fact(
                entry,
                family,
                name,
                valid_name,
                f"{path}.{subnet_key}[{member_index}]",
                diagnostics,
            )
            if fact is not None:
                facts.append(fact)

    kept, collision_diagnostics, quarantined_ids, quarantined_networks = _quarantine_collisions(
        facts,
        "configuration",
    )
    diagnostics.extend(collision_diagnostics)
    return _ConfigurationObservation(
        facts=tuple(kept),
        diagnostics=tuple(diagnostics),
        available=True,
        complete=not diagnostics,
        configuration_hash=configuration_hash,
        quarantined_ids=frozenset(quarantined_ids),
        quarantined_networks=frozenset(quarantined_networks),
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
    membership_complete: bool,
    path: str,
    diagnostics: list[Diagnostic],
) -> _ConfiguredFact | None:
    identity = _parse_identity(entry, family, "configuration", path, diagnostics)
    if identity is None:
        return None
    diagnostic_count = len(diagnostics)
    pools = _parse_pools(entry.get("pools", []), identity.network, path, diagnostics)
    options = _parse_options(entry.get("option-data", []), path, diagnostics)
    settings = _parse_settings(entry, family, path, diagnostics)
    # Nested errors intentionally do not discard the otherwise valid Subnet.
    if len(diagnostics) > diagnostic_count:
        logger.debug("Omitted invalid nested Subnet facts at %s", path)
    return _ConfiguredFact(
        identity=identity,
        configuration=SubnetConfiguration(pools=pools, options=options, settings=settings),
        shared_network_name=shared_network_name,
        membership_complete=membership_complete,
    )


def _parse_pools(
    entries: Any,
    subnet: IPNetwork,
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


def _parse_pool(value: Any, subnet: IPNetwork) -> Pool:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Pool must be a non-empty string.")
    value = value.strip()
    if "/" in value:
        pool_network = ipaddress.ip_network(value, strict=True)
        if pool_network.version != subnet.version or not pool_network.subnet_of(subnet):
            raise ValueError("Pool prefix is outside its Subnet.")
        return Pool(start=pool_network.network_address, end=pool_network.broadcast_address)
    parts = [part.strip() for part in value.split("-")]
    if len(parts) != 2 or not all(parts):
        raise ValueError("Pool range must have two endpoints.")
    start = ipaddress.ip_address(parts[0])
    end = ipaddress.ip_address(parts[1])
    if start.version != subnet.version or end.version != subnet.version:
        raise ValueError("Pool address family does not match its Subnet.")
    if start > end or start not in subnet or end not in subnet:
        raise ValueError("Pool range is outside its Subnet.")
    return Pool(start=start, end=end)


def _parse_options(entries: Any, path: str, diagnostics: list[Diagnostic]) -> tuple[SubnetOption, ...]:
    if not isinstance(entries, list):
        diagnostics.append(
            _diagnostic(
                "invalid-option-collection", "Kea returned a non-list option collection.", "configuration", path
            )
        )
        return ()
    options: list[SubnetOption] = []
    for index, entry in enumerate(entries):
        option_path = f"{path}.option-data[{index}]"
        option = _parse_option(entry)
        if option is None:
            diagnostics.append(
                _diagnostic("invalid-option", "Kea returned an invalid Subnet option.", "configuration", option_path)
            )
            continue
        options.append(option)
    return tuple(options)


def _parse_option(entry: Any) -> SubnetOption | None:
    if not isinstance(entry, dict):
        return None
    code = entry.get("code")
    if code is not None and (isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 65_535):
        return None
    name = entry.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        return None
    if code is None and name is None:
        return None
    space = entry.get("space")
    if space is not None and (not isinstance(space, str) or not space):
        return None
    data = entry.get("data", "")
    if not isinstance(data, str):
        return None
    flags = [entry.get("csv-format"), entry.get("always-send"), entry.get("never-send")]
    if any(flag is not None and not isinstance(flag, bool) for flag in flags):
        return None
    return SubnetOption(
        code=code,
        name=name,
        space=space,
        data=data,
        csv_format=flags[0],
        always_send=flags[1],
        never_send=flags[2],
    )


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
) -> tuple[IPAddress, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or not isinstance(value.get("ip-addresses"), list):
        diagnostics.append(
            _diagnostic("invalid-setting", "Kea returned invalid relay addresses.", "configuration", f"{path}.relay")
        )
        return ()
    addresses: list[IPAddress] = []
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


def _network_sort_key(network: IPNetwork) -> tuple[int, int, int]:
    return network.version, int(network.network_address), network.prefixlen


def _observations_disagree(
    identity: _IdentityObservation,
    configuration: _ConfigurationObservation,
) -> bool:
    if not identity.available or not configuration.available:
        return False
    identity_by_pair = {(fact.identity.subnet_id, fact.identity.network): fact for fact in identity.facts}
    configuration_by_pair = {(fact.identity.subnet_id, fact.identity.network): fact for fact in configuration.facts}
    if identity_by_pair.keys() != configuration_by_pair.keys():
        return True
    return any(
        identity_fact.membership_complete
        and configuration_by_pair[pair].membership_complete
        and identity_fact.shared_network_name != configuration_by_pair[pair].shared_network_name
        for pair, identity_fact in identity_by_pair.items()
    )


def _disagreement_is_only_collisions(
    identity: _IdentityObservation,
    configuration: _ConfigurationObservation,
) -> bool:
    quarantined_ids = identity.quarantined_ids | configuration.quarantined_ids
    quarantined_networks = identity.quarantined_networks | configuration.quarantined_networks
    if not quarantined_ids and not quarantined_networks:
        return False
    identity_by_pair = {
        (fact.identity.subnet_id, fact.identity.network): fact
        for fact in identity.facts
        if not _is_quarantined(fact.identity, quarantined_ids, quarantined_networks)
    }
    configuration_by_pair = {
        (fact.identity.subnet_id, fact.identity.network): fact
        for fact in configuration.facts
        if not _is_quarantined(fact.identity, quarantined_ids, quarantined_networks)
    }
    if identity_by_pair.keys() != configuration_by_pair.keys():
        return False
    return not any(
        identity_fact.membership_complete
        and configuration_by_pair[pair].membership_complete
        and identity_fact.shared_network_name != configuration_by_pair[pair].shared_network_name
        for pair, identity_fact in identity_by_pair.items()
    )


def _observe_once(client: KeaClient, family: Family) -> tuple[_IdentityObservation, _ConfigurationObservation]:
    return _read_identity(client, family), _read_configuration(client, family)


def _observe(server: Server, family: Family) -> tuple[_IdentityObservation, _ConfigurationObservation, str | None]:
    try:
        client = server.get_client(version=family)
    except (KeaException, requests.RequestException, ValueError):
        logger.warning("Could not create a Kea client for the Subnet Catalogue", exc_info=True)
        return (
            _unavailable_identity("identity-unavailable", "Kea subnet identity facts are unavailable."),
            _unavailable_configuration(
                "configuration-unavailable",
                "Kea Subnet configuration facts are unavailable.",
            ),
            None,
        )

    identity, configuration = _observe_once(client, family)
    if not _observations_disagree(identity, configuration):
        return identity, configuration, None
    first_hash = configuration.configuration_hash
    identity, configuration = _observe_once(client, family)
    if not _observations_disagree(identity, configuration):
        return identity, configuration, None
    if first_hash and configuration.configuration_hash and first_hash != configuration.configuration_hash:
        return identity, configuration, "configuration-changed-during-retry"
    if _disagreement_is_only_collisions(identity, configuration):
        return identity, configuration, "catalogue-identity-collision"
    return identity, configuration, "identity-configuration-disagreement"


def _cross_source_conflicts(
    identity: _IdentityObservation,
    configuration: _ConfigurationObservation,
) -> tuple[set[int], set[IPNetwork]]:
    identity_by_id = {fact.identity.subnet_id: fact.identity.network for fact in identity.facts}
    configuration_by_id = {fact.identity.subnet_id: fact.identity.network for fact in configuration.facts}
    identity_by_network = {fact.identity.network: fact.identity.subnet_id for fact in identity.facts}
    configuration_by_network = {fact.identity.network: fact.identity.subnet_id for fact in configuration.facts}

    conflicting_ids = {
        subnet_id
        for subnet_id in identity_by_id.keys() & configuration_by_id.keys()
        if identity_by_id[subnet_id] != configuration_by_id[subnet_id]
    }
    conflicting_networks = {
        network
        for network in identity_by_network.keys() & configuration_by_network.keys()
        if identity_by_network[network] != configuration_by_network[network]
    }
    for subnet_id in conflicting_ids:
        conflicting_networks.add(identity_by_id[subnet_id])
        conflicting_networks.add(configuration_by_id[subnet_id])
    for network in tuple(conflicting_networks):
        if network in identity_by_network:
            conflicting_ids.add(identity_by_network[network])
        if network in configuration_by_network:
            conflicting_ids.add(configuration_by_network[network])
    return conflicting_ids, conflicting_networks


def _reconcile(
    server: Server,
    family: Family,
    identity: _IdentityObservation,
    configuration: _ConfigurationObservation,
    disagreement_code: str | None,
) -> CatalogueSnapshot:
    diagnostics = list(identity.diagnostics) + list(configuration.diagnostics)
    cross_source_ids, cross_source_networks = _cross_source_conflicts(identity, configuration)
    conflicting_ids = set(cross_source_ids)
    conflicting_networks = set(cross_source_networks)
    within_source_collision = bool(
        identity.quarantined_ids
        or configuration.quarantined_ids
        or identity.quarantined_networks
        or configuration.quarantined_networks
    )
    conflicting_ids.update(identity.quarantined_ids)
    conflicting_ids.update(configuration.quarantined_ids)
    conflicting_networks.update(identity.quarantined_networks)
    conflicting_networks.update(configuration.quarantined_networks)

    if disagreement_code is None and (cross_source_ids or cross_source_networks):
        disagreement_code = "identity-configuration-disagreement"
    if disagreement_code is None and within_source_collision:
        disagreement_code = "catalogue-identity-collision"
    if disagreement_code is not None:
        messages = {
            "configuration-changed-during-retry": "Kea configuration changed during the fresh catalogue retry.",
            "catalogue-identity-collision": (
                "Kea returned duplicate subnet IDs or CIDRs within one catalogue source; all participants were omitted."
            ),
            "identity-configuration-disagreement": (
                "Kea subnet identity and configuration facts disagree after a fresh retry."
            ),
        }
        diagnostics.append(
            _diagnostic(
                disagreement_code,
                messages[disagreement_code],
                "reconciliation",
            )
        )
    consistent = disagreement_code is None

    configuration_by_pair = {(fact.identity.subnet_id, fact.identity.network): fact for fact in configuration.facts}
    identity_by_pair = {(fact.identity.subnet_id, fact.identity.network): fact for fact in identity.facts}
    subnets: list[VerifiedSubnet] = []
    configured_subnets: list[ConfiguredSubnet] = []

    for pair, identity_fact in identity_by_pair.items():
        if _is_quarantined(identity_fact.identity, conflicting_ids, conflicting_networks):
            continue
        configured_fact = configuration_by_pair.get(pair)
        if configured_fact is not None:
            if (
                identity_fact.membership_complete
                and configured_fact.membership_complete
                and identity_fact.shared_network_name != configured_fact.shared_network_name
            ):
                continue
            shared_network_name = (
                configured_fact.shared_network_name
                if configured_fact.membership_complete
                else identity_fact.shared_network_name
            )
            subnets.append(
                VerifiedSubnet(
                    identity=identity_fact.identity,
                    configuration=configured_fact.configuration,
                    shared_network=_membership(shared_network_name),
                )
            )
        elif not configuration.available or not configuration.complete or not consistent:
            subnets.append(
                VerifiedSubnet(
                    identity=identity_fact.identity,
                    configuration=None,
                    shared_network=_membership(identity_fact.shared_network_name),
                )
            )

    for pair, configured_fact in configuration_by_pair.items():
        if pair in identity_by_pair or _is_quarantined(configured_fact.identity, conflicting_ids, conflicting_networks):
            continue
        configured_subnets.append(
            ConfiguredSubnet(
                candidate_identity=configured_fact.identity,
                configuration=configured_fact.configuration,
                shared_network=_membership(configured_fact.shared_network_name),
            )
        )

    subnets.sort(key=lambda subnet: _network_sort_key(subnet.network))
    configured_subnets.sort(key=lambda subnet: _network_sort_key(subnet.candidate_identity.network))
    complete = (
        identity.available
        and identity.complete
        and configuration.available
        and configuration.complete
        and consistent
        and len(subnets) == len(identity.facts) == len(configuration.facts)
        and not configured_subnets
    )
    snapshot_class = _snapshot_class(complete, identity, configuration)
    return snapshot_class(
        server_id=server.pk,
        family=family,
        observed_at=timezone.now(),
        subnets=tuple(subnets),
        configured_subnets=tuple(configured_subnets),
        diagnostics=tuple(diagnostics),
        identity_complete=identity.complete,
        configuration_complete=configuration.complete,
        consistent=consistent,
        configuration_hash=configuration.configuration_hash,
    )


def _is_quarantined(identity: SubnetIdentity, ids: set[int], networks: set[IPNetwork]) -> bool:
    return identity.subnet_id in ids or identity.network in networks


def _membership(name: str | None) -> SharedNetworkMembership | None:
    return SharedNetworkMembership(name=name) if name else None


def _snapshot_class(
    complete: bool,
    identity: _IdentityObservation,
    configuration: _ConfigurationObservation,
) -> type[CatalogueSnapshot]:
    if complete:
        return CompleteCatalogueSnapshot
    if identity.available and not configuration.available:
        return IdentityOnlyCatalogueSnapshot
    if configuration.available and not identity.available:
        return ConfigurationOnlyCatalogueSnapshot
    return IncompleteCatalogueSnapshot


def _read_live(server: Server, family: Family) -> CatalogueSnapshot:
    _require_persisted_server(server)
    identity, configuration, disagreement_code = _observe(server, family)
    return _reconcile(server, family, identity, configuration, disagreement_code)


def display(server: Server, family: int) -> CatalogueSnapshot:
    """Return a cached Complete or live Incomplete snapshot for presentation.

    Only Complete snapshots enter the interactive cache. A failed or incomplete
    observation is retried on the next call.
    """
    validated_family = _validate_family(family)
    key = _cache_key(server, validated_family)
    cached = cache.get(key)
    if isinstance(cached, CompleteCatalogueSnapshot):
        return cached
    snapshot = _read_live(server, validated_family)
    if isinstance(snapshot, CompleteCatalogueSnapshot):
        cache.set(key, snapshot, constants.SUBNET_CHOICES_TTL)
    return snapshot


def for_synchronization(server: Server, family: int) -> CompleteCatalogueSnapshot:
    """Return one live Complete snapshot pinned for a synchronization run."""
    validated_family = _validate_family(family)
    snapshot = _read_live(server, validated_family)
    if not isinstance(snapshot, CompleteCatalogueSnapshot):
        raise CatalogueUnavailable("A complete Subnet Catalogue is required for synchronization.")
    return snapshot


def _allocate_subnet_id(used_ids: set[int]) -> int:
    """Return a free Kea subnet ID, preferring one above every ID already in use.

    A reused ID makes the DHCP-plugin importer repoint the Subnet its ``KeaDhcpLink``
    still names at a different network, so IDs above the range in use are handed out
    first. Reuse is the fallback, because one Subnet holding ``MAX_SUBNET_ID`` must not
    report the whole range as exhausted.
    """
    above = max(used_ids, default=0) + 1
    if above <= MAX_SUBNET_ID:
        return above
    free = next((candidate for candidate in range(MIN_SUBNET_ID, MAX_SUBNET_ID + 1) if candidate not in used_ids), None)
    if free is None:
        raise SubnetIdExhausted("The Kea subnet ID range is exhausted.")
    return free


class MutationScope(AbstractContextManager["MutationScope"]):
    """One live Subnet identity scope for exact lookup and creation preparation."""

    def __init__(self, server: Server, family: Family) -> None:
        self.server = server
        self.family = family
        self.snapshot: CatalogueSnapshot | None = None

    def __enter__(self) -> MutationScope:
        invalidate(self.server, self.family)
        self.snapshot = _read_live(self.server, self.family)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        # Identity facts stop being live confirmation here, so drop them: a caller that
        # keeps the scope must not decide a mutation on pre-mutation observations.
        self.snapshot = None
        invalidate(self.server, self.family)

    def find_by_id(self, subnet_id: int) -> VerifiedSubnet | None:
        """Return one exact Verified Subnet, or confirm its absence safely."""
        snapshot = self._require_snapshot()
        subnet = snapshot.find_by_id(subnet_id)
        if subnet is not None:
            return subnet
        self._require_complete_identity("Subnet absence cannot be confirmed from an incomplete identity observation.")
        return None

    def find_by_cidr(self, cidr: str) -> VerifiedSubnet | None:
        """Return one exact Verified Subnet, or confirm its absence safely."""
        snapshot = self._require_snapshot()
        subnet = snapshot.find_by_cidr(cidr)
        if subnet is not None:
            return subnet
        self._require_complete_identity("Subnet absence cannot be confirmed from an incomplete identity observation.")
        return None

    def prepare_creation(self, cidr: str, subnet_id: int | None = None) -> NewSubnetIdentity:
        """Return a live-confirmed identity that is available for creation."""
        snapshot = self._require_snapshot()
        self._require_complete_identity("New Subnet creation requires a complete live identity observation.")
        network = _network(cidr, self.family)
        if any(subnet.network == network for subnet in snapshot.subnets):
            raise SubnetIdentityConflict(f"Subnet {network} already exists.")

        used_ids = {subnet.subnet_id for subnet in snapshot.subnets}
        if subnet_id is None:
            subnet_id = _allocate_subnet_id(used_ids)
        elif isinstance(subnet_id, bool) or not isinstance(subnet_id, int):
            raise ValueError("subnet_id must be an integer.")
        elif not MIN_SUBNET_ID <= subnet_id <= MAX_SUBNET_ID:
            raise ValueError(f"subnet_id must be between {MIN_SUBNET_ID} and {MAX_SUBNET_ID}.")
        if subnet_id in used_ids:
            raise SubnetIdentityConflict(f"Subnet ID {subnet_id} already exists.")
        return NewSubnetIdentity(subnet_id=subnet_id, network=network)

    def _require_snapshot(self) -> CatalogueSnapshot:
        if self.snapshot is None:
            raise RuntimeError("MutationScope must be entered before use.")
        return self.snapshot

    def _require_complete_identity(self, message: str) -> None:
        snapshot = self._require_snapshot()
        if not snapshot.identity_complete or not snapshot.consistent:
            raise CatalogueUnavailable(message)


def mutation(server: Server, family: int) -> MutationScope:
    """Open a live mutation scope and invalidate the interactive cache around it."""
    return MutationScope(server, _validate_family(family))
