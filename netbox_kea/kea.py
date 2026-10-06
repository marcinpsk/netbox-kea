# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2023-2024 Devon Mar <devon-mar@users.noreply.github.com>
# SPDX-FileCopyrightText: 2026 Andrew Backeby <andrew@backeby.eu>
# SPDX-License-Identifier: Apache-2.0
import base64
import ipaddress
import itertools
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, NamedTuple, Protocol, TypedDict, cast

import requests
from requests.models import HTTPBasicAuth

from . import constants
from .constants import Family, IPAddressValue, IPNetworkValue, Persistence
from .decimal_text import parse_decimal
from .dhcp_options import (
    DHCPOption,
    InvalidAddress,
    address_list,
    form_managed_entry,
    form_managed_options,
    form_shows,
    merge_option_form_rows,
    parse_dhcp_options,
)
from .leases import (
    ALL_LEASES,
    ExactLeaseResult,
    LeaseAbsent,
    LeaseCoverage,
    LeaseDiagnostic,
    LeaseFound,
    LeaseIdentity,
    LeaseLookupFailed,
    LeaseQuery,
    LeaseRead,
    LeaseSnapshot,
    allocation_identities,
    lookup_arguments,
    read_exact_lease,
    read_lease_collection,
    read_lease_page,
    read_lease_page_count,
)
from .pools import parse_pool
from .reservations import (
    RESERVATION_PAGE_FETCH_FAILED,
    RESERVATION_PAGE_LIMIT_REACHED,
    RESERVATION_PAGINATION_STALLED,
    GlobalReservationScope,
    IdentifierType,
    InSubnetReservationScope,
    MalformedReservation,
    Reservation,
    ReservationCapabilities,
    ReservationChange,
    ReservationConflict,
    ReservationDiagnostic,
    ReservationIdentity,
    ReservationMutationResult,
    ReservationScope,
    ReservationSnapshot,
    _exact_reservation,
    _option_data,
    _parse_reservation_page,
    _reservation_to_raw,
    apply_reservation_change,
    reservation_fingerprint,
    reservation_identifier_types,
    reservation_matches_intent,
)

logger = logging.getLogger(__name__)

# A write changes live or on-disk Kea state. A read only reads or validates.
CommandKind = Literal["read", "write"]


class KeaCommand(Enum):
    """Each Kea command that the plugin sends: its wire name is the value, and it has a kind."""

    _value_: str
    kind: CommandKind

    def __new__(cls, wire: str, kind: CommandKind) -> "KeaCommand":
        """Store the wire name as the value, and the kind."""
        member = object.__new__(cls)
        member._value_ = wire
        member.kind = kind
        return member

    @property
    def is_write(self) -> bool:
        """Return whether the command changes live or on-disk Kea state."""
        return self.kind == "write"

    CONFIG_GET = "config-get", "read"
    CONFIG_TEST = "config-test", "read"
    LIST_COMMANDS = "list-commands", "read"
    STATUS_GET = "status-get", "read"
    VERSION_GET = "version-get", "read"
    RESERVATION_GET = "reservation-get", "read"
    RESERVATION_GET_BY_HOSTNAME = "reservation-get-by-hostname", "read"
    RESERVATION_GET_PAGE = "reservation-get-page", "read"
    LEASE4_GET = "lease4-get", "read"
    LEASE6_GET = "lease6-get", "read"
    LEASE4_GET_ALL = "lease4-get-all", "read"
    LEASE6_GET_ALL = "lease6-get-all", "read"
    LEASE4_GET_BY_CLIENT_ID = "lease4-get-by-client-id", "read"
    LEASE4_GET_BY_HOSTNAME = "lease4-get-by-hostname", "read"
    LEASE6_GET_BY_HOSTNAME = "lease6-get-by-hostname", "read"
    LEASE4_GET_BY_HW_ADDRESS = "lease4-get-by-hw-address", "read"
    LEASE6_GET_BY_DUID = "lease6-get-by-duid", "read"
    LEASE4_GET_BY_STATE = "lease4-get-by-state", "read"
    LEASE6_GET_BY_STATE = "lease6-get-by-state", "read"
    LEASE4_GET_PAGE = "lease4-get-page", "read"
    LEASE6_GET_PAGE = "lease6-get-page", "read"
    STAT_LEASE4_GET = "stat-lease4-get", "read"
    STAT_LEASE6_GET = "stat-lease6-get", "read"
    SUBNET4_GET = "subnet4-get", "read"
    SUBNET6_GET = "subnet6-get", "read"
    SUBNET4_LIST = "subnet4-list", "read"
    SUBNET6_LIST = "subnet6-list", "read"
    NETWORK4_GET = "network4-get", "read"
    NETWORK6_GET = "network6-get", "read"

    CONFIG_SET = "config-set", "write"
    CONFIG_WRITE = "config-write", "write"
    DHCP_DISABLE = "dhcp-disable", "write"
    DHCP_ENABLE = "dhcp-enable", "write"
    RESERVATION_ADD = "reservation-add", "write"
    RESERVATION_DEL = "reservation-del", "write"
    RESERVATION_UPDATE = "reservation-update", "write"
    LEASE4_ADD = "lease4-add", "write"
    LEASE6_ADD = "lease6-add", "write"
    LEASE4_DEL = "lease4-del", "write"
    LEASE6_DEL = "lease6-del", "write"
    LEASE4_UPDATE = "lease4-update", "write"
    LEASE6_UPDATE = "lease6-update", "write"
    LEASE4_WIPE = "lease4-wipe", "write"
    LEASE6_WIPE = "lease6-wipe", "write"
    SUBNET4_ADD = "subnet4-add", "write"
    SUBNET6_ADD = "subnet6-add", "write"
    SUBNET4_DEL = "subnet4-del", "write"
    SUBNET6_DEL = "subnet6-del", "write"
    SUBNET4_UPDATE = "subnet4-update", "write"
    SUBNET6_UPDATE = "subnet6-update", "write"
    SUBNET4_DELTA_ADD = "subnet4-delta-add", "write"
    SUBNET6_DELTA_ADD = "subnet6-delta-add", "write"
    SUBNET4_DELTA_DEL = "subnet4-delta-del", "write"
    SUBNET6_DELTA_DEL = "subnet6-delta-del", "write"
    NETWORK4_ADD = "network4-add", "write"
    NETWORK6_ADD = "network6-add", "write"
    NETWORK4_DEL = "network4-del", "write"
    NETWORK6_DEL = "network6-del", "write"
    NETWORK4_SUBNET_ADD = "network4-subnet-add", "write"
    NETWORK6_SUBNET_ADD = "network6-subnet-add", "write"
    NETWORK4_SUBNET_DEL = "network4-subnet-del", "write"
    NETWORK6_SUBNET_DEL = "network6-subnet-del", "write"


class WriteGuard(Protocol):
    """Refuses a write command before it is sent. ``branching.BranchBinding`` refuses one in a branch."""

    def refuse(self, operation: str) -> None:
        """Raise when *operation* must not change Kea now."""


# The member of each family-specific command, by family. Families absent from a mapping have no such command.
ByFamily = Mapping[Family, KeaCommand]
LEASE_GET: ByFamily = {4: KeaCommand.LEASE4_GET, 6: KeaCommand.LEASE6_GET}
LEASE_GET_ALL: ByFamily = {4: KeaCommand.LEASE4_GET_ALL, 6: KeaCommand.LEASE6_GET_ALL}
LEASE_GET_BY_CLIENT_ID: ByFamily = {4: KeaCommand.LEASE4_GET_BY_CLIENT_ID}
LEASE_GET_BY_DUID: ByFamily = {6: KeaCommand.LEASE6_GET_BY_DUID}
LEASE_GET_BY_HOSTNAME: ByFamily = {4: KeaCommand.LEASE4_GET_BY_HOSTNAME, 6: KeaCommand.LEASE6_GET_BY_HOSTNAME}
LEASE_GET_BY_HW_ADDRESS: ByFamily = {4: KeaCommand.LEASE4_GET_BY_HW_ADDRESS}
LEASE_GET_BY_STATE: ByFamily = {4: KeaCommand.LEASE4_GET_BY_STATE, 6: KeaCommand.LEASE6_GET_BY_STATE}
LEASE_GET_PAGE: ByFamily = {4: KeaCommand.LEASE4_GET_PAGE, 6: KeaCommand.LEASE6_GET_PAGE}
STAT_LEASE_GET: ByFamily = {4: KeaCommand.STAT_LEASE4_GET, 6: KeaCommand.STAT_LEASE6_GET}
SUBNET_GET: ByFamily = {4: KeaCommand.SUBNET4_GET, 6: KeaCommand.SUBNET6_GET}
SUBNET_LIST: ByFamily = {4: KeaCommand.SUBNET4_LIST, 6: KeaCommand.SUBNET6_LIST}
NETWORK_GET: ByFamily = {4: KeaCommand.NETWORK4_GET, 6: KeaCommand.NETWORK6_GET}
LEASE_ADD: ByFamily = {4: KeaCommand.LEASE4_ADD, 6: KeaCommand.LEASE6_ADD}
LEASE_DEL: ByFamily = {4: KeaCommand.LEASE4_DEL, 6: KeaCommand.LEASE6_DEL}
LEASE_UPDATE: ByFamily = {4: KeaCommand.LEASE4_UPDATE, 6: KeaCommand.LEASE6_UPDATE}
LEASE_WIPE: ByFamily = {4: KeaCommand.LEASE4_WIPE, 6: KeaCommand.LEASE6_WIPE}
SUBNET_ADD: ByFamily = {4: KeaCommand.SUBNET4_ADD, 6: KeaCommand.SUBNET6_ADD}
SUBNET_DEL: ByFamily = {4: KeaCommand.SUBNET4_DEL, 6: KeaCommand.SUBNET6_DEL}
SUBNET_UPDATE: ByFamily = {4: KeaCommand.SUBNET4_UPDATE, 6: KeaCommand.SUBNET6_UPDATE}
SUBNET_DELTA_ADD: ByFamily = {4: KeaCommand.SUBNET4_DELTA_ADD, 6: KeaCommand.SUBNET6_DELTA_ADD}
SUBNET_DELTA_DEL: ByFamily = {4: KeaCommand.SUBNET4_DELTA_DEL, 6: KeaCommand.SUBNET6_DELTA_DEL}
NETWORK_ADD: ByFamily = {4: KeaCommand.NETWORK4_ADD, 6: KeaCommand.NETWORK6_ADD}
NETWORK_DEL: ByFamily = {4: KeaCommand.NETWORK4_DEL, 6: KeaCommand.NETWORK6_DEL}
NETWORK_SUBNET_ADD: ByFamily = {4: KeaCommand.NETWORK4_SUBNET_ADD, 6: KeaCommand.NETWORK6_SUBNET_ADD}
NETWORK_SUBNET_DEL: ByFamily = {4: KeaCommand.NETWORK4_SUBNET_DEL, 6: KeaCommand.NETWORK6_SUBNET_DEL}

_MANAGED_OPTION_KEYS = frozenset({"code", "name", "space", "data", "csv-format", "always-send", "never-send"})

# One exhausted host backend can legitimately answer with an empty page before the
# cursor moves to the next source index, so allow a few before giving up.
_MAX_EMPTY_RESERVATION_PAGES = 8

# A backend that answers every cursor with a full page and a fresh cursor would grow the
# record list without end, so bound the whole traversal and report why it stopped.
_MAX_RESERVATION_SNAPSHOT_PAGES = 10_000


def _encode_reservation_cursor(source_index: int, from_index: int) -> str | None:
    """Encode Kea's two-part Reservation cursor as one opaque token."""
    if source_index == 0 and from_index == 0:
        return None
    payload = json.dumps([source_index, from_index], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_reservation_cursor(cursor: str | None) -> tuple[int, int]:
    """Decode one opaque Reservation cursor into Kea's source and offset."""
    if cursor is None:
        return 0, 0
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("Reservation cursor must be a non-empty string.")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.b64decode(cursor + padding, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid Reservation cursor.") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in decoded)
    ):
        raise ValueError("Invalid Reservation cursor.")
    return decoded[0], decoded[1]


def _reservation_scope_subnet_id(scope: ReservationScope) -> int:
    """Return the Kea subnet ID for one explicit Reservation Scope."""
    if isinstance(scope, GlobalReservationScope):
        return 0
    if isinstance(scope, InSubnetReservationScope):
        return scope.subnet.subnet_id
    raise ValueError("Unsupported Reservation Scope.")


def _take_matching_raw_option(
    remaining: list[tuple[DHCPOption, dict[str, Any]]], intended_option: DHCPOption
) -> dict[str, Any] | None:
    """Remove and return the raw Option that *intended_option* targets.

    An exact space match wins. Kea may omit the space, and such an Option matches any
    space, so it is claimed only once no exact match is left: taking it first lets the
    Option listed earliest absorb an intent that belongs to another space.
    """
    for exact_space in (True, False):
        for index, (current_option, _raw_option) in enumerate(remaining):
            if current_option.matches_intent(intended_option, exact_space=exact_space):
                return remaining.pop(index)[1]
    return None


def _merge_reservation_options(
    raw_options: list[dict[str, Any]],
    current_options: tuple[DHCPOption, ...],
    intended_options: tuple[DHCPOption, ...],
) -> list[dict[str, Any]]:
    """Overlay managed option facts while preserving matching Kea extension fields."""
    remaining = list(zip(current_options, raw_options, strict=True))
    merged_options: list[dict[str, Any]] = []
    for intended_option in intended_options:
        raw_option = _take_matching_raw_option(remaining, intended_option)
        merged_option = {key: value for key, value in (raw_option or {}).items() if key not in _MANAGED_OPTION_KEYS}
        merged_option.update(_option_data(intended_option))
        merged_options.append(merged_option)
    return merged_options


class KeaResponse(TypedDict):
    """Typed dict representing a single Kea API response object."""

    result: int
    arguments: dict[str, Any] | list[Any] | None
    text: str | None


def _reservation_get_arguments(response: list[KeaResponse]) -> dict[str, Any] | None:
    """Return reservation-get arguments or classify Kea's not-found responses."""
    if not response or not isinstance(response[0], dict):
        raise RuntimeError("reservation-get returned a malformed response.")
    result = response[0]
    if result.get("result") == 3 or (result.get("result") == 0 and result.get("text") == "Host not found."):
        return None
    arguments = result.get("arguments")
    if not isinstance(arguments, dict):
        raise RuntimeError("reservation-get returned malformed arguments.")
    return arguments


# A Pool change is ``subnet{v}-delta-add`` or ``subnet{v}-delta-del``.
PoolAction = Literal["add", "del"]


class PersistResult(NamedTuple):
    """The persistence state that one persist step reached, and why it failed."""

    persistence: Persistence
    diagnostics: tuple[str, ...] = ()


class LeaseQueryGuardError(Exception):
    """Base class for a lease query rejected before an unbounded Kea response."""


class LeaseQueryTooBroad(LeaseQueryGuardError):
    """Raised before an unbounded Kea lease query exceeds the local row limit."""

    def __init__(self, observed_leases: int, max_leases: int) -> None:
        self.observed_leases = observed_leases
        self.max_leases = max_leases
        super().__init__(f"The Subnet has at least {observed_leases} leases; the unpaged query limit is {max_leases}.")


class LeaseQueryNotMeasurable(LeaseQueryGuardError):
    """Raised when Kea cannot count a requested Subnet lease category."""

    def __init__(self, state: int) -> None:
        self.state = state
        super().__init__(f"Kea cannot measure Subnet lease state {state} before an unpaged query.")


class LeaseQueryPreflightUnavailable(LeaseQueryGuardError):
    """Raised when Kea cannot provide a capability required for a safe query."""

    def __init__(self, reason: Literal["statistics", "state-command"] = "statistics") -> None:
        self.reason = reason
        super().__init__(reason)


class LeaseQueryUnknownSubnet(LeaseQueryGuardError):
    """Raised when a Subnet CIDR query names no configured Subnet, so no Subnet ID scopes the read."""


def lease_query_guard_message(exc: LeaseQueryGuardError, state: int | None) -> str:
    """Return safe, actionable guidance for one rejected lease query."""
    if isinstance(exc, LeaseQueryUnknownSubnet):
        # Kea can hold leases under a Subnet ID that its configuration no longer has.
        return "This Subnet CIDR is not configured on the Kea server. Search by Subnet ID instead."
    if isinstance(exc, LeaseQueryNotMeasurable):
        return "Kea cannot safely measure this lease state. Use an exact IP or client identifier search."
    if isinstance(exc, LeaseQueryPreflightUnavailable):
        if exc.reason == "state-command":
            return (
                "State-filtered Subnet searches require Kea 3.1.5 or newer. "
                "Upgrade Kea or use an exact IP or client identifier search."
            )
        return "Kea cannot verify this Subnet query safely. Load the stat_cmds hook or disable the guard explicitly."
    if isinstance(exc, LeaseQueryTooBroad) and state is None:
        return (
            f"This Subnet has at least {exc.observed_leases} leases. "
            "Select the Active or Declined state to narrow the query."
        )
    if isinstance(exc, LeaseQueryTooBroad):
        return (
            f"The selected state has at least {exc.observed_leases} leases. "
            "Use an exact IP or client identifier search."
        )
    return "Kea rejected this unsafe lease query. Use a more specific search."


class _SubnetLeaseCounts(NamedTuple):
    """Lease categories that ``stat_cmds`` can count for one Subnet."""

    covered: int
    active: int
    declined: int


def _lease_cursor(version: int, cursor: str | None) -> IPAddressValue | None:
    """Return the parsed page cursor of one DHCP family, or ``None`` for the start of the scope."""
    if cursor is None:
        return None
    try:
        parsed = ipaddress.ip_address(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid DHCPv{version} lease cursor.") from exc
    if parsed.version != version:
        raise ValueError(f"Lease cursor family IPv{parsed.version} does not match DHCPv{version}.")
    return parsed


def _subnet_id_value(value: Any) -> int:
    """Return the Subnet ID in the Kea range of a lease query value, an integer or its decimal text."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("subnet_id must be a positive integer.")
    try:
        subnet_id = value if isinstance(value, int) else parse_decimal(value)
    except ValueError as exc:
        raise ValueError("subnet_id must be a positive integer.") from exc
    if not constants.MIN_SUBNET_ID <= subnet_id <= constants.MAX_SUBNET_ID:
        raise ValueError(f"subnet_id must be from {constants.MIN_SUBNET_ID} to {constants.MAX_SUBNET_ID}.")
    return subnet_id


def _lease_snapshot(
    server_id: int, query: LeaseQuery, started: datetime, read: LeaseRead, coverage: LeaseCoverage
) -> LeaseSnapshot:
    """Return the Snapshot of *read*, observed between *started* and now."""
    return LeaseSnapshot(
        server_id=server_id,
        family=query.family,
        query=query,
        read_started=started,
        read_finished=_now(),
        records=read.records,
        diagnostics=read.diagnostics,
        coverage=coverage,
        next_cursor=read.next_cursor,
    )


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def subnet_network(value: Any, family: int) -> IPNetworkValue:
    """Parse the Subnet prefix Kea declares; Kea accepts host bits, so this does too."""
    if not isinstance(value, str) or not value:
        raise ValueError("Subnet CIDR must be a non-empty string.")
    network = ipaddress.ip_network(value, strict=False)
    if network.version != family:
        raise ValueError(f"Subnet CIDR {value!r} is IPv{network.version}, not IPv{family}.")
    return network


def _configured_subnet_network(subnet: Any, version: int) -> IPNetworkValue:
    """Return one validated configured Subnet network for the requested family."""
    if not isinstance(subnet, dict):
        raise RuntimeError("config-get returned a malformed Subnet entry.")
    try:
        return subnet_network(subnet.get("subnet"), version)
    except ValueError as exc:
        raise RuntimeError(f"config-get returned a Subnet without a valid IPv{version} CIDR.") from exc


def _configured_subnet_id_for_network(
    subnet_collections: list[list[Any]],
    version: int,
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> int | None:
    """Find one Subnet ID while retaining malformed-entry evidence if no match exists."""
    malformed_entry_error: RuntimeError | None = None
    matching_ids: set[int] = set()
    for subnets in subnet_collections:
        for subnet in subnets:
            try:
                configured_network = _configured_subnet_network(subnet, version)
            except RuntimeError as exc:
                if malformed_entry_error is None:
                    malformed_entry_error = exc
                continue
            if configured_network != network:
                continue
            subnet_id = subnet.get("id")
            if isinstance(subnet_id, bool) or not isinstance(subnet_id, int) or subnet_id < 1:
                raise RuntimeError("config-get returned a Subnet without a valid ID.")
            matching_ids.add(subnet_id)
    # Kea keeps host bits, so two declared prefixes can name one network.
    if len(matching_ids) > 1:
        raise RuntimeError(f"config-get declares more than one Subnet for {network}: IDs {sorted(matching_ids)}.")
    if matching_ids:
        return matching_ids.pop()
    if malformed_entry_error is not None:
        raise malformed_entry_error
    return None


def _replace_managed_option(
    options: list[dict[str, Any]], version: int, field: str, data: str | None
) -> list[dict[str, Any]]:
    """Set the entry that the form field *field* manages to *data*, or remove it when *data* is empty.

    The form edits the value only. Delivery flags stay as they are. An entry whose value the form cannot show
    (empty data, binary-encoded, or a router list for the one gateway) is kept when the field is empty. Form text is
    CSV, so a new value drops a csv-format flag that described the old encoding.

    Raises:
        MalformedConfiguration: If an entry is not a valid DHCP Option, or more than one entry fits the field.

    """
    if data is None:
        return options
    try:
        parsed = parse_dhcp_options(options)
        index = form_managed_entry(parsed, version, field)
    except ValueError as exc:
        raise MalformedConfiguration(str(exc)) from exc
    kept = [option for position, option in enumerate(options) if position != index]
    if data:
        replacement = dict(options[index]) if index is not None else {"name": form_managed_options(version)[field].name}
        unchanged = replacement.get("csv-format") is not False and _same_addresses(data, replacement.get("data"))
        if not unchanged:
            replacement.pop("csv-format", None)
            replacement["data"] = data
        return [*kept, replacement]
    if index is not None and (not parsed[index].data or not form_shows(parsed[index], field)):
        return [*kept, options[index]]
    return kept


def _same_addresses(data: str, old_data: Any) -> bool:
    """Return whether *old_data* holds the addresses of *data*, in another text form or the same one."""
    if not isinstance(old_data, str):
        return False
    try:
        return address_list(data) == address_list(old_data)
    except InvalidAddress:
        return False


def shared_network_description(network: dict[str, Any]) -> str | None:
    """Return the Shared Network description, which Kea keeps as ``user-context.comment``.

    Kea rejects a ``description`` key on a Shared Network, and stores a config-file
    ``comment`` in ``user-context``. A comment that is not a string has no description.

    Raises:
        ValueError: If ``user-context`` is not an object.

    """
    context = network.get("user-context")
    if context is None:
        return None
    if not isinstance(context, dict):
        raise ValueError("A Shared Network user-context must be an object.")
    comment = context.get("comment")
    return comment if isinstance(comment, str) else None


def description_as_shown(text: str) -> str:
    """Return *text* as a text input shows it and Django cleans it: without line breaks and outer spaces."""
    return text.replace("\r", "").replace("\n", "").strip()


def _set_shared_network_description(network: dict[str, Any], description: str) -> None:
    """Write *description* as ``user-context.comment`` and keep every other user-context key.

    An empty *description* keeps a comment the form cannot show, the same as a binary option.
    A *description* equal to the comment as a text input shows it keeps the comment unchanged.
    """
    try:
        existing = shared_network_description(network)
    except ValueError as exc:
        raise MalformedConfiguration(str(exc)) from exc
    if existing is not None and description == description_as_shown(existing):
        return
    context = dict(network.get("user-context") or {})
    if description:
        context["comment"] = description
    elif isinstance(context.get("comment"), str):
        del context["comment"]
    if context:
        network["user-context"] = context
    else:
        network.pop("user-context", None)


class MalformedConfiguration(RuntimeError):
    """The running configuration from ``config-get`` has a shape that NetBox cannot edit safely."""


class CandidateTargetMissing(Exception):
    """The candidate configuration has no object that the edit names."""


def _config_entries(container: dict[str, Any], key: str, service: str) -> list[dict[str, Any]]:
    """Return the objects listed at *key* in a live configuration, or raise ``MalformedConfiguration``."""
    entries = container.get(key, [])
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise MalformedConfiguration(f"config-get returned a malformed {key} list for {service}.")
    return entries


def _config_options(container: dict[str, Any], service: str) -> list[dict[str, Any]]:
    """Return the ``option-data`` of *container* after a check that each entry is a valid DHCP Option."""
    options = _config_entries(container, "option-data", service)
    try:
        parse_dhcp_options(options)
    except ValueError as exc:
        raise MalformedConfiguration(f"config-get returned a malformed option-data list for {service}.") from exc
    return options


@dataclass(frozen=True)
class SharedNetworkEdit:
    """The Shared Network fields that the edit form manages. An empty value removes the field.

    The values that the form showed have the same type, so that an operation can compare them with the live values.
    """

    description: str
    interface: str
    relay_addresses: tuple[str, ...]
    dns_servers: tuple[str, ...]
    ntp_servers: tuple[str, ...]


@dataclass(frozen=True)
class SubnetFields:
    """The Subnet fields that the add form and the edit form both set.

    An empty value leaves the field out of a new Subnet. It removes the field from an edited Subnet, except a DHCP
    Option value that the edit form cannot show.
    """

    pools: tuple[str, ...]
    gateway: str
    dns_servers: tuple[str, ...]
    ntp_servers: tuple[str, ...]
    ddns_qualifying_suffix: str


@dataclass(frozen=True)
class SubnetEdit:
    """The Subnet fields that the edit form manages: the *fields* of both forms, the lifetimes and the timers.

    A lifetime or a timer of None keeps the live value. The values that the form showed have the same type, and there
    None is a value that Kea does not set.
    """

    fields: SubnetFields
    valid_lifetime: int | None
    min_valid_lifetime: int | None
    max_valid_lifetime: int | None
    renew_timer: int | None
    rebind_timer: int | None

    def written_by(self, edit: "SubnetEdit") -> "SubnetEdit":
        """Return these values with None for each lifetime and timer that *edit* keeps, because it cannot change them."""

        def written(value: int | None, new: int | None) -> int | None:
            return None if new is None else value

        return SubnetEdit(
            fields=self.fields,
            valid_lifetime=written(self.valid_lifetime, edit.valid_lifetime),
            min_valid_lifetime=written(self.min_valid_lifetime, edit.min_valid_lifetime),
            max_valid_lifetime=written(self.max_valid_lifetime, edit.max_valid_lifetime),
            renew_timer=written(self.renew_timer, edit.renew_timer),
            rebind_timer=written(self.rebind_timer, edit.rebind_timer),
        )


@dataclass(frozen=True)
class SubnetDefinition:
    """One Subnet as ``subnet{v}-get`` returned it. Two reads are equal when Kea returned the same Subnet."""

    family: Family
    subnet_id: int
    network: IPNetworkValue
    # The reply entry as sorted JSON text, so that the value is immutable and compares by content.
    entry: str

    def edited(self, edit: SubnetEdit) -> dict[str, Any]:
        """Return the Subnet with the fields of *edit*, and every other field that Kea returned.

        Raises:
            MalformedConfiguration: If the Subnet has a value that the edit cannot keep or replace safely.

        """
        subnet = json.loads(self.entry)
        # Kea adds a read-only metadata key to some replies, so the update does not send it back.
        subnet.pop("metadata", None)
        fields = edit.fields
        options = subnet.get("option-data", [])
        if self.family == 4:
            options = _replace_managed_option(options, 4, "gateway", fields.gateway)
        options = _replace_managed_option(options, self.family, "dns_servers", ", ".join(fields.dns_servers))
        subnet["option-data"] = _replace_managed_option(
            options, self.family, "ntp_servers", ", ".join(fields.ntp_servers)
        )
        if fields.ddns_qualifying_suffix:
            subnet["ddns-qualifying-suffix"] = fields.ddns_qualifying_suffix
        else:
            subnet.pop("ddns-qualifying-suffix", None)
        live_pools = self._pools_by_range(subnet.get("pools", []))
        subnet["pools"] = [{**live_pools.get(pool, {}), "pool": pool} for pool in fields.pools]
        # A Subnet takes the *-lifetime keys; valid-lft is a lease field that Kea refuses here.
        for key, value in (
            ("valid-lifetime", edit.valid_lifetime),
            ("min-valid-lifetime", edit.min_valid_lifetime),
            ("max-valid-lifetime", edit.max_valid_lifetime),
            ("renew-timer", edit.renew_timer),
            ("rebind-timer", edit.rebind_timer),
        ):
            if value is not None:
                subnet[key] = value
        return subnet

    def _pools_by_range(self, pools: list[Any]) -> dict[str, dict[str, Any]]:
        """Return each live Pool entry by its range text, so that a kept Pool keeps its Pool-level fields.

        Raises:
            MalformedConfiguration: If a live Pool entry is not an object or has no range in the Subnet.

        """
        by_range: dict[str, dict[str, Any]] = {}
        for entry in pools:
            if not isinstance(entry, dict):
                raise MalformedConfiguration(
                    f"subnet{self.family}-get returned a non-object Pool entry for Subnet {self.subnet_id}."
                )
            try:
                by_range[parse_pool(entry.get("pool"), self.network).range] = entry
            except ValueError as exc:
                raise MalformedConfiguration(
                    f"subnet{self.family}-get returned a malformed Pool {entry.get('pool')!r} for Subnet "
                    f"{self.subnet_id}."
                ) from exc
        return by_range


class CandidateConfiguration:
    """The running configuration of one daemon from ``config-get``, which a read-modify-write edits in place.

    Every field that Kea returned stays, so a ``config-set`` of it keeps the fields that NetBox does not model.
    An edit raises ``MalformedConfiguration`` for a configuration that it cannot edit safely, and
    ``CandidateTargetMissing`` when the object it names is absent. An error in the submitted rows propagates.
    """

    def __init__(self, family: Family, arguments: dict[str, Any]) -> None:
        """Keep *arguments*, the ``config-get`` arguments without ``hash``.

        Raises:
            MalformedConfiguration: If the ``Dhcp{v}`` block is not an object.

        """
        daemon = arguments.get(f"Dhcp{family}")
        if not isinstance(daemon, dict):
            raise MalformedConfiguration(f"config-get returned a non-object Dhcp{family} for dhcp{family}.")
        self.family = family
        self.service = f"dhcp{family}"
        self.arguments = arguments
        self._daemon = daemon

    def set_global_options(self, rows: list[dict[str, Any]]) -> None:
        """Merge the options form *rows* into the server-global DHCP Options."""
        self._daemon["option-data"] = merge_option_form_rows(rows, _config_options(self._daemon, self.service))

    def set_subnet_options(self, subnet_id: int, network: IPNetworkValue, rows: list[dict[str, Any]]) -> None:
        """Merge the options form *rows* into the Subnet with *subnet_id*, only while that ID names *network*."""
        subnet_key = f"subnet{self.family}"
        subnets = list(_config_entries(self._daemon, subnet_key, self.service))
        for shared_network in _config_entries(self._daemon, "shared-networks", self.service):
            subnets.extend(_config_entries(shared_network, subnet_key, self.service))
        matches = [subnet for subnet in subnets if _is_subnet_id(subnet.get("id"), subnet_id)]
        if len(matches) > 1:
            raise MalformedConfiguration(f"config-get declares more than one Subnet with ID {subnet_id}.")
        if not matches:
            raise CandidateTargetMissing
        try:
            declared = subnet_network(matches[0].get("subnet"), self.family)
        except ValueError as exc:
            raise MalformedConfiguration(f"config-get returned Subnet {subnet_id} without a valid CIDR.") from exc
        if declared != network:
            raise CandidateTargetMissing
        matches[0]["option-data"] = merge_option_form_rows(rows, _config_options(matches[0], self.service))

    def add_option_definition(self, option_def: dict[str, Any]) -> None:
        """Append *option_def* to the Option Definitions."""
        self._daemon["option-def"] = [*_config_entries(self._daemon, "option-def", self.service), option_def]

    def delete_option_definition(self, code: int, space: str) -> None:
        """Remove the Option Definitions with *code* in *space*."""
        definitions = _config_entries(self._daemon, "option-def", self.service)
        kept = [entry for entry in definitions if not (entry.get("code") == code and entry.get("space") == space)]
        if len(kept) == len(definitions):
            raise CandidateTargetMissing
        self._daemon["option-def"] = kept

    def edit_shared_network(self, name: str, edit: SharedNetworkEdit) -> None:
        """Set the managed fields of the Shared Network *name*, and keep every other field."""
        matches = [
            network
            for network in _config_entries(self._daemon, "shared-networks", self.service)
            if network.get("name") == name
        ]
        if len(matches) > 1:
            raise MalformedConfiguration(f"config-get declares more than one Shared Network named {name!r}.")
        if not matches:
            raise CandidateTargetMissing
        network = matches[0]
        _set_shared_network_description(network, edit.description)
        if edit.interface:
            network["interface"] = edit.interface
        else:
            network.pop("interface", None)
        if edit.relay_addresses:
            network["relay"] = {"ip-addresses": list(edit.relay_addresses)}
        else:
            network.pop("relay", None)
        options = list(_config_entries(network, "option-data", self.service))
        options = _replace_managed_option(options, self.family, "dns_servers", ",".join(edit.dns_servers))
        options = _replace_managed_option(options, self.family, "ntp_servers", ",".join(edit.ntp_servers))
        network["option-data"] = options


def _is_subnet_id(value: Any, subnet_id: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == subnet_id


class KeaClient:
    """HTTP client for the Kea Control API."""

    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        verify: bool | str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        persist_config: bool = True,
        send_service: bool = True,
        on_config_change: Callable[[], None] | None = None,
        *,
        timeout: int,
        max_unpaged_leases: int | None,
        write_guard: WriteGuard,
    ):
        """Initialise a Kea HTTP client session.

        Args:
            url: Base URL of the Kea Control Agent or DHCP daemon endpoint.
            username: Optional HTTP Basic Auth username.
            password: Optional HTTP Basic Auth password.
            verify: SSL verification — True/False or path to a CA bundle.
            client_cert: Path to client certificate for mutual TLS.
            client_key: Path to private key matching client_cert.
            timeout: Required. Request timeout in seconds; ``Server.get_client()`` passes ``kea_timeout``.
            persist_config: When True (default), ``config-write`` is issued after
                each mutation.  Set to False when Kea configuration is managed
                externally (e.g. Ansible, Puppet) and you do not want the plugin
                to overwrite the on-disk config file.
            send_service: When True (default), the ``service`` argument is included
                in the command body — correct for a Control Agent, which routes by
                service.  Set to False when *url* points **directly** at a DHCP
                daemon: Kea 3.2.0+ rejects a ``service`` that does not match the
                daemon, and ISC recommends omitting it for direct connections
                (3.0.x silently ignored it).
            max_unpaged_leases: Reject an unpaged Subnet query when Kea reports
                more than this many covered leases. ``None`` disables the guard. Required, so no default
                here can drift from the ``lease_query_max_unpaged_leases`` plugin setting.
            on_config_change: Optional callback invoked after Kea's live configuration
                changes. Cache invalidation failures are logged and do not interrupt
                persistence of an already-applied change.
            write_guard: Required. Refuses each write command before it is sent. ``Server.get_client()``
                passes the branch binding; a caller that passes another guard owns that choice.

        Raises:
            ValueError: If only one of client_cert/client_key is provided.

        """
        if (client_cert is not None and client_key is None) or (client_cert is None and client_key is not None):
            raise ValueError("Key and Cert must be used together.")
        if max_unpaged_leases is not None and (
            isinstance(max_unpaged_leases, bool) or not isinstance(max_unpaged_leases, int) or max_unpaged_leases < 1
        ):
            raise ValueError("max_unpaged_leases must be a positive integer or None.")

        self.url = url
        self.timeout = timeout
        self.persist_config = persist_config
        self.send_service = send_service
        self.max_unpaged_leases = max_unpaged_leases
        self._on_config_change = on_config_change
        self.write_guard = write_guard

        # command() passes these on each request, because requests lets the environment replace session values.
        self.verify: bool | str = True if verify is None else verify
        self.cert: tuple[str, str] | None = None
        if client_cert is not None and client_key is not None:
            self.cert = (client_cert, client_key)
        self._session = requests.Session()
        if username is not None and password is not None:
            self._session.auth = HTTPBasicAuth(username, password)

    def command(
        self,
        command: KeaCommand,
        target: Family | None,
        arguments: dict[str, Any] | None = None,
        check: Sequence[int] | None = (0,),
    ) -> list[KeaResponse]:
        """Send a command to the Kea API and return the response list. This is the only HTTP send of the plugin.

        Args:
            command: The Kea command.
            target: The DHCP family whose daemon runs the command, or ``None`` for the Control Agent itself.
                A family becomes the ``service`` of the request body only when ``send_service`` is true;
                a direct daemon connection omits it, see :meth:`__init__`.
            arguments: Optional command arguments payload.
            check: Sequence of acceptable result codes. Pass ``None`` to skip checking.

        Returns:
            Parsed JSON response as a list of KeaResponse dicts.

        Raises:
            TypeError: If *command* is not a KeaCommand member.
            ValueError: If *target* is not 4, 6 or None.
            BranchActive: If *command* is a write and the write guard refuses it, for example in a branch.
            KeaTLSFileError: If requests cannot find a TLS file of the client. It is a ``RequestException``.
            requests.HTTPError: If the HTTP response status is not 2xx.
            KeaException: If any response result code is not in *check*.

        """
        if not isinstance(command, KeaCommand):
            raise TypeError(f"command must be a KeaCommand member, not {type(command).__name__}")
        if target is not None and (isinstance(target, bool) or target not in (4, 6)):
            raise ValueError(f"target must be 4, 6 or None, not {target!r}")
        self._refuse(command)
        body: dict[str, Any] = {"command": command.value}

        # Kea 3.2.0+ rejects a service that does not match the daemon that the URL already targets.
        if target is not None and self.send_service:
            body["service"] = [f"dhcp{target}"]

        if arguments is not None:
            body["arguments"] = arguments

        try:
            resp = self._session.post(self.url, json=body, timeout=self.timeout, verify=self.verify, cert=self.cert)
        except requests.RequestException:
            raise
        except OSError as exc:
            raise KeaTLSFileError("A TLS CA, certificate or key file of the client could not be found.") from exc
        resp.raise_for_status()
        resp_json = resp.json()
        if not isinstance(resp_json, list):
            raise ValueError(f"Expected list response from Kea API, got {type(resp_json).__name__}")
        if check is not None:
            check_response(resp_json, check)
        return resp_json

    def clone(self) -> "KeaClient":
        """Return a new KeaClient that shares the same connection settings.

        ``requests.Session`` is not thread-safe, so parallel workers must each
        call ``client.clone()`` rather than sharing a single ``KeaClient``
        instance across threads.
        """
        new = KeaClient.__new__(KeaClient)
        new.url = self.url
        new.timeout = self.timeout
        new.verify = self.verify
        new.cert = self.cert
        new._session = requests.Session()
        new._session.auth = self._session.auth
        new.persist_config = self.persist_config
        new.send_service = self.send_service
        new.max_unpaged_leases = self.max_unpaged_leases
        new._on_config_change = self._on_config_change
        # Thread-pool workers do not inherit context variables, so the clone keeps the branch binding.
        new.write_guard = self.write_guard
        return new

    def _refuse(self, command: KeaCommand) -> None:
        """Ask the write guard before a write member, before any side effect of sending it."""
        if command.is_write:
            self.write_guard.refuse(f"Kea command {command.value}")

    def _notify_config_change(self, family: Family) -> None:
        """Notify the owner without interrupting persistence of a live change."""
        if self._on_config_change is None:
            return
        try:
            self._on_config_change()
        except Exception:
            logger.exception("Configuration changed for DHCPv%s, but cache invalidation failed", family)

    def _config_mutation_command(self, command: KeaCommand, family: Family, arguments: dict[str, Any]) -> None:
        """Send one live configuration mutation between invalidation notifications, and require one success reply."""
        self._refuse(command)
        self._notify_config_change(family)
        try:
            response = self.command(command, family, arguments=arguments, check=None)
        finally:
            # The response can be lost after Kea applies the command. Invalidate again
            # so a read during the request cannot repopulate the active cache generation.
            self._notify_config_change(family)
        _one_reply(command, family, response)

    def close(self) -> None:
        """Close the underlying requests.Session and release connection resources."""
        self._session.close()

    def __enter__(self) -> "KeaClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_available_commands(self, family: Family) -> set[str]:
        """Return the set of commands available on the daemon of *family*.

        Returns:
            Set of command name strings reported by ``list-commands``.

        """
        resp = self.command(KeaCommand.LIST_COMMANDS, family)
        if not resp or not isinstance(resp[0], dict):
            raise RuntimeError(f"list-commands returned malformed response: {resp!r}")
        arguments = resp[0].get("arguments")
        if not isinstance(arguments, list) or any(not isinstance(command, str) for command in arguments):
            raise RuntimeError(f"list-commands returned malformed arguments: {resp[0]!r}")
        return set(arguments)

    def reservation_capabilities(self, version: Family) -> ReservationCapabilities:
        """Read live identifier configuration and host command availability."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        commands = self.get_available_commands(version)
        response = self.command(KeaCommand.CONFIG_GET, version)
        if not response or not isinstance(response[0], dict):
            raise RuntimeError("config-get returned a malformed response.")
        arguments = response[0].get("arguments")
        dhcp = arguments.get(f"Dhcp{version}") if isinstance(arguments, dict) else None
        if not isinstance(dhcp, dict):
            raise RuntimeError("config-get returned malformed DHCP configuration.")
        if "host-reservation-identifiers" in dhcp:
            configured = dhcp["host-reservation-identifiers"]
        else:
            configured = ["hw-address", "duid", "circuit-id", "client-id"] if version == 4 else ["duid", "hw-address"]
        if not isinstance(configured, list) or any(not isinstance(identifier, str) for identifier in configured):
            raise RuntimeError("config-get returned malformed host-reservation-identifiers.")

        supported = reservation_identifier_types(version)
        hooks = dhcp.get("hooks-libraries", [])
        if not isinstance(hooks, list):
            raise RuntimeError("config-get returned malformed hooks-libraries.")
        flex_hook = any(
            isinstance(hook, dict) and isinstance(hook.get("library"), str) and "libdhcp_flex_id" in hook["library"]
            for hook in hooks
        )
        available: tuple[IdentifierType, ...] = tuple(
            cast(IdentifierType, identifier)
            for identifier in configured
            if identifier in supported and (identifier != "flex-id" or flex_hook)
        )
        unavailable = tuple(
            (
                cast(IdentifierType, identifier),
                "The Flex ID hook is not configured."
                if identifier == "flex-id" and identifier in configured
                else "This identifier is not enabled in host-reservation-identifiers.",
            )
            for identifier in supported
            if identifier not in available
        )
        required_commands = {
            member.value
            for member in (
                KeaCommand.RESERVATION_GET,
                KeaCommand.RESERVATION_ADD,
                KeaCommand.RESERVATION_UPDATE,
                KeaCommand.RESERVATION_DEL,
            )
        }
        missing_commands = required_commands - commands
        mutation_available = bool(available) and not missing_commands
        if missing_commands:
            explanation = "The host_cmds hook does not provide all required Reservation commands."
        elif not available:
            explanation = "No supported Reservation identifier is enabled."
        else:
            explanation = ""
        return ReservationCapabilities(
            family=version,
            identifiers=available,
            mutation_available=mutation_available,
            explanation=explanation,
            unavailable_identifiers=unavailable,
        )

    def _reservation_raw_page(
        self,
        family: Family,
        source_index: int = 0,
        from_index: int = 0,
        limit: int = 100,
        subnet_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch a page of host reservations from Kea.

        Args:
            family: The DHCP family whose daemon holds the reservations.
            source_index: 0 = all sources, 1+ = specific backend source index.
            from_index: Starting offset within the source (use ``next_from`` returned
                by a previous call to continue pagination).
            limit: Maximum number of hosts to return per page.
            subnet_id: Restrict the page to one subnet. ``None`` reads every subnet.

        Returns:
            A ``(hosts, next_from, next_source_index)`` tuple.  Both ``next_from``
            and ``next_source_index`` are always read from Kea's ``next`` cursor.
            Pass them as ``from_index`` / ``source_index`` on the next call to
            continue paginating; both will be 0 when the source is exhausted.

        Raises:
            KeaException: If Kea returns result code 1 or 2 (error / unknown command).

        """
        arguments: dict[str, Any] = {"source-index": source_index, "from": from_index, "limit": limit}
        if subnet_id is not None:
            arguments["subnet-id"] = subnet_id
        resp = self.command(
            KeaCommand.RESERVATION_GET_PAGE,
            family,
            arguments=arguments,
            check=(0, 3),
        )
        if not resp or not isinstance(resp[0], dict):
            raise RuntimeError("reservation-get-page returned a malformed response.")
        if resp[0].get("result") == 3:
            return [], 0, 0
        args = resp[0].get("arguments")
        if not isinstance(args, dict) or not isinstance(args.get("hosts"), list):
            raise RuntimeError("reservation-get-page returned malformed arguments.")
        next_obj = args.get("next")
        if not isinstance(next_obj, dict):
            raise RuntimeError("reservation-get-page returned a malformed next cursor.")
        next_from = next_obj.get("from")
        next_source = next_obj.get("source-index")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (next_from, next_source)
        ):
            raise RuntimeError("reservation-get-page returned a malformed next cursor.")
        return cast(list[dict[str, Any]], args["hosts"]), cast(int, next_from), cast(int, next_source)

    def reservation_page(
        self,
        version: Family,
        catalogue,
        *,
        cursor: str | None = None,
        limit: int = 100,
        subnet_id: int | None = None,
        max_raw_pages: int | None = None,
    ) -> ReservationSnapshot:
        """Return a typed page, retaining its cursor when ``max_raw_pages`` stops filling it."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer.")
        if max_raw_pages is not None and (
            isinstance(max_raw_pages, bool) or not isinstance(max_raw_pages, int) or max_raw_pages < 1
        ):
            raise ValueError("max_raw_pages must be a positive integer.")
        source_index, from_index = _decode_reservation_cursor(cursor)
        hosts: list[dict[str, Any]] = []
        next_source, next_from = source_index, from_index
        seen_cursors = {(next_source, next_from)}
        empty_pages = 0
        raw_pages = 0
        while len(hosts) < limit:
            remaining = limit - len(hosts)
            page, candidate_from, candidate_source = self._reservation_raw_page(
                version,
                source_index=next_source,
                from_index=next_from,
                limit=remaining,
                subnet_id=subnet_id,
            )
            raw_pages += 1
            if len(page) > remaining:
                raise RuntimeError("reservation-get-page exceeded the requested page limit.")
            hosts.extend(page)
            if page:
                # An empty page between real ones is a source transition, not a stall.
                empty_pages = 0
            else:
                # Only a non-empty page moves this loop towards its limit. A backend that
                # keeps advancing the cursor over empty pages would never end it.
                empty_pages += 1
                if empty_pages > _MAX_EMPTY_RESERVATION_PAGES:
                    raise RuntimeError("reservation-get-page returned only empty pages.")
            candidate = (candidate_source, candidate_from)
            if candidate == (0, 0):
                next_cursor = None
                break
            if candidate in seen_cursors:
                raise RuntimeError("Reservation page cursor did not advance.")
            next_source, next_from = candidate
            if len(hosts) == limit or (max_raw_pages is not None and raw_pages >= max_raw_pages):
                next_cursor = _encode_reservation_cursor(next_source, next_from)
                break
            seen_cursors.add(candidate)
        return _parse_reservation_page(hosts, version, catalogue, next_cursor)

    def reservation_by_identity(
        self,
        version: Family,
        catalogue,
        scope: ReservationScope,
        identity: ReservationIdentity,
    ) -> Reservation | None:
        """Return one exact typed Reservation Identity target."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if identity.identifier_type not in reservation_identifier_types(version):
            raise ValueError(f"{identity.identifier_type} is not supported for DHCPv{version} Reservations.")
        raw = self._reservation_raw_by_identity(version, scope, identity)
        if raw is None:
            return None
        reservation = _exact_reservation(raw, version, catalogue)
        if reservation.scope != scope or reservation.identity != identity:
            raise MalformedReservation(
                "target-mismatch",
                "Kea returned a Reservation that does not match the exact target.",
            )
        return reservation

    def _reservation_raw_by_identity(
        self,
        version: Family,
        scope: ReservationScope,
        identity: ReservationIdentity,
    ) -> dict[str, Any] | None:
        """Fetch one exact raw Reservation for private read-modify-write use."""
        subnet_id = _reservation_scope_subnet_id(scope)
        response = self.command(
            KeaCommand.RESERVATION_GET,
            version,
            arguments={
                "subnet-id": subnet_id,
                "identifier-type": identity.identifier_type,
                "identifier": identity.value,
            },
            check=(0, 3),
        )
        return _reservation_get_arguments(response)

    def _reservation_raw_by_address(
        self,
        version: Family,
        scope: InSubnetReservationScope,
        address: str,
    ) -> dict[str, Any] | None:
        """Fetch one scoped raw Reservation by allocation address."""
        response = self.command(
            KeaCommand.RESERVATION_GET,
            version,
            arguments={"subnet-id": scope.subnet.subnet_id, "ip-address": address},
            check=(0, 3),
        )
        return _reservation_get_arguments(response)

    def reservation_by_address(
        self,
        version: Family,
        catalogue,
        scope: ReservationScope,
        address: str,
    ) -> Reservation | None:
        """Resolve one scoped allocation address to its canonical Reservation."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if not isinstance(scope, InSubnetReservationScope):
            raise ValueError("Reservation address discovery requires an In-Subnet Scope.")
        try:
            parsed_address = ipaddress.ip_address(address)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid DHCPv{version} Reservation address.") from exc
        if parsed_address.version != version or parsed_address not in scope.subnet.network:
            raise ValueError("The Reservation address must belong to its In-Subnet Scope.")
        raw = self._reservation_raw_by_address(version, scope, str(parsed_address))
        if raw is None:
            return None
        reservation = _exact_reservation(raw, version, catalogue)
        if reservation.scope != scope or parsed_address not in reservation.addresses:
            raise MalformedReservation(
                "target-mismatch",
                "Kea returned a Reservation that does not match the scoped address target.",
            )
        return reservation

    def reservations_by_hostname(
        self,
        version: Family,
        catalogue,
        hostname: str,
    ) -> ReservationSnapshot:
        """Return the typed Reservations that match one exact hostname."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if not isinstance(hostname, str) or not hostname:
            raise ValueError("hostname must be a non-empty string.")
        response = self.command(
            KeaCommand.RESERVATION_GET_BY_HOSTNAME,
            version,
            arguments={"hostname": hostname},
            check=(0, 3),
        )
        if not response or not isinstance(response[0], dict):
            raise RuntimeError("reservation-get-by-hostname returned a malformed response.")
        if response[0].get("result") == 3:
            return _parse_reservation_page([], version, catalogue, None)
        arguments = response[0].get("arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError("reservation-get-by-hostname returned malformed arguments.")
        return _parse_reservation_page(
            arguments.get("hosts"),
            version,
            catalogue,
            None,
            expected_hostname=hostname,
        )

    def reservation_snapshot(
        self,
        version: Family,
        catalogue,
        *,
        page_size: int = 100,
        subnet_id: int | None = None,
    ) -> ReservationSnapshot:
        """Traverse bounded pages and return one non-atomic Reservation Snapshot.

        ``subnet_id`` restricts the traversal to one subnet, so a caller that needs one
        subnet does not read every reservation on the server.
        """
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        records: list[Reservation] = []
        diagnostics: list[ReservationDiagnostic] = []
        cursor = None
        seen_cursors: set[str] = set()
        pages_fetched = 0
        while True:
            try:
                page = self.reservation_page(
                    version,
                    catalogue,
                    cursor=cursor,
                    limit=page_size,
                    subnet_id=subnet_id,
                )
            except (KeaException, requests.RequestException, RuntimeError, ValueError):
                if pages_fetched == 0:
                    raise
                diagnostics.append(
                    ReservationDiagnostic(
                        code=RESERVATION_PAGE_FETCH_FAILED,
                        message="Reservation page traversal did not complete.",
                        source_position=f"pages[{pages_fetched}]",
                    )
                )
                break
            pages_fetched += 1
            records.extend(page.records)
            diagnostics.extend(page.diagnostics)
            if page.next_cursor is None:
                break
            if page.next_cursor == cursor or page.next_cursor in seen_cursors:
                diagnostics.append(
                    ReservationDiagnostic(
                        code=RESERVATION_PAGINATION_STALLED,
                        message="Reservation page traversal did not complete because its cursor did not advance.",
                        source_position=f"pages[{pages_fetched - 1}].next",
                    )
                )
                break
            if pages_fetched >= _MAX_RESERVATION_SNAPSHOT_PAGES:
                diagnostics.append(
                    ReservationDiagnostic(
                        code=RESERVATION_PAGE_LIMIT_REACHED,
                        message="Reservation page traversal did not complete because it reached its page limit.",
                        source_position=f"pages[{pages_fetched - 1}].next",
                    )
                )
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        return ReservationSnapshot(
            family=version,
            records=tuple(records),
            diagnostics=tuple(diagnostics),
            complete=not diagnostics,
            next_cursor=None,
        )

    def _reservation_mutation_command(self, command: KeaCommand, version: Family, arguments: dict[str, Any]) -> None:
        """Apply one Reservation command and require one success reply."""
        _one_reply(command, version, self.command(command, version, arguments=arguments, check=None))

    def _verify_reservation(
        self,
        intended: Reservation | None,
        target: Reservation,
        catalogue,
    ) -> Literal["verified", "failed"]:
        """Refetch one mutation target and compare its managed typed facts."""
        try:
            observed = self.reservation_by_identity(
                target.family,
                catalogue,
                target.scope,
                target.identity,
            )
        except (KeaException, MalformedReservation, requests.RequestException, RuntimeError, ValueError):
            logger.warning("Could not verify a confirmed Reservation mutation", exc_info=True)
            return "failed"
        if observed is None or intended is None:
            return "verified" if observed is intended else "failed"
        return "verified" if reservation_matches_intent(observed, intended) else "failed"

    def reservation_create(self, reservation: Reservation, catalogue) -> ReservationMutationResult:
        """Create one typed In-Subnet Reservation and verify the result."""
        if isinstance(reservation.scope, GlobalReservationScope):
            raise ValueError("Creating Global Reservations is not supported.")
        raw = _reservation_to_raw(reservation)
        self._reservation_mutation_command(
            KeaCommand.RESERVATION_ADD,
            reservation.family,
            {"reservation": raw},
        )
        persisted = self.persist(reservation.family)
        return ReservationMutationResult(
            previous=None,
            intended=reservation,
            application="applied",
            persistence=persisted.persistence,
            persistence_diagnostics=persisted.diagnostics,
            verification=self._verify_reservation(reservation, reservation, catalogue),
        )

    def reservation_change(
        self,
        target: Reservation,
        expected_fingerprint: str,
        change: ReservationChange,
        catalogue,
    ) -> ReservationMutationResult:
        """Update mutable managed facts and preserve the latest unknown Kea fields."""
        if isinstance(target.scope, GlobalReservationScope):
            raise ValueError("Updating Global Reservations is not supported.")
        raw = self._reservation_raw_by_identity(target.family, target.scope, target.identity)
        if raw is None:
            raise ReservationConflict("The Reservation no longer exists.")
        current = _exact_reservation(raw, target.family, catalogue)
        if current.scope != target.scope or current.identity != target.identity:
            raise MalformedReservation("target-mismatch", "Kea returned a different Reservation target.")
        if reservation_fingerprint(current) != expected_fingerprint:
            raise ReservationConflict("The Reservation changed after the edit form was opened.")
        intended = apply_reservation_change(current, change)
        raw_options = raw.get("option-data", [])
        if not isinstance(raw_options, list) or any(not isinstance(option, dict) for option in raw_options):
            raise MalformedReservation("invalid-options", "The Reservation contains invalid DHCP Options.")
        serialized = _reservation_to_raw(intended)
        if intended.options:
            serialized["option-data"] = _merge_reservation_options(raw_options, current.options, intended.options)
        merged = dict(raw)
        for key in (
            "subnet-id",
            "hw-address",
            "duid",
            "circuit-id",
            "client-id",
            "flex-id",
            "remote-id",
            "ip-address",
            "ip-addresses",
            "prefixes",
            "hostname",
            "option-data",
        ):
            merged.pop(key, None)
        merged.update(serialized)
        self._reservation_mutation_command(
            KeaCommand.RESERVATION_UPDATE,
            target.family,
            {"reservation": merged},
        )
        persisted = self.persist(target.family)
        return ReservationMutationResult(
            previous=current,
            intended=intended,
            application="applied",
            persistence=persisted.persistence,
            persistence_diagnostics=persisted.diagnostics,
            verification=self._verify_reservation(intended, target, catalogue),
        )

    def reservation_delete(self, target: Reservation, catalogue) -> ReservationMutationResult:
        """Delete one typed In-Subnet Reservation by Scope and Identity."""
        if isinstance(target.scope, GlobalReservationScope):
            raise ValueError("Deleting Global Reservations is not supported.")
        raw = self._reservation_raw_by_identity(target.family, target.scope, target.identity)
        if raw is None:
            raise ReservationConflict("The Reservation no longer exists.")
        current = _exact_reservation(raw, target.family, catalogue)
        if current.scope != target.scope or current.identity != target.identity:
            raise MalformedReservation("target-mismatch", "Kea returned a different Reservation target.")
        self._reservation_mutation_command(
            KeaCommand.RESERVATION_DEL,
            target.family,
            {
                "subnet-id": target.scope.subnet.subnet_id,
                "identifier-type": target.identity.identifier_type,
                "identifier": target.identity.value,
            },
        )
        persisted = self.persist(target.family)
        return ReservationMutationResult(
            previous=current,
            intended=None,
            application="applied",
            persistence=persisted.persistence,
            persistence_diagnostics=persisted.diagnostics,
            verification=self._verify_reservation(None, target, catalogue),
        )

    def configured_subnet_id_from_cidr(self, version: Family, cidr: str) -> int | None:
        """Resolve a CIDR from the running config without requiring ``subnet_cmds``."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        try:
            network = ipaddress.ip_network(cidr, strict=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"subnet must be a valid IPv{version} CIDR.") from exc
        if network.version != version:
            raise ValueError(f"Subnet family IPv{network.version} does not match DHCPv{version}.")

        response = self.command(KeaCommand.CONFIG_GET, version)
        if not response or not isinstance(response[0], dict):
            raise RuntimeError("config-get returned a malformed response.")
        arguments = response[0].get("arguments")
        dhcp_config = arguments.get(f"Dhcp{version}") if isinstance(arguments, dict) else None
        if not isinstance(dhcp_config, dict):
            raise RuntimeError(f"config-get returned malformed Dhcp{version} configuration.")

        subnet_key = f"subnet{version}"
        top_level = dhcp_config.get(subnet_key, [])
        shared_networks = dhcp_config.get("shared-networks", [])
        if not isinstance(top_level, list) or not isinstance(shared_networks, list):
            raise RuntimeError("config-get returned malformed Subnet collections.")
        subnet_collections = [top_level]
        for shared_network in shared_networks:
            if not isinstance(shared_network, dict):
                raise RuntimeError("config-get returned a malformed shared-network entry.")
            shared_subnets = shared_network.get(subnet_key, [])
            if not isinstance(shared_subnets, list):
                raise RuntimeError("config-get returned a malformed shared-network entry.")
            subnet_collections.append(shared_subnets)

        return _configured_subnet_id_for_network(subnet_collections, version, network)

    def subnet_add(self, version: Family, subnet_id: int, cidr: str, fields: SubnetFields) -> None:
        """Send one ``subnet{v}-add`` for the Subnet *cidr* with *subnet_id*. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        subnet: dict[str, Any] = {"subnet": cidr, "id": subnet_id}
        if fields.pools:
            subnet["pools"] = [{"pool": pool} for pool in fields.pools]
        managed = form_managed_options(version)
        option_data: list[dict[str, str]] = []
        if fields.gateway and version == 4:
            option_data.append({"name": managed["gateway"].name, "data": fields.gateway})
        if fields.dns_servers:
            option_data.append({"name": managed["dns_servers"].name, "data": ", ".join(fields.dns_servers)})
        if fields.ntp_servers:
            option_data.append({"name": managed["ntp_servers"].name, "data": ", ".join(fields.ntp_servers)})
        if option_data:
            subnet["option-data"] = option_data
        if fields.ddns_qualifying_suffix:
            subnet["ddns-qualifying-suffix"] = fields.ddns_qualifying_suffix
        self._config_mutation_command(SUBNET_ADD[version], version, {f"subnet{version}": [subnet]})

    def subnet_del(self, version: Family, subnet_id: int) -> None:
        """Send one ``subnet{v}-del``. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_mutation_command(SUBNET_DEL[version], version, {"id": subnet_id})

    def shared_network_exists(self, version: Family, name: str) -> bool:
        """Return whether the daemon has a Shared Network named *name*.

        Raises:
            KeaException: If Kea returns a result other than 0 (found) or 3 (not found).
            RuntimeError: If the reply is malformed or names another Shared Network.

        """
        command = NETWORK_GET[version]
        reply = self._one_command(command, version, {"name": name}, (0, 3))
        if reply["result"] == 3:
            return False
        arguments = reply.get("arguments")
        networks = arguments.get("shared-networks") if isinstance(arguments, dict) else None
        if (
            not isinstance(networks, list)
            or len(networks) != 1
            or not isinstance(networks[0], dict)
            or networks[0].get("name") != name
        ):
            raise RuntimeError(f"{command.value} returned a malformed Shared Network for {name!r}.")
        return True

    def network_add(self, version: Family, name: str) -> None:
        """Send one ``network{v}-add`` for an empty Shared Network. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_mutation_command(NETWORK_ADD[version], version, {"shared-networks": [{"name": name}]})

    def network_del(self, version: Family, name: str) -> None:
        """Send one ``network{v}-del``. Member Subnets stay and leave the Shared Network. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_mutation_command(NETWORK_DEL[version], version, {"name": name})

    def forwarding_failed(self, exc: "KeaException", version: Family) -> bool:
        """Return whether a Control Agent answered that it could not forward a command to the daemon.

        The agent sends this answer also when it loses the daemon's reply, so the daemon can have run the command.
        """
        text = exc.response.get("text")
        return (
            self.send_service
            and exc.response.get("result") == 1
            and isinstance(text, str)
            and text.startswith(f"unable to forward command to the dhcp{version} service")
        )

    def network_subnet_add(self, version: Family, name: str, subnet_id: int) -> None:
        """Send one ``network{v}-subnet-add`` that moves the Subnet into the Shared Network *name*. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_mutation_command(NETWORK_SUBNET_ADD[version], version, {"name": name, "id": subnet_id})

    def network_subnet_del(self, version: Family, name: str, subnet_id: int) -> None:
        """Send one ``network{v}-subnet-del``. The Subnet stays, outside any Shared Network. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_mutation_command(NETWORK_SUBNET_DEL[version], version, {"name": name, "id": subnet_id})

    def subnet_definition(self, version: Family, subnet_id: int) -> SubnetDefinition:
        """Send one ``subnet{v}-get`` and return the Subnet with *subnet_id*, for an update.

        Raises:
            KeaException: If Kea returns a failure result, or no Subnet.
            RuntimeError: If the reply is malformed, names another Subnet ID, or has no valid CIDR or option-data.

        """
        entry = self.subnet_get(version, subnet_id)
        if not _is_subnet_id(entry.get("id"), subnet_id):
            raise RuntimeError(f"subnet{version}-get returned another Subnet for ID {subnet_id}.")
        try:
            network = subnet_network(entry.get("subnet"), version)
        except ValueError as exc:
            raise RuntimeError(f"subnet{version}-get returned Subnet {subnet_id} without a valid CIDR.") from exc
        options = entry.get("option-data", [])
        if not isinstance(options, list) or not all(isinstance(option, dict) for option in options):
            raise RuntimeError(f"subnet{version}-get returned Subnet {subnet_id} with a malformed option-data list.")
        return SubnetDefinition(version, subnet_id, network, json.dumps(entry, sort_keys=True))

    def subnet_update(self, family: Family, subnet: dict[str, Any]) -> None:
        """Send one ``subnet{v}-update`` of *subnet*, as ``SubnetDefinition.edited`` built it. It does not persist.

        ``subnet{v}-update`` replaces the whole Subnet, so *subnet* holds every field that the read returned.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_mutation_command(SUBNET_UPDATE[family], family, {f"subnet{family}": [subnet]})

    def lease_wipe(self, version: Family, subnet_id: int) -> None:
        """Delete all leases in a subnet using the ``lease{v}-wipe`` command.

        Requires the ``lease_cmds`` hook to be loaded on the Kea server.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID whose leases should be wiped.

        Raises:
            KeaException: If Kea returns a non-zero result code (including result=1
                when ``lease_cmds`` is not loaded).

        """
        self.command(LEASE_WIPE[version], version, arguments={"subnet-id": subnet_id})

    def lease_add(self, version: Family, lease: dict) -> None:
        """Create a new lease in the Kea lease database using ``lease{v}-add``.

        Args:
            version: DHCP version (4 or 6).
            lease: Full lease dict as expected by the Kea API. For v4, ``ip-address``
                is required. For v6, ``ip-address``, ``duid``, and ``iaid`` are required.

        Raises:
            KeaException: If Kea returns a non-zero result code (e.g. address already
                in use, subnet not found).

        """
        self.command(LEASE_ADD[version], version, arguments=lease)

    def lease_update(
        self,
        version: Family,
        ip_address: str,
        hostname: str | None = None,
        hw_address: str | None = None,
        valid_lft: int | None = None,
        duid: str | None = None,
    ) -> None:
        """Modify an existing lease in-place using ``lease{v}-update``.

        Fetches the current lease via ``lease{v}-get``, merges the provided
        non-None overrides, then posts the updated lease back.  No
        config-test/write cycle is needed because lease mutations go directly
        to Kea's live lease database.

        Args:
            version: DHCP version (4 or 6).
            ip_address: IP address of the lease to update.
            hostname: Optional new hostname.
            hw_address: Optional new hardware address (v4 only, ``xx:xx:...`` format).
            valid_lft: Optional new valid lifetime in seconds.
            duid: Optional new DUID (v6 only).

        Raises:
            KeaException: If the lease does not exist (result=3) or Kea returns
                an error for the update.

        """
        resp = self.command(LEASE_GET[version], version, arguments={"ip-address": ip_address})
        if resp[0]["result"] == 3:
            raise KeaException(resp[0])
        lease = resp[0]["arguments"]
        if not isinstance(lease, dict):
            raise ValueError(
                f"lease{version}-get returned result=0 but arguments is {type(lease).__name__}, expected dict"
            )
        if hostname is not None:
            lease["hostname"] = hostname
        if hw_address is not None:
            lease["hw-address"] = hw_address
        if valid_lft is not None:
            lease["valid-lft"] = valid_lft
        if duid is not None:
            lease["duid"] = duid
        self.command(LEASE_UPDATE[version], version, arguments=lease)

    def lease_get(self, identity: LeaseIdentity) -> ExactLeaseResult:
        """Read the one Lease with *identity*: found, confirmed absent, or a failed observation.

        Raises:
            KeaException: If Kea returns a result other than success or not found.
            MalformedLeaseResponse: If the reply envelope is unusable.

        """
        response = self.command(
            LEASE_GET[identity.family], identity.family, arguments=lookup_arguments(identity), check=(0, 3)
        )
        return read_exact_lease(response, identity)

    def lease_search(
        self,
        version: Family,
        selector: str,
        value: Any,
        *,
        state: int | None = None,
        server_id: int,
    ) -> LeaseSnapshot:
        """Return the Lease Snapshot of one supported selector; it covers the query scope, never the whole daemon.

        Raises:
            ValueError: If the selector, value or state does not fit the family.
            LeaseQueryGuardError: If a Subnet query is unsafe or cannot be measured.
            KeaException: If Kea returns a failure result.
            MalformedLeaseResponse: If the reply envelope is unusable.

        """
        selector_specs = {
            constants.BY_HW_ADDRESS: (LEASE_GET_BY_HW_ADDRESS, "hw-address"),
            constants.BY_HOSTNAME: (LEASE_GET_BY_HOSTNAME, "hostname"),
            constants.BY_CLIENT_ID: (LEASE_GET_BY_CLIENT_ID, "client-id"),
            constants.BY_DUID: (LEASE_GET_BY_DUID, "duid"),
        }
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if state is not None and selector not in (constants.BY_SUBNET, constants.BY_SUBNET_ID):
            raise ValueError("state can only be combined with a Subnet ID search.")
        started = _now()
        if selector == constants.BY_IP:
            return self._exact_lease_snapshot(version, value, started=started, server_id=server_id)
        if selector in (constants.BY_SUBNET, constants.BY_SUBNET_ID):
            if state is not None and (isinstance(state, bool) or state not in constants.LEASE_QUERY_STATE_CODES):
                raise LeaseQueryNotMeasurable(state)
            if selector == constants.BY_SUBNET:
                if not isinstance(value, str) or not value:
                    raise ValueError("subnet must be a non-empty CIDR string.")
                query = self._lease_query(version, selector, value, state)
                subnet_id = self.configured_subnet_id_from_cidr(version, value)
                if subnet_id is None:
                    raise LeaseQueryUnknownSubnet
            else:
                subnet_id = _subnet_id_value(value)
                query = self._lease_query(version, selector, subnet_id, state)
            command, arguments = self._subnet_lease_search_spec(version, subnet_id, state)
        else:
            spec = selector_specs.get(selector)
            if spec is None or version not in spec[0]:
                raise ValueError(f"Lease selector {selector!r} is not supported for DHCPv{version}.")
            if not isinstance(value, str) or not value:
                raise ValueError(f"{selector} must be a non-empty string.")
            query = self._lease_query(version, selector, value, None)
            command, arguments = spec[0][version], {spec[1]: value}

        response = self._lease_search_response(version, command, arguments)
        read = read_lease_collection(response, family=version)
        # Kea's statistics do not count every state, so the measured size can be smaller than the reply.
        subnet_query = query.selector in (constants.BY_SUBNET, constants.BY_SUBNET_ID)
        if subnet_query and self.max_unpaged_leases is not None and read.raw_count > self.max_unpaged_leases:
            raise LeaseQueryTooBroad(read.raw_count, self.max_unpaged_leases)
        return _lease_snapshot(server_id, query, started, read, coverage="exhaustive")

    @staticmethod
    def _lease_query(version: Family, selector: str, value: Any, state: int | None) -> LeaseQuery:
        return LeaseQuery(
            family=version,
            selector=selector,
            value=value,
            state=None if state is None else constants.LEASE_STATES[state],
        )

    def _exact_lease_snapshot(self, version: Family, value: Any, *, started: datetime, server_id: int) -> LeaseSnapshot:
        """Return the Snapshot of every allocation at one address: each kind is read on its own.

        A malformed record is a diagnostic, so absence is complete only when Kea confirms it for every kind.
        """
        if not isinstance(value, str) or not value:
            raise ValueError("ip must be a non-empty string.")
        results = [self.lease_get(identity) for identity in allocation_identities(version, value)]
        records = tuple(result.lease for result in results if isinstance(result, LeaseFound))
        diagnostics = tuple(
            diagnostic
            for result in results
            if isinstance(result, LeaseLookupFailed)
            for diagnostic in result.diagnostics
        )
        raw_count = sum(not isinstance(result, LeaseAbsent) for result in results)
        read = LeaseRead(
            family=version, records=records, diagnostics=diagnostics, raw_count=raw_count, next_cursor=None
        )
        query = LeaseQuery(family=version, selector=constants.BY_IP, value=str(ipaddress.ip_address(value)))
        return _lease_snapshot(server_id, query, started, read, coverage="exhaustive")

    def _lease_search_response(
        self,
        version: Family,
        command: KeaCommand,
        arguments: dict[str, Any],
    ) -> list[KeaResponse]:
        """Run one scoped lease query and fail closed if the state command is unavailable."""
        try:
            return self.command(command, version, arguments=arguments, check=(0, 3))
        except KeaException as exc:
            if command is not LEASE_GET_BY_STATE[version] or not exc.unsupported_command:
                raise
            raise LeaseQueryPreflightUnavailable("state-command") from exc

    def _subnet_lease_search_spec(
        self, version: Family, subnet_id: int, state: int | None
    ) -> tuple[KeaCommand, dict[str, Any]]:
        """Guard one Subnet lease query before selecting its command."""
        if self.max_unpaged_leases is None:
            if state is None:
                return LEASE_GET_ALL[version], {"subnets": [subnet_id]}
            return LEASE_GET_BY_STATE[version], {"subnet-id": subnet_id, "state": state}

        try:
            counts = self._subnet_lease_counts(version, subnet_id)
        except KeaException as exc:
            if exc.unsupported_command:
                raise LeaseQueryPreflightUnavailable from exc
            raise
        if state is None:
            observed_leases = counts.covered
            command = LEASE_GET_ALL[version]
            arguments = {"subnets": [subnet_id]}
        else:
            observed_leases = counts.active if state == constants.LEASE_STATE_CODES["assigned"] else counts.declined
            command = LEASE_GET_BY_STATE[version]
            arguments = {"subnet-id": subnet_id, "state": state}
        if observed_leases > self.max_unpaged_leases:
            raise LeaseQueryTooBroad(observed_leases, self.max_unpaged_leases)
        return command, arguments

    def _subnet_lease_counts(self, version: Family, subnet_id: int) -> _SubnetLeaseCounts:
        """Return the covered per-Subnet lease counts from ``stat_cmds``.

        Raises:
            LeaseQueryPreflightUnavailable: If Kea reports no statistics for the Subnet.
                The guard cannot size the query then, so the caller must fail closed
                rather than treat an unmeasured Subnet as an empty one.

        """
        command = STAT_LEASE_GET[version].value
        response = self.command(STAT_LEASE_GET[version], version, arguments={"subnet-id": subnet_id}, check=(0, 3))
        if not response or not isinstance(response[0], dict):
            raise RuntimeError(f"{command} returned a malformed response.")
        if response[0].get("result") == 3:
            raise LeaseQueryPreflightUnavailable
        arguments = response[0].get("arguments")
        result_set = arguments.get("result-set") if isinstance(arguments, dict) else None
        columns = result_set.get("columns") if isinstance(result_set, dict) else None
        rows = result_set.get("rows") if isinstance(result_set, dict) else None
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise RuntimeError(f"{command} returned malformed statistics.")

        count_columns = (
            ["assigned-addresses", "declined-addresses"]
            if version == 4
            else ["assigned-nas", "declined-addresses", "assigned-pds"]
        )
        try:
            subnet_index = columns.index("subnet-id")
            count_indexes = [columns.index(name) for name in count_columns]
        except ValueError as exc:
            raise RuntimeError(f"{command} omitted required statistics columns.") from exc

        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) <= max(subnet_index, *count_indexes):
                raise RuntimeError(f"{command} returned a malformed statistics row.")
            if row[subnet_index] != subnet_id:
                continue
            values = [row[index] for index in count_indexes]
            if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in values):
                raise RuntimeError(f"{command} returned an invalid lease count.")
            assigned, declined, *assigned_pds = values
            if assigned < declined:
                raise RuntimeError(f"{command} returned inconsistent lease counts.")
            delegated = assigned_pds[0] if assigned_pds else 0
            return _SubnetLeaseCounts(
                covered=assigned + delegated,
                active=assigned - declined + delegated,
                declined=declined,
            )
        # Kea knows no statistics for this Subnet, so the guard has nothing to size the
        # query with. Report it as unavailable instead of reading it as zero leases.
        raise LeaseQueryPreflightUnavailable

    def lease_get_page(
        self,
        version: Family,
        *,
        limit: int,
        cursor: str | None = None,
        server_id: int,
    ) -> LeaseSnapshot:
        """Return one validated page of every Lease of the family, after *cursor* or from the start.

        Raises:
            ValueError: If *limit* or *cursor* is not valid for the family.
            KeaException: If Kea returns a failure result.
            MalformedLeaseResponse: If the envelope, count or continuation is unusable.

        """
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        after = _lease_cursor(version, cursor)
        started = _now()
        read = self._lease_page(version, limit=limit, after=after)
        coverage: LeaseCoverage = "exhaustive" if after is None and read.next_cursor is None else "page"
        return _lease_snapshot(server_id, LeaseQuery(family=version, selector=ALL_LEASES), started, read, coverage)

    def _lease_page(self, version: Family, *, limit: int, after: IPAddressValue | None) -> LeaseRead:
        """Request and read one ``lease{v}-get-page`` reply."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        start = str(after) if after is not None else ("0.0.0.0" if version == 4 else "::")  # noqa: S104  page cursor
        response = self.command(
            LEASE_GET_PAGE[version], version, arguments={"from": start, "limit": limit}, check=(0, 3)
        )
        return read_lease_page(response, family=version, limit=limit, after=after)

    def _lease_page_count(self, version: Family, *, after: IPAddressValue) -> int:
        """Return how many raw records follow *after*, at most one; a record counts whatever its shape."""
        response = self.command(
            LEASE_GET_PAGE[version], version, arguments={"from": str(after), "limit": 1}, check=(0, 3)
        )
        return read_lease_page_count(response, limit=1)

    def lease_get_all(
        self, version: Family, *, per_page: int = 250, max_leases: int | None = None, server_id: int
    ) -> LeaseSnapshot:
        """Return a bounded Snapshot of every Lease of the family, read page by page.

        Pages continue from the last raw record, so a page whose every record is excluded still continues.
        *max_leases* caps the raw records read; a Snapshot that reaches the cap before Kea proves the end
        has ``page`` coverage and keeps the continuation.

        Raises:
            ValueError: If *per_page* or *max_leases* is not a positive integer.
            KeaException: If Kea returns a failure result.
            MalformedLeaseResponse: If a page envelope, count or continuation is unusable, or a page does not advance.

        """
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if isinstance(per_page, bool) or not isinstance(per_page, int) or per_page < 1:
            raise ValueError(f"per_page must be >= 1, got {per_page!r}")
        if max_leases is not None and (
            isinstance(max_leases, bool) or not isinstance(max_leases, int) or max_leases < 1
        ):
            raise ValueError(f"max_leases must be >= 1 (or None for no cap), got {max_leases!r}")
        started = _now()
        records: list[Any] = []
        diagnostics: list[LeaseDiagnostic] = []
        raw_count = 0
        cursor: IPAddressValue | None = None
        for page in itertools.count():
            limit = per_page if max_leases is None else min(per_page, max_leases - raw_count)
            read = self._lease_page(version, limit=limit, after=cursor)
            records.extend(read.records)
            diagnostics.extend(
                diagnostic.model_copy(update={"source_position": f"pages[{page}].{diagnostic.source_position}"})
                for diagnostic in read.diagnostics
            )
            raw_count += read.raw_count
            cursor = read.next_cursor
            if cursor is not None and max_leases is not None and raw_count >= max_leases:
                # The cap is reached: one more raw record decides whether the end is proven.
                if self._lease_page_count(version, after=cursor) == 0:
                    cursor = None
                break
            if cursor is None:
                break
        read = LeaseRead(
            family=version,
            records=tuple(records),
            diagnostics=tuple(diagnostics),
            raw_count=raw_count,
            next_cursor=cursor,
        )
        coverage: LeaseCoverage = "exhaustive" if cursor is None else "page"
        return _lease_snapshot(server_id, LeaseQuery(family=version, selector=ALL_LEASES), started, read, coverage)

    def dhcp_disable(self, family: Family, max_period: int | None = None) -> None:
        """Temporarily disable DHCP processing on the daemon of *family*.

        The daemon continues running but stops responding to DHCP requests.
        Pass *max_period* (in seconds) to automatically re-enable after that time;
        omit it to keep the service disabled until :meth:`dhcp_enable` is called.

        Args:
            family: The DHCP family whose daemon stops processing.
            max_period: Optional number of seconds before the service auto-re-enables.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        arguments: dict[str, Any] | None = None
        if max_period is not None:
            arguments = {"max-period": max_period}
        self.command(KeaCommand.DHCP_DISABLE, family, arguments=arguments)

    def dhcp_enable(self, family: Family) -> None:
        """Re-enable DHCP processing on the daemon of *family* after a :meth:`dhcp_disable` call.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        self.command(KeaCommand.DHCP_ENABLE, family)

    def pool_change(self, version: Family, action: PoolAction, subnet_id: int, declared_cidr: str, pool: str) -> None:
        """Send one ``subnet{v}-delta-{action}`` for the Pool of the Subnet. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        commands = SUBNET_DELTA_ADD if action == "add" else SUBNET_DELTA_DEL
        subnet = {"id": subnet_id, "subnet": declared_cidr, "pools": [{"pool": pool}]}
        self._config_mutation_command(commands[version], version, {f"subnet{version}": [subnet]})

    def _one_command(
        self,
        command: KeaCommand,
        family: Family,
        arguments: dict[str, Any] | None = None,
        ok_codes: Sequence[int] = (0,),
    ) -> KeaResponse:
        """Send one single-service command and return its one well-formed reply."""
        return _one_reply(command, family, self.command(command, family, arguments=arguments, check=None), ok_codes)

    def config_candidate(self, version: Family) -> CandidateConfiguration:
        """Send one ``config-get`` and return the running configuration, for a read-modify-write.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.
            MalformedConfiguration: If the ``Dhcp{v}`` block is not an object.

        """
        reply = self._one_command(KeaCommand.CONFIG_GET, version)
        arguments = reply.get("arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError(f"config-get returned no configuration object for dhcp{version}.")
        # Kea 2.4+ adds "hash"; config-test and config-set reject it.
        arguments.pop("hash", None)
        return CandidateConfiguration(version, arguments)

    def config_test(self, candidate: CandidateConfiguration) -> None:
        """Send one ``config-test`` of *candidate*. It changes nothing.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._one_command(KeaCommand.CONFIG_TEST, candidate.family, candidate.arguments)

    def config_set(self, candidate: CandidateConfiguration) -> None:
        """Send one ``config-set`` of *candidate*. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_mutation_command(KeaCommand.CONFIG_SET, candidate.family, candidate.arguments)

    def persist(self, version: Family) -> PersistResult:
        """Write the running configuration to disk: ``config-get``, ``config-test`` of it, then ``config-write``.

        It never raises. ``failed`` means that NetBox cannot confirm that the disk copy holds the running
        configuration. A failed phase stops the step, so NetBox never writes a configuration it could not test.
        """
        if not self.persist_config:
            return PersistResult("not-requested")
        service = f"dhcp{version}"
        # OSError covers each requests error, KeaTLSFileError included.
        try:
            candidate = self.config_candidate(version)
        except (KeaException, OSError, ValueError, RuntimeError):
            logger.warning("config-get failed for %s, so config-write was not sent", service, exc_info=True)
            return PersistResult("failed", ("Kea did not return its running configuration, so it was not saved.",))
        try:
            self.config_test(candidate)
        except KeaException as exc:
            if not exc.unsupported_command:
                logger.warning("config-test rejected the running configuration of %s: %s", service, exc)
                return PersistResult("failed", (f"config-test rejected the running configuration: {exc.reply_text}",))
        except (OSError, ValueError, RuntimeError):
            logger.warning("config-test failed for %s, so config-write was not sent", service, exc_info=True)
            return PersistResult("failed", ("config-test did not return a usable reply, so nothing was saved.",))
        try:
            self._one_command(KeaCommand.CONFIG_WRITE, version)
        except KeaException as exc:
            logger.warning("config-write failed for %s: %s", service, exc)
            return PersistResult("failed", (f"config-write failed: {exc.reply_text}",))
        except (OSError, ValueError, RuntimeError):
            logger.warning("The config-write reply of %s was lost or unreadable", service, exc_info=True)
            return PersistResult("failed", ("The reply to config-write was lost or unreadable.",))
        return PersistResult("persisted")

    def subnet_get(self, version: Family, subnet_id: int) -> dict:
        """Fetch the full subnet config dict for *subnet_id* from Kea.

        The complete subnet object (id, subnet, pools, option-data, relay, allocator, ...)
        enables a read-modify-write cycle without losing live-only fields.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to look up.

        Returns:
            A shallow copy of the full subnet dict (nested structures like pools
            and option-data are not deep-copied — callers must not mutate nested
            lists/dicts in place) as returned by Kea.

        Raises:
            KeaException: If the subnet is not found or Kea returns an error.

        """
        subnet_key = f"subnet{version}"
        resp = self.command(SUBNET_GET[version], version, arguments={"id": subnet_id})
        if not isinstance(resp, list) or not resp or not isinstance(resp[0], dict):
            raise RuntimeError(f"subnet{version}-get returned an invalid response envelope")
        args = resp[0].get("arguments") or {}
        if not isinstance(args, dict) or not isinstance(args.get(subnet_key, []), list):
            raise RuntimeError(f"subnet{version}-get returned an invalid subnet collection")
        subnets = args.get(subnet_key, [])
        if not subnets:
            raise KeaException(
                {"result": 3, "text": f"subnet{version}-get returned no subnet for id={subnet_id}", "arguments": None},
                index=0,
            )
        if not isinstance(subnets[0], dict):
            raise RuntimeError(f"subnet{version}-get returned an invalid subnet")
        return dict(subnets[0])


class KeaException(Exception):
    """Raised when a Kea API response contains an unexpected result code."""

    def __init__(self, resp: KeaResponse, msg: str | None = None, index: int | None = None) -> None:
        """Initialise with the failing response and optional context."""
        self.index = index
        self.response = resp

        if msg is None:
            msg = f"Kea returned result[{index}] {self.response.get('result')}"
        message = f"{msg}: {self.response.get('text')}"
        super().__init__(message)

    @property
    def unsupported_command(self) -> bool:
        """Return whether Kea rejected an unsupported command."""
        return self.response.get("result") == 2

    @property
    def reply_text(self) -> str:
        """Return the text of Kea's failure reply, or its result code when the reply has no text."""
        text = self.response.get("text")
        return text if isinstance(text, str) and text else f"result {self.response.get('result')}"


class KeaTLSFileError(requests.exceptions.RequestException):
    """Raised when requests cannot find a TLS CA, certificate or key file of the client. Nothing was sent.

    requests raises a plain ``OSError`` for this case. ``KeaClient.command`` raises this subclass of
    ``RequestException`` instead, so each handler of request errors also handles it. The ``OSError`` is the cause.
    """


def _one_reply(
    command: KeaCommand, family: Family, response: list[KeaResponse], ok_codes: Sequence[int] = (0,)
) -> KeaResponse:
    """Return the one reply of a single-service command, or raise when it is malformed or its result is not ok."""
    if (
        len(response) != 1
        or not isinstance(response[0], dict)
        or not isinstance(response[0].get("result"), int)
        or isinstance(response[0].get("result"), bool)
    ):
        raise RuntimeError(f"{command.value} did not return one valid result for dhcp{family}.")
    check_response(response, ok_codes)
    return response[0]


def check_response(resp: list[KeaResponse], ok_codes: Sequence[int]) -> None:
    """Raise a KeaException for any non 0 responses.

    Raises:
        RuntimeError: If an entry is not a dict or has no ``result``. Reading
            ``kr["result"]`` unguarded would raise TypeError/KeyError instead,
            which no caller catches, so a malformed payload became an HTTP 500.
        KeaException: If a result code is not in *ok_codes*.

    """
    for idx, kr in enumerate(resp):
        if not isinstance(kr, dict) or "result" not in kr:
            raise RuntimeError(f"Kea returned a malformed response entry at index {idx}: {kr!r}")
        if kr["result"] not in ok_codes:
            raise KeaException(kr, index=idx)
