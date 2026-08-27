from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, cast

from .dhcp_options import DHCPOption, parse_dhcp_options

if TYPE_CHECKING:
    from .subnet_catalogue import CatalogueSnapshot, SubnetIdentity

Family = Literal[4, 6]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IdentifierType = Literal["hw-address", "duid", "circuit-id", "client-id", "flex-id"]
ReservationQueryMode = Literal["page", "identity", "address", "hostname"]

_IDENTIFIERS: dict[Family, tuple[IdentifierType, ...]] = {
    4: ("hw-address", "duid", "circuit-id", "client-id", "flex-id"),
    6: ("duid", "hw-address", "flex-id"),
}
_IDENTIFIER_LABELS: dict[IdentifierType, str] = {
    "hw-address": "Hardware Address",
    "duid": "DUID",
    "client-id": "Client ID",
    "circuit-id": "Circuit ID",
    "flex-id": "Flex ID",
}
_HEX_IDENTIFIER_OCTETS: dict[IdentifierType, tuple[int, int]] = {
    "hw-address": (6, 6),
    "duid": (1, 128),
    "client-id": (2, 128),
}
_HEX_IDENTIFIERS = frozenset(_HEX_IDENTIFIER_OCTETS)
_MAX_OPAQUE_IDENTIFIER_LENGTH = 255
_QUERY_PARAMETERS_BY_MODE: dict[ReservationQueryMode, frozenset[str]] = {
    "page": frozenset({"page", "limit", "cursor"}),
    "identity": frozenset({"scope", "subnet_id", "identifier_type", "identifier"}),
    "address": frozenset({"subnet_id", "ip_address"}),
    "hostname": frozenset({"hostname"}),
}
_QUERY_SELECTORS_BY_MODE: dict[ReservationQueryMode, frozenset[str]] = {
    "page": frozenset({"page", "limit", "cursor"}),
    "identity": frozenset({"scope", "identifier_type", "identifier"}),
    "address": frozenset({"ip_address"}),
    "hostname": frozenset({"hostname"}),
}
_QUERY_PARAMETERS = frozenset().union(*_QUERY_PARAMETERS_BY_MODE.values())


def _max_identifier_length(identifier_type: IdentifierType) -> int:
    """Return the longest normalized value one identifier type can hold."""
    octets = _HEX_IDENTIFIER_OCTETS.get(identifier_type)
    # Normalized hex is colon-separated octet pairs: three characters per octet, less one.
    return octets[1] * 3 - 1 if octets else _MAX_OPAQUE_IDENTIFIER_LENGTH


#: Longest ``identifier-type:value`` string any Reservation Identity can produce.
MAX_IDENTITY_LENGTH = max(len(name) + 1 + _max_identifier_length(name) for name in _IDENTIFIER_LABELS)


def reservation_query_mode(parameter_names: Collection[str]) -> ReservationQueryMode:
    """Select one REST Reservation query mode from its supplied parameters."""
    provided = set(parameter_names) & _QUERY_PARAMETERS
    selected = tuple(mode for mode, selectors in _QUERY_SELECTORS_BY_MODE.items() if not provided.isdisjoint(selectors))
    if len(selected) != 1:
        raise ValueError("Select exactly one Reservation query.")
    mode = selected[0]
    if not provided <= _QUERY_PARAMETERS_BY_MODE[mode]:
        raise ValueError("Select exactly one Reservation query.")
    return mode


def reservation_identifier_types(family: int) -> tuple[IdentifierType, ...]:
    """Return the ordered native Reservation Identity types for one family."""
    if family not in (4, 6):
        raise ValueError(f"family must be 4 or 6, got {family!r}")
    return _IDENTIFIERS[cast(Family, family)]


def reservation_identifier_choices(family: int) -> tuple[tuple[IdentifierType, str], ...]:
    """Return the native Reservation Identity types and their UI labels."""
    return tuple(
        (identifier_type, _IDENTIFIER_LABELS[identifier_type])
        for identifier_type in reservation_identifier_types(family)
    )


class MalformedReservation(ValueError):
    """A raw reservation cannot become one unambiguous domain record."""

    def __init__(self, code: str, message: str, field: str = "") -> None:
        self.code = code
        self.field = field
        super().__init__(message)


@dataclass(frozen=True)
class ReservationIdentity:
    """Exactly one normalized native Kea Reservation identifier."""

    identifier_type: IdentifierType
    value: str

    def __post_init__(self) -> None:
        if self.identifier_type not in _IDENTIFIER_LABELS:
            raise ValueError("Unsupported Reservation identifier type.")
        if self.identifier_type in _HEX_IDENTIFIERS:
            try:
                normalized = _normalize_hex(self.value, self.identifier_type)
            except MalformedReservation as exc:
                raise ValueError(str(exc)) from exc
        elif not isinstance(self.value, str) or not self.value:
            raise ValueError("Reservation identifier value must be a non-empty string.")
        elif len(self.value) > _MAX_OPAQUE_IDENTIFIER_LENGTH:
            raise ValueError(
                f"Reservation identifier value must not exceed {_MAX_OPAQUE_IDENTIFIER_LENGTH} characters."
            )
        else:
            normalized = self.value
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True)
class GlobalReservationScope:
    """The Kea Global Reservation Scope."""

    kind: Literal["global"] = "global"


@dataclass(frozen=True)
class InSubnetReservationScope:
    """An In-Subnet Reservation Scope with verified Subnet Identity."""

    subnet: SubnetIdentity
    kind: Literal["in-subnet"] = "in-subnet"


ReservationScope = GlobalReservationScope | InSubnetReservationScope


@dataclass(frozen=True)
class IPv4Reservation:
    """One immutable DHCPv4 Reservation."""

    scope: ReservationScope
    identity: ReservationIdentity
    addresses: tuple[ipaddress.IPv4Address, ...]
    delegated_prefixes: tuple[()] = ()
    hostname: str = ""
    options: tuple[DHCPOption, ...] = ()
    family: Literal[4] = 4

    def __post_init__(self) -> None:
        _validate_reservation_value(self)


@dataclass(frozen=True)
class IPv6Reservation:
    """One immutable DHCPv6 Reservation."""

    scope: ReservationScope
    identity: ReservationIdentity
    addresses: tuple[ipaddress.IPv6Address, ...]
    delegated_prefixes: tuple[ipaddress.IPv6Network, ...]
    hostname: str = ""
    options: tuple[DHCPOption, ...] = ()
    family: Literal[6] = 6

    def __post_init__(self) -> None:
        _validate_reservation_value(self)


Reservation = IPv4Reservation | IPv6Reservation


def reservation_record_data(reservation: Reservation, *, include_subnet_id: bool = True) -> dict[str, Any]:
    """Return the normalized public data for one Reservation."""
    if isinstance(reservation.scope, InSubnetReservationScope):
        subnet: dict[str, Any] = {"cidr": reservation.scope.subnet.cidr}
        if include_subnet_id:
            subnet = {"id": reservation.scope.subnet.subnet_id, **subnet}
        scope: dict[str, Any] = {"type": "in-subnet", "subnet": subnet}
    else:
        scope = {"type": "global"}
    return {
        "family": reservation.family,
        "scope": scope,
        "identity": {
            "type": reservation.identity.identifier_type,
            "value": reservation.identity.value,
        },
        "addresses": [str(address) for address in reservation.addresses],
        "delegated_prefixes": [str(prefix) for prefix in reservation.delegated_prefixes],
        "hostname": reservation.hostname,
        "options": [
            {
                "code": option.code,
                "name": option.name,
                "space": option.space,
                "data": option.data,
                "csv_format": option.csv_format,
                "always_send": option.always_send,
                "never_send": option.never_send,
            }
            for option in reservation.options
        ],
    }


T = TypeVar("T")


@dataclass(frozen=True)
class Unchanged:
    """Leave one managed Reservation fact unchanged."""


@dataclass(frozen=True)
class SetValue(Generic[T]):
    """Set one managed Reservation fact to an explicit value."""

    value: T


@dataclass(frozen=True)
class ClearValue:
    """Remove one optional managed Reservation fact."""


FieldChange = Unchanged | SetValue[T] | ClearValue
UNCHANGED = Unchanged()
ReservationPersistence = Literal["persisted", "failed", "not-requested"]


@dataclass(frozen=True)
class ReservationChange:
    """Explicit changes to the mutable facts of one Reservation."""

    addresses: FieldChange[tuple[IPAddress, ...]] = UNCHANGED
    delegated_prefixes: FieldChange[tuple[ipaddress.IPv6Network, ...]] = UNCHANGED
    hostname: FieldChange[str] = UNCHANGED
    options: FieldChange[tuple[DHCPOption, ...]] = UNCHANGED


@dataclass(frozen=True)
class ReservationMutationResult:
    """The separate application, persistence, and verification outcomes."""

    previous: Reservation | None
    intended: Reservation | None
    application: Literal["applied"]
    persistence: ReservationPersistence
    verification: Literal["verified", "failed"]


@dataclass(frozen=True)
class ReservationCapabilities:
    """Live identifier and mutation capabilities for one DHCP family."""

    family: Family
    identifiers: tuple[IdentifierType, ...]
    mutation_available: bool
    explanation: str
    unavailable_identifiers: tuple[tuple[IdentifierType, str], ...] = ()


class ReservationConflict(RuntimeError):
    """The live managed facts changed after the operator opened the edit form."""


@dataclass(frozen=True)
class ReservationDiagnostic:
    """One sanitized explanation for a quarantined Reservation."""

    code: str
    message: str
    source_position: str


@dataclass(frozen=True)
class ReservationSnapshot:
    """One bounded observation that preserves valid Reservations and diagnostics."""

    family: Family
    records: tuple[Reservation, ...]
    diagnostics: tuple[ReservationDiagnostic, ...]
    complete: bool
    next_cursor: str | None


#: Display label to stable machine key. Templates and Python branch on the key, so
#: renaming a label can never silently change which branch a state matches.
SynchronizationLabel = Literal[
    "Not Applicable",
    "Not Synchronized",
    "Partially Synchronized",
    "Synchronized",
    "Unknown",
]

_SYNCHRONIZATION_STATE_CODES: dict[SynchronizationLabel, str] = {
    "Not Applicable": "not-applicable",
    "Not Synchronized": "not-synchronized",
    "Partially Synchronized": "partially-synchronized",
    "Synchronized": "synchronized",
    "Unknown": "unknown",
}


@dataclass(frozen=True)
class ReservationSynchronizationState:
    """One aggregate synchronization state for all allocation addresses."""

    label: SynchronizationLabel
    synchronized: int
    total: int
    reason: str = ""

    @property
    def code(self) -> str:
        """Return the stable machine key to compare against, never the display text."""
        return _SYNCHRONIZATION_STATE_CODES[self.label]

    @classmethod
    def from_counts(cls, synchronized: int, total: int) -> ReservationSynchronizationState:
        """Build the exact aggregate state for known address counts."""
        if total < 1 or not 0 <= synchronized <= total:
            raise ValueError("Synchronization counts are invalid.")
        label: SynchronizationLabel
        if synchronized == 0:
            label = "Not Synchronized"
        elif synchronized == total:
            label = "Synchronized"
        else:
            label = "Partially Synchronized"
        return cls(label=label, synchronized=synchronized, total=total)

    @classmethod
    def not_applicable(cls, reason: str) -> ReservationSynchronizationState:
        """Build a Not Applicable state with a concise reason."""
        return cls(label="Not Applicable", synchronized=0, total=0, reason=reason)

    @classmethod
    def unknown(cls, total: int, reason: str) -> ReservationSynchronizationState:
        """Build an Unknown state when the target could not be observed."""
        return cls(label="Unknown", synchronized=0, total=total, reason=reason)


def _validate_reservation_value(reservation: Reservation) -> None:
    if reservation.identity.identifier_type not in reservation_identifier_types(reservation.family):
        raise ValueError(f"{reservation.identity.identifier_type} is not valid for DHCPv{reservation.family}.")
    if (
        isinstance(reservation.scope, InSubnetReservationScope)
        and reservation.scope.subnet.network.version != reservation.family
    ):
        raise ValueError("The Reservation Scope must use the same address family as the Reservation.")
    if not isinstance(reservation.hostname, str):
        raise ValueError("Reservation hostname must be a string.")
    if len(set(reservation.addresses)) != len(reservation.addresses):
        raise ValueError("Reservation addresses must be unique.")
    if any(address.version != reservation.family for address in reservation.addresses):
        raise ValueError("Reservation addresses must use the Reservation family.")
    if isinstance(reservation.scope, InSubnetReservationScope) and any(
        address not in reservation.scope.subnet.network for address in reservation.addresses
    ):
        raise ValueError("Reservation addresses must belong to the In-Subnet Scope.")
    if reservation.family == 4:
        if len(reservation.addresses) > 1:
            raise ValueError("A DHCPv4 Reservation permits at most one address.")
        if reservation.delegated_prefixes:
            raise ValueError("A DHCPv4 Reservation does not support delegated prefixes.")
    else:
        if len(set(reservation.delegated_prefixes)) != len(reservation.delegated_prefixes):
            raise ValueError("Reservation delegated prefixes must be unique.")
        if any(prefix.version != 6 for prefix in reservation.delegated_prefixes):
            raise ValueError("A DHCPv6 Reservation requires IPv6 delegated prefixes.")
    if any(not isinstance(option, DHCPOption) for option in reservation.options):
        raise ValueError("Reservation options must be DHCPOption values.")


def _option_data(option: DHCPOption) -> dict[str, Any]:
    data: dict[str, Any] = {"data": option.data}
    if option.code is not None:
        data["code"] = option.code
    if option.name is not None:
        data["name"] = option.name
    if option.space is not None:
        data["space"] = option.space
    if option.csv_format is not None:
        data["csv-format"] = option.csv_format
    if option.always_send is not None:
        data["always-send"] = option.always_send
    if option.never_send is not None:
        data["never-send"] = option.never_send
    return data


def _reservation_to_raw(reservation: Reservation) -> dict[str, Any]:
    """Serialize managed Reservation facts for the private Kea adapter."""
    subnet_id = 0 if isinstance(reservation.scope, GlobalReservationScope) else reservation.scope.subnet.subnet_id
    raw: dict[str, Any] = {
        "subnet-id": subnet_id,
        reservation.identity.identifier_type: reservation.identity.value,
    }
    if reservation.family == 4 and reservation.addresses:
        raw["ip-address"] = str(reservation.addresses[0])
    if reservation.family == 6 and reservation.addresses:
        raw["ip-addresses"] = [str(address) for address in reservation.addresses]
    if reservation.delegated_prefixes:
        raw["prefixes"] = [str(prefix) for prefix in reservation.delegated_prefixes]
    if reservation.hostname:
        raw["hostname"] = reservation.hostname
    if reservation.options:
        raw["option-data"] = [_option_data(option) for option in reservation.options]
    return raw


def _option_matches_intent(observed: DHCPOption, intended: DHCPOption) -> bool:
    """Say whether one observed Option carries every Option fact the caller submitted."""
    if observed.data != intended.data:
        return False
    return all(
        getattr(intended, field) is None or getattr(observed, field) == getattr(intended, field)
        for field in ("code", "name", "space", "csv_format", "always_send", "never_send")
    )


def reservation_matches_intent(observed: Reservation, intended: Reservation) -> bool:
    """Say whether one refetched Reservation carries every fact the caller submitted.

    Kea resolves an Option submitted by name alone against its own definitions and
    returns the code, space and CSV format with it, so an unspecified Option fact is
    Kea's to fill and is not compared.
    """
    if replace(observed, options=()) != replace(intended, options=()):
        return False
    if len(observed.options) != len(intended.options):
        return False
    return all(
        _option_matches_intent(observed_option, intended_option)
        for observed_option, intended_option in zip(observed.options, intended.options, strict=True)
    )


def reservation_fingerprint(reservation: Reservation) -> str:
    """Return a stable fingerprint of managed facts only."""
    encoded = json.dumps(_reservation_to_raw(reservation), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _change_value(change: FieldChange[T], current: T, empty: T) -> T:
    if isinstance(change, Unchanged):
        return current
    if isinstance(change, ClearValue):
        return empty
    return change.value


def apply_reservation_change(reservation: Reservation, change: ReservationChange) -> Reservation:
    """Apply explicit mutable-field changes without changing Identity or Scope."""
    addresses = _change_value(change.addresses, reservation.addresses, ())
    prefixes = _change_value(change.delegated_prefixes, reservation.delegated_prefixes, ())
    hostname = _change_value(change.hostname, reservation.hostname, "")
    options = _change_value(change.options, reservation.options, ())
    # One ReservationChange serves both families, so narrow before replace(). Each cast
    # reaches __post_init__, which rejects a value the other family cannot hold.
    if isinstance(reservation, IPv4Reservation):
        return replace(
            reservation,
            addresses=cast("tuple[ipaddress.IPv4Address, ...]", addresses),
            delegated_prefixes=cast("tuple[()]", prefixes),
            hostname=hostname,
            options=options,
        )
    return replace(
        reservation,
        addresses=cast("tuple[ipaddress.IPv6Address, ...]", addresses),
        delegated_prefixes=prefixes,
        hostname=hostname,
        options=options,
    )


def _family(value: int) -> Family:
    if value not in (4, 6):
        raise ValueError(f"family must be 4 or 6, got {value!r}")
    return cast(Family, value)


def _normalize_hex(value: Any, identifier_type: IdentifierType) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedReservation("invalid-identifier", "The Reservation identifier is invalid.", identifier_type)
    compact = re.sub(r"[:.-]", "", value)
    if not compact or len(compact) % 2 or re.fullmatch(r"[0-9A-Fa-f]+", compact) is None:
        raise MalformedReservation("invalid-identifier", "The Reservation identifier is invalid.", identifier_type)
    octets = len(compact) // 2
    minimum, maximum = _HEX_IDENTIFIER_OCTETS[identifier_type]
    if not minimum <= octets <= maximum:
        raise MalformedReservation("invalid-identifier", "The Reservation identifier is invalid.", identifier_type)
    return ":".join(compact[index : index + 2].lower() for index in range(0, len(compact), 2))


def _identity(raw: dict[str, Any], family: Family) -> ReservationIdentity:
    if "remote-id" in raw:
        raise MalformedReservation(
            "unsupported-identifier",
            "Relay remote ID is not a native Reservation Identity. Configure the Kea Flex ID hook instead.",
            "remote-id",
        )
    invalid_family_identifier = next(
        (
            identifier_type
            for identifier_type in _IDENTIFIER_LABELS
            if identifier_type in raw and identifier_type not in reservation_identifier_types(family)
        ),
        None,
    )
    if invalid_family_identifier is not None:
        raise MalformedReservation(
            "invalid-family-identifier",
            f"The Reservation contains an identifier that is not valid for DHCPv{family}.",
            invalid_family_identifier,
        )
    identifiers = [
        (identifier_type, raw[identifier_type])
        for identifier_type in reservation_identifier_types(family)
        if identifier_type in raw
    ]
    if len(identifiers) != 1:
        code = "missing-identifier" if not identifiers else "ambiguous-identifier"
        raise MalformedReservation(code, "A Reservation must contain exactly one supported identifier.")
    identifier_type, value = identifiers[0]
    if identifier_type in _HEX_IDENTIFIERS:
        normalized = _normalize_hex(value, identifier_type)
    elif not isinstance(value, str) or not value:
        raise MalformedReservation("invalid-identifier", "The Reservation identifier is invalid.", identifier_type)
    else:
        normalized = value
    try:
        return ReservationIdentity(identifier_type=identifier_type, value=normalized)
    except ValueError as exc:
        raise MalformedReservation(
            "invalid-identifier",
            "The Reservation identifier is invalid.",
            identifier_type,
        ) from exc


def _scope(raw: dict[str, Any], catalogue: CatalogueSnapshot) -> ReservationScope:
    subnet_id = raw.get("subnet-id")
    if isinstance(subnet_id, bool) or not isinstance(subnet_id, int) or subnet_id < 0:
        raise MalformedReservation("invalid-scope", "The Reservation has an invalid scope.", "subnet-id")
    if subnet_id == 0:
        return GlobalReservationScope()
    subnet = catalogue.find_by_id(subnet_id)
    if subnet is None:
        raise MalformedReservation(
            "unverified-scope",
            "The Reservation refers to a Subnet that the Subnet Catalogue cannot verify.",
            "subnet-id",
        )
    return InSubnetReservationScope(subnet=subnet.identity)


def _addresses(raw: dict[str, Any], family: Family) -> tuple[IPAddress, ...]:
    if family == 4:
        if "ip-addresses" in raw:
            raise MalformedReservation(
                "invalid-addresses", "DHCPv4 does not support an ip-addresses collection.", "ip-addresses"
            )
        values = () if raw.get("ip-address") in (None, "") else (raw.get("ip-address"),)
    else:
        if "ip-address" in raw:
            raise MalformedReservation(
                "invalid-addresses", "DHCPv6 does not support a scalar ip-address.", "ip-address"
            )
        values = raw.get("ip-addresses", [])
        if not isinstance(values, list):
            raise MalformedReservation("invalid-addresses", "Reservation addresses must be a list.", "ip-addresses")
    parsed: list[IPAddress] = []
    for index, value in enumerate(values):
        field = "ip-address" if family == 4 else f"ip-addresses[{index}]"
        if not isinstance(value, str):
            raise MalformedReservation("invalid-address", "The Reservation contains an invalid address.", field)
        try:
            address = ipaddress.ip_address(value)
        except (TypeError, ValueError) as exc:
            raise MalformedReservation(
                "invalid-address", "The Reservation contains an invalid address.", field
            ) from exc
        if address.version != family or address in parsed:
            raise MalformedReservation(
                "invalid-address", "The Reservation contains an invalid or duplicate address.", field
            )
        parsed.append(address)
    return tuple(parsed)


def _prefixes(raw: dict[str, Any], family: Family) -> tuple[ipaddress.IPv6Network, ...]:
    values = raw.get("prefixes", [])
    if family == 4:
        if values not in (None, []):
            raise MalformedReservation("invalid-prefixes", "DHCPv4 does not support delegated prefixes.", "prefixes")
        return ()
    if not isinstance(values, list):
        raise MalformedReservation("invalid-prefixes", "Delegated prefixes must be a list.", "prefixes")
    parsed: list[ipaddress.IPv6Network] = []
    for index, value in enumerate(values):
        field = f"prefixes[{index}]"
        if not isinstance(value, str):
            raise MalformedReservation("invalid-prefix", "The Reservation contains an invalid delegated prefix.", field)
        try:
            prefix = ipaddress.IPv6Network(value, strict=True)
        except (TypeError, ValueError) as exc:
            raise MalformedReservation(
                "invalid-prefix", "The Reservation contains an invalid delegated prefix.", field
            ) from exc
        if prefix in parsed:
            raise MalformedReservation(
                "invalid-prefix", "The Reservation contains a duplicate delegated prefix.", field
            )
        parsed.append(prefix)
    return tuple(parsed)


def _parse_reservation(raw: Any, family: Family, catalogue: CatalogueSnapshot) -> Reservation:
    if not isinstance(raw, dict):
        raise MalformedReservation("invalid-record", "Kea returned a non-object Reservation.")
    identity = _identity(raw, family)
    scope = _scope(raw, catalogue)
    addresses = _addresses(raw, family)
    if isinstance(scope, InSubnetReservationScope):
        for index, address in enumerate(addresses):
            if address not in scope.subnet.network:
                field = "ip-address" if family == 4 else f"ip-addresses[{index}]"
                raise MalformedReservation(
                    "invalid-address",
                    "The Reservation contains an address outside its verified In-Subnet Scope.",
                    field,
                )
    prefixes = _prefixes(raw, family)
    hostname = raw.get("hostname", "")
    if not isinstance(hostname, str):
        raise MalformedReservation("invalid-hostname", "The Reservation hostname is invalid.", "hostname")
    try:
        options = parse_dhcp_options(raw.get("option-data", []))
    except ValueError as exc:
        raise MalformedReservation(
            "invalid-options", "The Reservation contains invalid DHCP Options.", "option-data"
        ) from exc
    if family == 4:
        return IPv4Reservation(
            scope=scope,
            identity=identity,
            addresses=cast(tuple[ipaddress.IPv4Address, ...], addresses),
            hostname=hostname,
            options=options,
        )
    return IPv6Reservation(
        scope=scope,
        identity=identity,
        addresses=cast(tuple[ipaddress.IPv6Address, ...], addresses),
        delegated_prefixes=prefixes,
        hostname=hostname,
        options=options,
    )


def _exact_reservation(raw: Any, family: int, catalogue: CatalogueSnapshot) -> Reservation:
    """Parse one exact Kea target or fail closed."""
    return _parse_reservation(raw, _family(family), catalogue)


def _parse_record_at(
    raw: Any,
    family: Family,
    catalogue: CatalogueSnapshot,
    index: int,
    expected_hostname: str | None = None,
) -> tuple[Reservation | None, ReservationDiagnostic | None]:
    try:
        reservation = _parse_reservation(raw, family, catalogue)
        if expected_hostname is not None and reservation.hostname != expected_hostname:
            raise MalformedReservation(
                "target-mismatch",
                "Kea returned a Reservation that does not match the requested hostname.",
                "hostname",
            )
        return reservation, None
    except MalformedReservation as exc:
        suffix = f".{exc.field}" if exc.field else ""
        return None, ReservationDiagnostic(
            code=exc.code,
            message=str(exc),
            source_position=f"hosts[{index}]{suffix}",
        )


def _parse_reservation_page(
    hosts: Any,
    family: int,
    catalogue: CatalogueSnapshot,
    next_cursor: str | None,
    *,
    expected_hostname: str | None = None,
) -> ReservationSnapshot:
    """Build one typed Snapshot and quarantine malformed records individually."""
    parsed_family = _family(family)
    if not isinstance(hosts, list):
        raise RuntimeError("reservation-get-page returned a malformed hosts collection.")
    records: list[Reservation] = []
    diagnostics: list[ReservationDiagnostic] = []
    for index, raw in enumerate(hosts):
        record, diagnostic = _parse_record_at(raw, parsed_family, catalogue, index, expected_hostname)
        if record is not None:
            records.append(record)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return ReservationSnapshot(
        family=parsed_family,
        records=tuple(records),
        diagnostics=tuple(diagnostics),
        complete=not diagnostics,
        next_cursor=next_cursor,
    )
