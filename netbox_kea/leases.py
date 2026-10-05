# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Typed Lease values, record validation, observation coverage and current-use evaluation.

This module owns the Kea 3.2 lease record shape. KeaClient keeps the transport and each raw
body; callers get the frozen values below and do not read wire fields themselves.
"""

from __future__ import annotations

import copy
import ipaddress
import math
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .constants import (
    BY_CLIENT_ID,
    BY_DUID,
    BY_HOSTNAME,
    BY_HW_ADDRESS,
    BY_IP,
    BY_SUBNET,
    BY_SUBNET_ID,
    INFINITE_LIFETIME,
    LEASE_CLIENT_ID_OCTETS,
    LEASE_DUID_OCTETS,
    LEASE_HW_ADDRESS_OCTETS,
    LEASE_QUERY_STATES,
    LEASE_STATES,
    UINT32_MAX,
    Family,
    IPAddressValue,
    LeaseState,
)
from .identifiers import normalize_hex

# The last second that an aware datetime can hold.
_MAX_TIMESTAMP = int(datetime.max.replace(microsecond=0, tzinfo=timezone.utc).timestamp())
#: The query selector of a read that covers every Lease of one family.
ALL_LEASES = "all"
# The selectors of KeaClient.lease_search for each family, and ALL_LEASES for page traversal.
_QUERY_SELECTORS: dict[int, frozenset[str]] = {
    4: frozenset({ALL_LEASES, BY_IP, BY_HW_ADDRESS, BY_HOSTNAME, BY_CLIENT_ID, BY_SUBNET, BY_SUBNET_ID}),
    6: frozenset({ALL_LEASES, BY_IP, BY_HOSTNAME, BY_DUID, BY_SUBNET, BY_SUBNET_ID}),
}
_SUBNET_SELECTORS = frozenset({BY_SUBNET, BY_SUBNET_ID})

_INACTIVE_STATES: frozenset[str] = frozenset({"declined", "expired-reclaimed", "released"})
AllocationKind = Literal["address", "delegated-prefix"]
_EVERY_KIND: tuple[AllocationKind, ...] = ("address", "delegated-prefix")
# Kea 3.2 supports no other DHCPv6 lease type; IA_TA is refused.
_KEA_TYPES: dict[str, AllocationKind] = {"IA_NA": "address", "IA_PD": "delegated-prefix"}
_PREFIX_TYPE = "IA_PD"
# Kea's DUID::EMPTY(), which a declined DHCPv6 lease carries.
_EMPTY_DUID = "00:00:00"

LeaseDiagnosticCode = Literal[
    "invalid-record",
    "missing-field",
    "invalid-type",
    "out-of-range",
    "invalid-address",
    "wrong-family",
    "unsupported-kind",
    "unknown-state",
    "unsupported-state",
    "invalid-identifier",
    "invalid-prefix",
    "invalid-lifetime",
    "duplicate-lease",
    "target-mismatch",
]
_MESSAGES: dict[LeaseDiagnosticCode, str] = {
    "invalid-record": "Kea returned a lease that is not an object.",
    "missing-field": "A required lease field is missing.",
    "invalid-type": "A lease field has the wrong type.",
    "out-of-range": "A lease field is outside the range that Kea permits.",
    "invalid-address": "The lease address is not valid.",
    "wrong-family": "The lease address belongs to the other address family.",
    "unsupported-kind": "The lease type is not supported.",
    "unknown-state": "The lease state is unknown.",
    "unsupported-state": "Kea does not permit this state for this kind of lease.",
    "invalid-identifier": "A client identifier of the lease is not valid.",
    "invalid-prefix": "The delegated prefix address is not the canonical base of the prefix.",
    "invalid-lifetime": "The lease lifetime ends after the last time that can be represented.",
    "duplicate-lease": "Kea returned more than one lease with this identity.",
    "target-mismatch": "Kea returned a lease other than the requested one.",
}
_CODES: dict[str, LeaseDiagnosticCode] = {code: code for code in _MESSAGES}
_PYDANTIC_CODES: dict[str, LeaseDiagnosticCode] = {
    "missing": "missing-field",
    "int_type": "invalid-type",
    "bool_type": "invalid-type",
    "string_type": "invalid-type",
    "greater_than_equal": "out-of-range",
    "less_than_equal": "out-of-range",
}


def _hex_identifier(octets: tuple[int, int]) -> BeforeValidator:
    def normalize(value: Any) -> Any:
        # A value of another type passes through, so the strict string check reports its type.
        if not isinstance(value, str):
            return value
        try:
            return normalize_hex(value, octets)
        except ValueError:
            raise PydanticCustomError("invalid-identifier", _MESSAGES["invalid-identifier"]) from None

    return BeforeValidator(normalize)


HardwareAddress = Annotated[str, _hex_identifier(LEASE_HW_ADDRESS_OCTETS)]
ClientIdentifier = Annotated[str, _hex_identifier(LEASE_CLIENT_ID_OCTETS)]
Duid = Annotated[str, _hex_identifier(LEASE_DUID_OCTETS)]
Uint32 = Annotated[int, Field(ge=0, le=UINT32_MAX)]
PositiveUint32 = Annotated[int, Field(ge=1, le=UINT32_MAX)]
Timestamp = Annotated[int, Field(ge=1, le=_MAX_TIMESTAMP)]
PrefixLength = Annotated[int, Field(ge=1, le=128)]


def _rule(code: LeaseDiagnosticCode, field: str) -> PydanticCustomError:
    """Return a validation error that becomes one stable Lease diagnostic on *field*."""
    return PydanticCustomError(code, _MESSAGES[code], {"field": field})


class _Value(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class LeaseIdentity(_Value):
    """The allocation kind and canonical address that identify one Lease within a Server and family."""

    family: Family
    kind: AllocationKind
    address: IPAddressValue

    @model_validator(mode="after")
    def _consistent(self) -> LeaseIdentity:
        if self.address.version != self.family:
            raise ValueError("A Lease Identity address must belong to its family.")
        if self.kind == "delegated-prefix" and self.family != 6:
            raise ValueError("Only DHCPv6 delegates prefixes.")
        return self


class DHCPv4Binding(_Value):
    """The DHCPv4 client identifiers of one Lease; ``None`` is an identifier that Kea left empty or omitted."""

    hw_address: HardwareAddress | None
    client_id: ClientIdentifier | None


class DHCPv6Binding(_Value):
    """The DHCPv6 client identity of one Lease; ``None`` is Kea's empty DUID on an inactive Lease."""

    duid: Duid | None
    iaid: Uint32


class _Lease(_Value):
    family: ClassVar[Family]
    kind: ClassVar[AllocationKind]
    subnet_id: PositiveUint32
    # Kea omits a zero pool ID.
    pool_id: PositiveUint32 | None = None
    state: LeaseState
    cltt: Timestamp
    valid_lifetime: Uint32
    hostname: str
    fqdn_forward: bool
    fqdn_reverse: bool

    @property
    def prefix_length(self) -> int | None:
        """Return the delegated prefix length, or ``None`` for an address."""
        return None

    @property
    def identity(self) -> LeaseIdentity:
        """Return the Lease Identity."""
        return LeaseIdentity(family=self.family, kind=self.kind, address=self._allocation_address())

    def _allocation_address(self) -> IPAddressValue:
        raise NotImplementedError

    @property
    def infinite(self) -> bool:
        """Return whether the valid lifetime never ends."""
        return self.valid_lifetime == INFINITE_LIFETIME

    @property
    def last_transaction(self) -> datetime:
        """Return the aware time of the last client transaction."""
        return datetime.fromtimestamp(self.cltt, tz=timezone.utc)

    @property
    def expires_at(self) -> datetime | None:
        """Return the aware end of the valid lifetime, or ``None`` for an infinite lifetime."""
        if self.infinite:
            return None
        return datetime.fromtimestamp(self.cltt + self.valid_lifetime, tz=timezone.utc)

    @model_validator(mode="after")
    def _representable_expiration(self) -> _Lease:
        if not self.infinite and self.cltt + self.valid_lifetime > _MAX_TIMESTAMP:
            raise _rule("invalid-lifetime", "valid_lifetime")
        return self


class _AddressAllocation:
    """The facts that every address Lease derives from its ``address`` field."""

    kind: ClassVar[AllocationKind] = "address"
    if TYPE_CHECKING:
        address: IPAddressValue

    def _allocation_address(self) -> IPAddressValue:
        return self.address


class DHCPv4AddressLease(_AddressAllocation, _Lease):
    """One DHCPv4 address Lease."""

    family: ClassVar[Family] = 4
    variant: Literal["dhcpv4-address"] = "dhcpv4-address"
    address: ipaddress.IPv4Address
    # A declined lease keeps Kea's empty hardware address, and a client ID can replace it.
    hw_address: HardwareAddress | None
    client_id: ClientIdentifier | None = None

    @property
    def binding(self) -> DHCPv4Binding:
        """Return the client identifiers."""
        return DHCPv4Binding(hw_address=self.hw_address, client_id=self.client_id)

    @model_validator(mode="after")
    def _kea_rules(self) -> DHCPv4AddressLease:
        # lease_cmds refuses it: "DHCPv4 leases do not support registered state".
        if self.state == "registered":
            raise _rule("unsupported-state", "state")
        if self.state not in _INACTIVE_STATES and self.hw_address is None and self.client_id is None:
            raise _rule("invalid-identifier", "hw_address")
        return self


class _DHCPv6Lease(_Lease):
    family: ClassVar[Family] = 6
    duid: Duid | None
    iaid: Uint32
    preferred_lifetime: Uint32
    hw_address: HardwareAddress | None = None

    @property
    def binding(self) -> DHCPv6Binding:
        """Return the client identity."""
        return DHCPv6Binding(duid=self.duid, iaid=self.iaid)

    @model_validator(mode="after")
    def _client_identity(self) -> _DHCPv6Lease:
        if self.duid is None and self.state not in _INACTIVE_STATES:
            raise _rule("invalid-identifier", "duid")
        return self


class DHCPv6AddressLease(_AddressAllocation, _DHCPv6Lease):
    """One DHCPv6 address (IA_NA) Lease."""

    variant: Literal["dhcpv6-address"] = "dhcpv6-address"
    address: ipaddress.IPv6Address


class DHCPv6PrefixLease(_DHCPv6Lease):
    """One DHCPv6 delegated-prefix (IA_PD) Lease; Kea may delegate it from outside the Subnet's CIDR."""

    kind: ClassVar[AllocationKind] = "delegated-prefix"
    variant: Literal["dhcpv6-delegated-prefix"] = "dhcpv6-delegated-prefix"
    prefix: ipaddress.IPv6Network

    @property
    def prefix_length(self) -> int:
        """Return the delegated prefix length."""
        return self.prefix.prefixlen

    def _allocation_address(self) -> IPAddressValue:
        return self.prefix.network_address

    @model_validator(mode="after")
    def _prefix_rules(self) -> DHCPv6PrefixLease:
        if self.prefix.prefixlen < 1:
            raise _rule("out-of-range", "prefix")
        # lease_cmds refuses it: "Invalid declined state for PD prefix."
        if self.state == "declined":
            raise _rule("unsupported-state", "state")
        return self


Lease: TypeAlias = Annotated[
    DHCPv4AddressLease | DHCPv6AddressLease | DHCPv6PrefixLease, Field(discriminator="variant")
]
#: The Lease variant classes, for an ``isinstance`` check.
LEASE_VARIANTS = (DHCPv4AddressLease, DHCPv6AddressLease, DHCPv6PrefixLease)


class LeaseDiagnostic(_Value):
    """One safe reason why an observation excluded a Kea lease record; it never holds the rejected value."""

    code: LeaseDiagnosticCode
    field: str
    source_position: str
    # Each allocation kind the excluded record can be, so each affected ownership source knows.
    kinds: tuple[AllocationKind, ...]

    @property
    def message(self) -> str:
        """Return the fixed explanation of the code."""
        return _MESSAGES[self.code]


class LeaseRead(_Value):
    """The valid Leases and diagnostics of one Kea reply, with the raw evidence to continue a page read."""

    family: Family
    records: tuple[Lease, ...]
    diagnostics: tuple[LeaseDiagnostic, ...]
    raw_count: Annotated[int, Field(ge=0)]
    next_cursor: IPAddressValue | None


class LeaseQuery(_Value):
    """The scope that one Lease observation requested from Kea."""

    family: Family
    selector: str
    value: int | str | None = None
    state: LeaseState | None = None

    @model_validator(mode="after")
    def _scope(self) -> LeaseQuery:
        if self.selector not in _QUERY_SELECTORS[self.family]:
            raise ValueError("The Lease query selector is not supported for this family.")
        if self.selector == ALL_LEASES:
            valid = self.value is None
        elif self.selector == BY_SUBNET_ID:
            valid = isinstance(self.value, int) and self.value >= 1
        else:
            valid = isinstance(self.value, str) and bool(self.value)
        if not valid:
            raise ValueError("The Lease query value does not fit its selector.")
        if self.state is not None and (self.selector not in _SUBNET_SELECTORS or self.state not in LEASE_QUERY_STATES):
            raise ValueError("Only a Subnet query can filter by state, and only by a state that Kea can count.")
        return self

    @property
    def covers_family(self) -> bool:
        """Return whether the query asks for every Lease of the family."""
        return self.selector == ALL_LEASES and self.state is None


LeaseCoverage = Literal["page", "exhaustive"]


class LeaseSnapshot(_Value):
    """A time-bounded Lease observation of one Server, family and query scope.

    ``page`` coverage is a validated part of the scope; ``exhaustive`` coverage proved the end of it.
    """

    server_id: Annotated[int, Field(ge=1)]
    family: Family
    query: LeaseQuery
    read_started: AwareDatetime
    read_finished: AwareDatetime
    records: tuple[Lease, ...]
    diagnostics: tuple[LeaseDiagnostic, ...]
    coverage: LeaseCoverage
    next_cursor: IPAddressValue | None
    _identities: frozenset[LeaseIdentity] = PrivateAttr(default=frozenset())

    @model_validator(mode="after")
    def _consistent(self) -> LeaseSnapshot:
        if self.read_finished < self.read_started:
            raise ValueError("A Lease Snapshot read cannot finish before it starts.")
        if self.query.family != self.family:
            raise ValueError("The query must belong to the Snapshot family.")
        if any(record.family != self.family for record in self.records):
            raise ValueError("Every Lease of a Snapshot must belong to its family.")
        self._identities = frozenset(record.identity for record in self.records)
        if len(self._identities) != len(self.records):
            raise ValueError("A Lease Snapshot cannot hold two Leases with one identity.")
        if self.next_cursor is not None and self.next_cursor.version != self.family:
            raise ValueError("The continuation must belong to the Snapshot family.")
        if self.coverage == "exhaustive" and self.next_cursor is not None:
            raise ValueError("An exhaustive Lease Snapshot has no continuation.")
        return self

    @property
    def complete(self) -> bool:
        """Return whether the Snapshot covers its whole scope with no excluded record."""
        return self.coverage == "exhaustive" and not self.diagnostics

    @property
    def evaluated_at(self) -> datetime:
        """Return the one evaluation time of the observation: the end of its read."""
        return self.read_finished

    @property
    def current_records(self) -> tuple[Lease, ...]:
        """Return the Current Leases at the evaluation time."""
        return tuple(record for record in self.records if is_current(record, self.evaluated_at))

    def attests_absence(self, identity: LeaseIdentity) -> bool:
        """Return whether the Snapshot proves that Kea had no Lease with *identity*."""
        return (
            self.complete
            and self.query.covers_family
            and identity.family == self.family
            and identity not in self._identities
        )


class LeaseFound(_Value):
    """An exact lookup observed one valid Lease."""

    outcome: Literal["found"] = "found"
    lease: Lease


class LeaseAbsent(_Value):
    """Kea confirmed that it has no Lease with the requested identity."""

    outcome: Literal["absent"] = "absent"
    identity: LeaseIdentity


class LeaseLookupFailed(_Value):
    """An exact lookup returned a record that is not a valid Lease of the requested identity."""

    outcome: Literal["failed"] = "failed"
    identity: LeaseIdentity
    diagnostics: Annotated[tuple[LeaseDiagnostic, ...], Field(min_length=1)]


ExactLeaseResult: TypeAlias = Annotated[LeaseFound | LeaseAbsent | LeaseLookupFailed, Field(discriminator="outcome")]


class ShownLease(_Value):
    """The Lease facts that a form showed, for comparison with a fresh read before a change."""

    identity: LeaseIdentity
    prefix_length: PrefixLength | None
    subnet_id: PositiveUint32
    binding: DHCPv4Binding | DHCPv6Binding
    hostname: str
    valid_lifetime: Uint32


class LeaseEdit(_Value):
    """The fields that one Lease edit writes: ``None`` writes nothing, and an empty hostname clears it.

    The client identifier is the hardware address for DHCPv4 and the DUID for DHCPv6.
    """

    hostname: str | None = None
    client_identifier: str | None = None
    valid_lifetime: Uint32 | None = None

    @property
    def written(self) -> tuple[str, ...]:
        """Return the names of the written fields."""
        return tuple(
            name for name in ("hostname", "client_identifier", "valid_lifetime") if getattr(self, name) is not None
        )


class DHCPv4LeaseRequest(_Value):
    """A request to create one DHCPv4 address Lease; Kea supplies the observed facts."""

    variant: Literal["dhcpv4-address"] = "dhcpv4-address"
    address: ipaddress.IPv4Address
    # lease4-add requires it.
    hw_address: HardwareAddress
    # None lets Kea select the Subnet and take its valid lifetime.
    subnet_id: PositiveUint32 | None = None
    valid_lifetime: Uint32 | None = None
    client_id: ClientIdentifier | None = None
    hostname: str | None = None


class DHCPv6LeaseRequest(_Value):
    """A request to create one DHCPv6 address Lease; Kea supplies the observed facts."""

    variant: Literal["dhcpv6-address"] = "dhcpv6-address"
    address: ipaddress.IPv6Address
    duid: Duid
    iaid: Uint32
    subnet_id: PositiveUint32 | None = None
    valid_lifetime: Uint32 | None = None
    hostname: str | None = None

    @model_validator(mode="after")
    def _client_identity(self) -> DHCPv6LeaseRequest:
        if self.duid == _EMPTY_DUID:
            raise ValueError("A new Lease needs a client DUID, not Kea's empty DUID.")
        return self


LeaseRequest: TypeAlias = Annotated[DHCPv4LeaseRequest | DHCPv6LeaseRequest, Field(discriminator="variant")]


class MalformedLeaseResponse(RuntimeError):
    """Kea returned a lease reply envelope or page cursor that the read cannot use."""


def is_current(lease: Lease, at: datetime) -> bool:
    """Return whether *lease* is a Current Lease at the aware time *at*.

    Raises:
        ValueError: If *at* is naive.

    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Lease current use needs an aware evaluation time.")
    if lease.state != "assigned" and not (lease.state == "registered" and lease.kind == "address"):
        return False
    # Kea compares whole seconds: a finite lifetime ends when its last second is before now.
    return lease.infinite or lease.cltt + lease.valid_lifetime >= math.floor(at.timestamp())


def shown_lease(lease: Lease) -> ShownLease:
    """Return the facts of *lease* that a form shows and a change compares."""
    return ShownLease(
        identity=lease.identity,
        prefix_length=lease.prefix_length,
        subnet_id=lease.subnet_id,
        binding=lease.binding,
        hostname=lease.hostname,
        valid_lifetime=lease.valid_lifetime,
    )


def _client_identifier(value: str, family: int) -> str:
    """Return a submitted client identifier in Kea's form, or raise ValueError."""
    normalized = normalize_hex(value.strip(), LEASE_HW_ADDRESS_OCTETS if family == 4 else LEASE_DUID_OCTETS)
    if family == 6 and normalized == _EMPTY_DUID:
        raise ValueError("A client DUID cannot be Kea's empty DUID.")
    return normalized


def lease_edit(shown: ShownLease, *, hostname: str, client_identifier: str, valid_lifetime: int | None) -> LeaseEdit:
    """Derive the written fields from what a form showed and what the operator submitted.

    A blank hostname clears it. A blank client identifier or lifetime keeps the current value.
    A value equal to the shown one writes nothing.

    Raises:
        ValueError: If the client identifier is not valid for the family.

    """
    identifier = _client_identifier(client_identifier, shown.identity.family) if client_identifier else None
    shown_identifier = shown.binding.hw_address if isinstance(shown.binding, DHCPv4Binding) else shown.binding.duid
    return LeaseEdit(
        hostname=None if hostname == shown.hostname else hostname,
        client_identifier=None if identifier == shown_identifier else identifier,
        valid_lifetime=None if valid_lifetime == shown.valid_lifetime else valid_lifetime,
    )


def lease_edit_conflicts(shown: ShownLease, fresh: Lease, edit: LeaseEdit) -> tuple[str, ...]:
    """Return the shown facts that a fresh read contradicts; a renewal alone is no conflict."""
    current = shown_lease(fresh)
    compared = ["identity", "prefix_length", "subnet_id", "binding"]
    compared += [name for name in ("hostname", "valid_lifetime") if getattr(edit, name) is not None]
    return tuple(name for name in compared if getattr(current, name) != getattr(shown, name))


def lease_record_data(lease: Lease, *, evaluated_at: datetime) -> dict[str, Any]:
    """Return the documented public projection of one Lease, evaluated at the aware time *evaluated_at*."""
    expires_at = lease.expires_at
    return {
        "family": lease.family,
        "kind": lease.kind,
        "address": str(lease.identity.address),
        "prefix_length": lease.prefix_length,
        "subnet_id": lease.subnet_id,
        "state": lease.state,
        "current": is_current(lease, evaluated_at),
        "binding": lease.binding.model_dump(),
        "hostname": lease.hostname,
        "valid_lifetime": lease.valid_lifetime,
        "last_transaction": lease.last_transaction.isoformat(),
        "expiration": {
            "infinite": expires_at is None,
            "expires_at": None if expires_at is None else expires_at.isoformat(),
        },
    }


# --- the Kea wire shape, for KeaClient only ---

_COMMON_KEYS = {
    "ip-address": "address",
    "subnet-id": "subnet_id",
    "pool-id": "pool_id",
    "state": "state",
    "cltt": "cltt",
    "valid-lft": "valid_lifetime",
    "hostname": "hostname",
    "fqdn-fwd": "fqdn_forward",
    "fqdn-rev": "fqdn_reverse",
    "hw-address": "hw_address",
}
_KEYS: dict[int, dict[str, str]] = {
    4: {**_COMMON_KEYS, "client-id": "client_id"},
    6: {**_COMMON_KEYS, "duid": "duid", "iaid": "iaid", "preferred-lft": "preferred_lifetime"},
}
_FIELD_KEYS: dict[int, dict[str, str]] = {
    family: {**{name: key for key, name in keys.items()}, "prefix": "prefix-len"} for family, keys in _KEYS.items()
}


class _FieldProblem(Exception):
    def __init__(self, code: LeaseDiagnosticCode) -> None:
        super().__init__(code)
        self.code = code


_Problem: TypeAlias = tuple[LeaseDiagnosticCode, str]


def _diagnostics(
    problems: list[_Problem], kinds: tuple[AllocationKind, ...], position: str
) -> tuple[LeaseDiagnostic, ...]:
    return tuple(
        LeaseDiagnostic(code=code, field=field, source_position=position, kinds=kinds)
        for code, field in dict.fromkeys(problems)
    )


class _Rejected(Exception):
    """A Kea lease record that is not a valid Lease, with its safe problems."""

    def __init__(self, problems: list[_Problem], kinds: tuple[AllocationKind, ...]) -> None:
        super().__init__("Kea returned a malformed lease.")
        self.problems = problems
        self.kinds = kinds


def _wire_address(value: Any, family: int) -> IPAddressValue:
    if not isinstance(value, str):
        raise _FieldProblem("invalid-type")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise _FieldProblem("invalid-address") from None
    if address.version != family:
        raise _FieldProblem("wrong-family")
    return address


def _wire_state(value: Any, _family: int) -> LeaseState:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _FieldProblem("invalid-type")
    if not 0 <= value < len(LEASE_STATES):
        raise _FieldProblem("unknown-state")
    return LEASE_STATES[value]


def _unchanged(value: Any, _family: int) -> Any:
    return value


def _wire_hw_address(value: Any, _family: int) -> Any:
    return None if value == "" else value


def _wire_duid(value: Any, _family: int) -> Any:
    # A value that is not a DUID passes through, so the model reports it.
    try:
        empty = isinstance(value, str) and normalize_hex(value, LEASE_DUID_OCTETS) == _EMPTY_DUID
    except ValueError:
        return value
    return None if empty else value


_CONVERTERS: dict[str, Callable[[Any, int], Any]] = {
    "address": _wire_address,
    "state": _wire_state,
    "hw_address": _wire_hw_address,
    "duid": _wire_duid,
}


_LeaseModel: TypeAlias = type[DHCPv4AddressLease] | type[DHCPv6AddressLease] | type[DHCPv6PrefixLease]


def _variant(raw: dict[str, Any], family: Family) -> tuple[_LeaseModel, tuple[AllocationKind, ...]]:
    if family == 4:
        return DHCPv4AddressLease, ("address",)
    if "type" not in raw:
        raise _Rejected([("missing-field", "type")], _EVERY_KIND)
    if not isinstance(raw["type"], str):
        raise _Rejected([("invalid-type", "type")], _EVERY_KIND)
    kind = _KEA_TYPES.get(raw["type"])
    if kind is None:
        raise _Rejected([("unsupported-kind", "type")], _EVERY_KIND)
    return (DHCPv6AddressLease if kind == "address" else DHCPv6PrefixLease), (kind,)


def _wire_prefix(raw: dict[str, Any], address: Any) -> ipaddress.IPv6Network:
    """Return the canonical delegated prefix from the address and ``prefix-len``, or raise a keyed problem."""
    if "prefix-len" not in raw:
        raise _Rejected([("missing-field", "prefix-len")], ("delegated-prefix",))
    length = raw["prefix-len"]
    if isinstance(length, bool) or not isinstance(length, int):
        raise _Rejected([("invalid-type", "prefix-len")], ("delegated-prefix",))
    if not 1 <= length <= 128:
        raise _Rejected([("out-of-range", "prefix-len")], ("delegated-prefix",))
    try:
        return ipaddress.IPv6Network((address, length))
    except ValueError:
        raise _Rejected([("invalid-prefix", "ip-address")], ("delegated-prefix",)) from None


def _lease_from_record(raw: Any, family: Family) -> Lease:
    """Validate one Kea lease record, or raise _Rejected with every problem found."""
    if not isinstance(raw, dict):
        raise _Rejected([("invalid-record", "")], ("address",) if family == 4 else _EVERY_KIND)
    model, kinds = _variant(raw, family)
    problems: list[_Problem] = []
    diagnosed: set[str] = set()
    values: dict[str, Any] = {}
    for key, name in _KEYS[family].items():
        if key not in raw:
            continue
        try:
            values[name] = _CONVERTERS.get(name, _unchanged)(raw[key], family)
        except _FieldProblem as problem:
            problems.append((problem.code, key))
            diagnosed.add(name)
    if model is DHCPv6PrefixLease:
        address = values.pop("address", None)
        diagnosed.add("prefix")
        if address is not None:
            try:
                values["prefix"] = _wire_prefix(raw, address)
                diagnosed.discard("prefix")
            except _Rejected as rejected:
                problems.extend(rejected.problems)
        elif "ip-address" not in raw:
            problems.append(("missing-field", "ip-address"))
    try:
        lease = model.model_validate(values)
    except ValidationError as exc:
        raise _Rejected([*problems, *_model_problems(exc, family, diagnosed)], kinds) from None
    if problems:
        raise _Rejected(problems, kinds)
    return lease


def _model_problems(exc: ValidationError, family: int, diagnosed: set[str]) -> list[_Problem]:
    problems: list[_Problem] = []
    for error in exc.errors():
        name = str(error["loc"][0]) if error["loc"] else str(error.get("ctx", {}).get("field", ""))
        if name in diagnosed:
            continue
        code = _CODES.get(error["type"]) or _PYDANTIC_CODES.get(error["type"], "invalid-record")
        problems.append((code, _FIELD_KEYS[family].get(name, "")))
    return problems


def _record_at(raw: Any, family: Family, index: int) -> tuple[Lease | None, tuple[LeaseDiagnostic, ...]]:
    try:
        return _lease_from_record(raw, family), ()
    except _Rejected as rejected:
        return None, _diagnostics(rejected.problems, rejected.kinds, f"leases[{index}]")


def _read_records(raw_leases: list[Any], family: Family) -> tuple[tuple[Lease, ...], tuple[LeaseDiagnostic, ...]]:
    """Validate each record on its own; a dropped record leaves a diagnostic, never an empty result."""
    parsed: list[tuple[int, Lease]] = []
    diagnostics: list[tuple[int, LeaseDiagnostic]] = []
    for index, raw in enumerate(raw_leases):
        lease, rejected = _record_at(raw, family, index)
        if lease is not None:
            parsed.append((index, lease))
        diagnostics.extend((index, diagnostic) for diagnostic in rejected)
    counts = Counter(lease.identity for _index, lease in parsed)
    for index, lease in parsed:
        if counts[lease.identity] > 1:
            duplicate = _diagnostics([("duplicate-lease", "ip-address")], (lease.kind,), f"leases[{index}]")
            diagnostics.extend((index, diagnostic) for diagnostic in duplicate)
    records = tuple(lease for _index, lease in parsed if counts[lease.identity] == 1)
    return records, tuple(diagnostic for _index, diagnostic in sorted(diagnostics, key=lambda item: item[0]))


def _reply(response: Any) -> tuple[int, Any]:
    """Return the result code and arguments of a one-service lease reply, or fail the read."""
    if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
        raise MalformedLeaseResponse("Kea returned a malformed lease response.")
    result = response[0].get("result")
    if isinstance(result, bool) or not isinstance(result, int) or result not in (0, 3):
        raise MalformedLeaseResponse("Kea returned a lease response with an unexpected result.")
    return result, response[0].get("arguments")


def _leases_argument(arguments: Any) -> list[Any]:
    leases = arguments.get("leases") if isinstance(arguments, dict) else None
    if not isinstance(leases, list):
        raise MalformedLeaseResponse("Kea returned a malformed leases collection.")
    return leases


def read_lease_collection(response: Any, *, family: Family) -> LeaseRead:
    """Read a complete lease collection reply, such as ``lease{4,6}-get-all``.

    Raises:
        MalformedLeaseResponse: If the envelope is unusable.

    """
    result, arguments = _reply(response)
    raw_leases = [] if result == 3 and arguments is None else _leases_argument(arguments)
    if result == 3 and raw_leases:
        raise MalformedLeaseResponse("Kea reported no leases and returned some.")
    records, diagnostics = _read_records(raw_leases, family)
    return LeaseRead(
        family=family, records=records, diagnostics=diagnostics, raw_count=len(raw_leases), next_cursor=None
    )


def read_lease_page(response: Any, *, family: Family, limit: int, after: IPAddressValue | None) -> LeaseRead:
    """Read one ``lease{4,6}-get-page`` reply that continued after *after*, or started when it is ``None``.

    The continuation comes from the last raw record, so a page whose every record is
    excluded still continues.

    Raises:
        ValueError: If *limit* or *after* is not valid for the request.
        MalformedLeaseResponse: If the envelope, count or continuation is unusable, or a record is not
            after the cursor and the record before it.

    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("The lease page limit must be a positive integer.")
    if after is not None and after.version != family:
        raise ValueError("The lease page cursor must belong to the requested family.")
    result, arguments = _reply(response)
    raw_leases = [] if result == 3 and arguments is None else _leases_argument(arguments)
    count = len(raw_leases) if arguments is None else arguments.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(raw_leases) or count > limit:
        raise MalformedLeaseResponse("Kea returned an invalid lease page count.")
    if result == 3 and count:
        raise MalformedLeaseResponse("Kea reported no leases and returned some.")
    addresses = [_raw_address(raw, family) for raw in raw_leases]
    previous = after
    # Each page continues strictly after the last one, so no record repeats across pages.
    for address in addresses:
        if address is None:
            continue
        if previous is not None and int(address) <= int(previous):
            raise MalformedLeaseResponse("The lease page is not in ascending order after its cursor.")
        previous = address
    next_cursor = addresses[-1] if count == limit else None
    if count == limit and next_cursor is None:
        raise MalformedLeaseResponse("Kea returned a lease page without a usable continuation.")
    records, diagnostics = _read_records(raw_leases, family)
    return LeaseRead(family=family, records=records, diagnostics=diagnostics, raw_count=count, next_cursor=next_cursor)


def _raw_address(raw: Any, family: int) -> IPAddressValue | None:
    """Return the address of one raw page record, or ``None`` when its own diagnostic covers it."""
    try:
        return _wire_address(raw.get("ip-address") if isinstance(raw, dict) else None, family)
    except _FieldProblem:
        return None


def _lookup_arguments(identity: LeaseIdentity) -> dict[str, Any]:
    """Return the ``lease{4,6}-get`` arguments for *identity*."""
    arguments: dict[str, Any] = {"ip-address": str(identity.address)}
    # Without the type, Kea 3.2 reports a delegated prefix as not found.
    if identity.kind == "delegated-prefix":
        arguments["type"] = _PREFIX_TYPE
    return arguments


def read_exact_lease(response: Any, identity: LeaseIdentity) -> ExactLeaseResult:
    """Read one ``lease{4,6}-get`` reply sent with ``_lookup_arguments(identity)``.

    A malformed record or another target is a failed observation, never a confirmed absence.

    Raises:
        MalformedLeaseResponse: If the envelope is unusable, or a not-found reply carries arguments.

    """
    result, arguments = _reply(response)
    if result == 3 and arguments is not None:
        raise MalformedLeaseResponse("Kea reported no lease and returned one.")
    if result == 3:
        return LeaseAbsent(identity=identity)
    if not isinstance(arguments, dict):
        raise MalformedLeaseResponse("Kea returned a lease without a record.")
    try:
        lease = _lease_from_record(arguments, identity.family)
    except _Rejected as rejected:
        return LeaseLookupFailed(
            identity=identity, diagnostics=_diagnostics(rejected.problems, rejected.kinds, "arguments")
        )
    if lease.identity != identity:
        field = "type" if lease.kind != identity.kind else "ip-address"
        mismatch = _diagnostics([("target-mismatch", field)], (lease.kind,), "arguments")
        return LeaseLookupFailed(identity=identity, diagnostics=mismatch)
    return LeaseFound(lease=lease)


def _edited_arguments(raw: Mapping[str, Any], fresh: Lease, edit: LeaseEdit) -> dict[str, Any]:
    """Return a copy of the fresh Kea body with only the written fields changed.

    An unwritten lifetime keeps the fresh transaction time; a written one starts at the update.
    Nested extension values, including changes by another writer, stay as the fresh read returned them.

    Raises:
        ValueError: If *raw* is not the body that *fresh* came from, or a written value is not valid.

    """
    try:
        parsed = _lease_from_record(dict(raw), fresh.family)
    except _Rejected as rejected:
        raise ValueError("The fresh lease body is not a valid Lease.") from rejected
    if parsed != fresh:
        raise ValueError("The lease body is not the fresh Lease.")
    body = copy.deepcopy(dict(raw))
    if edit.hostname is not None:
        body["hostname"] = edit.hostname
        if not edit.hostname:
            # Kea refuses an FQDN update flag without a hostname.
            body["fqdn-fwd"] = body["fqdn-rev"] = False
    if edit.client_identifier is not None:
        body["hw-address" if fresh.family == 4 else "duid"] = _client_identifier(edit.client_identifier, fresh.family)
    if edit.valid_lifetime is None:
        # Without an expiration time, Kea sets the transaction time of the update to now.
        body["expire"] = fresh.cltt + fresh.valid_lifetime
    else:
        # A written lifetime counts from the update, so the body sends no expiration time.
        body["valid-lft"] = edit.valid_lifetime
        body.pop("expire", None)
    return body


def _creation_arguments(request: LeaseRequest) -> dict[str, Any]:
    """Return the ``lease{4,6}-add`` arguments; an omitted fact stays Kea's to supply."""
    body: dict[str, Any] = {"ip-address": str(request.address)}
    if isinstance(request, DHCPv4LeaseRequest):
        body["hw-address"] = request.hw_address
        optional: dict[str, Any] = {"client-id": request.client_id}
    else:
        body.update({"duid": request.duid, "iaid": request.iaid})
        optional = {}
    optional.update({"subnet-id": request.subnet_id, "valid-lft": request.valid_lifetime, "hostname": request.hostname})
    body.update({key: value for key, value in optional.items() if value is not None})
    return body
