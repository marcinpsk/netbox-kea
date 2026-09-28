import base64
import ipaddress
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, TypedDict, cast

import requests
from requests.models import HTTPBasicAuth

from . import constants
from .constants import Family, IPNetworkValue, Persistence
from .dhcp_options import (
    DHCPOption,
    FormManagedOption,
    form_managed_options,
    merge_option_form_rows,
    parse_dhcp_options,
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


class LeasePage(NamedTuple):
    """One validated Kea lease page and its next cursor."""

    leases: list[dict[str, Any]]
    next_cursor: str | None


class LeaseCollection(NamedTuple):
    """A bounded validated lease collection."""

    leases: list[dict[str, Any]]
    truncated: bool


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


def lease_query_guard_message(exc: LeaseQueryGuardError, state: int | None) -> str:
    """Return safe, actionable guidance for one rejected lease query."""
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


def _lease_page_start(version: int, cursor: str | None) -> str:
    """Return the validated Kea page cursor for one DHCP family."""
    if cursor is not None:
        try:
            parsed_cursor = ipaddress.ip_address(cursor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid DHCPv{version} lease cursor.") from exc
        if parsed_cursor.version != version:
            raise ValueError(f"Lease cursor family IPv{parsed_cursor.version} does not match DHCPv{version}.")
        return str(parsed_cursor)
    return "0.0.0.0" if version == 4 else "::"  # noqa: S104  Kea pagination cursor


def _lease_address_values(leases: list[Any], command: str) -> list[str]:
    """Return non-empty lease address strings or reject a malformed page."""
    values: list[str] = []
    for index, lease in enumerate(leases):
        raw_address = lease.get("ip-address") if isinstance(lease, dict) else None
        if not isinstance(raw_address, str) or not raw_address:
            raise RuntimeError(f"{command} returned an invalid ip-address at lease index {index}.")
        values.append(raw_address)
    return values


def _validated_lease_addresses(
    leases: list[Any],
    version: int,
    command: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return parsed lease addresses that match the requested DHCP family."""
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for index, raw_address in enumerate(_lease_address_values(leases, command)):
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise RuntimeError(f"{command} returned an invalid ip-address at lease index {index}.") from exc
        if address.version != version:
            raise RuntimeError(f"{command} returned a lease for the wrong address family at index {index}.")
        addresses.append(address)
    return addresses


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


def _managed_option_matcher(version: int, managed: FormManagedOption) -> Callable[[dict[str, Any]], bool]:
    """Return a predicate for one managed option in the family's default space."""

    def matches(option: dict[str, Any]) -> bool:
        if option.get("space") not in (None, f"dhcp{version}") or option.get("client-classes"):
            return False
        if option.get("code") is not None:
            return option.get("code") == managed.code
        return option.get("name") == managed.name

    return matches


def _replace_managed_option(
    options: list[dict[str, Any]],
    version: int,
    field: str,
    data: str | None,
    *,
    single_value: bool = False,
) -> list[dict[str, Any]]:
    """Set one form-managed option to *data*, or remove it when *data* is empty.

    The form edits the value only. Delivery flags stay as they are. An entry the
    form cannot show (empty data, binary-encoded, or a list where the
    form holds one value) is kept when the field is empty. Form text is CSV, so a
    new value drops a csv-format flag that described the old encoding.
    """
    if data is None:
        return options
    managed = form_managed_options(version)[field]
    is_managed = _managed_option_matcher(version, managed)
    existing = next((option for option in options if is_managed(option)), None)
    kept = [option for option in options if not is_managed(option)]
    if data:
        replacement = dict(existing) if existing else {"name": managed.name}
        old_data = replacement.get("data")
        unchanged = (
            isinstance(old_data, str)
            and replacement.get("csv-format") is not False
            and [value.strip() for value in data.split(",")] == [value.strip() for value in old_data.split(",")]
        )
        if not unchanged:
            replacement.pop("csv-format", None)
            replacement["data"] = data
        return [*kept, replacement]
    if existing is not None and (
        not existing.get("data")
        or existing.get("csv-format") is False
        or (single_value and "," in str(existing.get("data", "")))
    ):
        return [*kept, existing]
    return kept


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


def _set_shared_network_description(network: dict[str, Any], description: str) -> None:
    """Write *description* as ``user-context.comment`` and keep every other user-context key.

    An empty *description* keeps a comment the form cannot show, the same as a binary option.
    A *description* equal to the comment as a text input shows it keeps the comment unchanged.
    """
    try:
        existing = shared_network_description(network)
    except ValueError as exc:
        raise MalformedConfiguration(str(exc)) from exc
    if existing is not None and description == existing.replace("\r", "").replace("\n", "").strip():
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
    """The Shared Network fields that the edit form manages. An empty value removes the field."""

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

    A lifetime or a timer of None keeps the live value.
    """

    fields: SubnetFields
    valid_lifetime: int | None
    min_valid_lifetime: int | None
    max_valid_lifetime: int | None
    renew_timer: int | None
    rebind_timer: int | None


@dataclass(frozen=True)
class SubnetDefinition:
    """One Subnet as ``subnet{v}-get`` returned it. Two reads are equal when Kea returned the same Subnet."""

    family: Family
    subnet_id: int
    network: IPNetworkValue
    # The reply entry as sorted JSON text, so that the value is immutable and compares by content.
    entry: str

    def edited(self, edit: SubnetEdit) -> dict[str, Any]:
        """Return the Subnet with the fields of *edit*, and every other field that Kea returned."""
        subnet = json.loads(self.entry)
        # Kea adds a read-only metadata key to some replies, so the update does not send it back.
        subnet.pop("metadata", None)
        fields = edit.fields
        options = subnet.get("option-data", [])
        if self.family == 4:
            options = _replace_managed_option(options, 4, "gateway", fields.gateway, single_value=True)
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
        """Return each live Pool entry by its range text, so that a kept Pool keeps its Pool-level fields."""
        by_range: dict[str, dict[str, Any]] = {}
        for entry in pools:
            if not isinstance(entry, dict):
                continue
            try:
                by_range[parse_pool(entry.get("pool"), self.network).range] = entry
            except ValueError:
                continue
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
        timeout: int = 30,
        persist_config: bool = True,
        send_service: bool = True,
        max_unpaged_leases: int | None = 1000,
        on_config_change: Callable[[], None] | None = None,
    ):
        """Initialise a Kea HTTP client session.

        Args:
            url: Base URL of the Kea Control Agent or DHCP daemon endpoint.
            username: Optional HTTP Basic Auth username.
            password: Optional HTTP Basic Auth password.
            verify: SSL verification — True/False or path to a CA bundle.
            client_cert: Path to client certificate for mutual TLS.
            client_key: Path to private key matching client_cert.
            timeout: Request timeout in seconds.
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
                more than this many covered leases. ``None`` disables the guard.
            on_config_change: Optional callback invoked after Kea's live configuration
                changes. Cache invalidation failures are logged and do not interrupt
                persistence of an already-applied change.

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

        self._session = requests.Session()
        if verify is not None:
            self._session.verify = verify
        if username is not None and password is not None:
            self._session.auth = HTTPBasicAuth(username, password)
        if client_cert is not None and client_key is not None:
            self._session.cert = (client_cert, client_key)

    def command(
        self,
        command: str,
        service: list[str] | None = None,
        arguments: dict[str, Any] | None = None,
        check: Sequence[int] | None = (0,),
    ) -> list[KeaResponse]:
        """Send a command to the Kea API and return the response list.

        Args:
            command: Kea command name (e.g. ``"lease4-get-all"``).
            service: List of target services (e.g. ``["dhcp4"]``). Omit for CA-level commands.
                Dropped from the request body when the client targets a DHCP daemon
                directly (``send_service=False``) — see :meth:`__init__`.
            arguments: Optional command arguments payload.
            check: Sequence of acceptable result codes. Pass ``None`` to skip checking.

        Returns:
            Parsed JSON response as a list of KeaResponse dicts.

        Raises:
            requests.HTTPError: If the HTTP response status is not 2xx.
            KeaException: If any response result code is not in *check*.

        """
        body: dict[str, Any] = {"command": command}

        # A direct daemon connection must not carry ``service``: Kea 3.2.0+ rejects a
        # non-matching service, and callers pass a version-matched singleton that is
        # redundant when the URL already targets that one daemon.
        if service is not None and self.send_service:
            body["service"] = service

        if arguments is not None:
            body["arguments"] = arguments

        resp = self._session.post(self.url, json=body, timeout=self.timeout)
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
        new._session = requests.Session()
        new._session.auth = self._session.auth
        new._session.verify = self._session.verify
        new._session.cert = self._session.cert
        new.persist_config = self.persist_config
        new.send_service = self.send_service
        new.max_unpaged_leases = self.max_unpaged_leases
        new._on_config_change = self._on_config_change
        return new

    def _notify_config_change(self, service: str) -> None:
        """Notify the owner without interrupting persistence of a live change."""
        if self._on_config_change is None:
            return
        try:
            self._on_config_change()
        except Exception:
            logger.exception("Configuration changed for %s, but cache invalidation failed", service)

    def _config_mutation_command(
        self,
        command: str,
        service: str,
        arguments: dict[str, Any],
        *,
        check: Sequence[int] | None = (0,),
    ) -> list[KeaResponse]:
        """Send one live configuration mutation between invalidation notifications."""
        self._notify_config_change(service)
        try:
            return self.command(command, service=[service], arguments=arguments, check=check)
        finally:
            # The response can be lost after Kea applies the command. Invalidate again
            # so a read during the request cannot repopulate the active cache generation.
            self._notify_config_change(service)

    def close(self) -> None:
        """Close the underlying requests.Session and release connection resources."""
        self._session.close()

    def __enter__(self) -> "KeaClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_available_commands(self, service: str) -> set[str]:
        """Return the set of commands available on *service* (e.g. ``"dhcp4"``).

        Args:
            service: Kea service name to query (``"dhcp4"`` or ``"dhcp6"``).

        Returns:
            Set of command name strings reported by ``list-commands``.

        """
        resp = self.command("list-commands", service=[service])
        if not resp or not isinstance(resp[0], dict):
            raise RuntimeError(f"list-commands returned malformed response: {resp!r}")
        arguments = resp[0].get("arguments")
        if not isinstance(arguments, list) or any(not isinstance(command, str) for command in arguments):
            raise RuntimeError(f"list-commands returned malformed arguments: {resp[0]!r}")
        return set(arguments)

    def reservation_capabilities(self, version: int) -> ReservationCapabilities:
        """Read live identifier configuration and host command availability."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        service = f"dhcp{version}"
        commands = self.get_available_commands(service)
        response = self.command("config-get", service=[service])
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
        required_commands = {"reservation-get", "reservation-add", "reservation-update", "reservation-del"}
        missing_commands = required_commands - commands
        mutation_available = bool(available) and not missing_commands
        if missing_commands:
            explanation = "The host_cmds hook does not provide all required Reservation commands."
        elif not available:
            explanation = "No supported Reservation identifier is enabled."
        else:
            explanation = ""
        return ReservationCapabilities(
            family=cast(Family, version),
            identifiers=available,
            mutation_available=mutation_available,
            explanation=explanation,
            unavailable_identifiers=unavailable,
        )

    def _reservation_raw_page(
        self,
        service: str,
        source_index: int = 0,
        from_index: int = 0,
        limit: int = 100,
        subnet_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch a page of host reservations from Kea.

        Args:
            service: Target service (``"dhcp4"`` or ``"dhcp6"``).
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
            "reservation-get-page",
            service=[service],
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
        version: int,
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
                f"dhcp{version}",
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
        version: int,
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
        version: int,
        scope: ReservationScope,
        identity: ReservationIdentity,
    ) -> dict[str, Any] | None:
        """Fetch one exact raw Reservation for private read-modify-write use."""
        subnet_id = _reservation_scope_subnet_id(scope)
        response = self.command(
            "reservation-get",
            service=[f"dhcp{version}"],
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
        version: int,
        scope: InSubnetReservationScope,
        address: str,
    ) -> dict[str, Any] | None:
        """Fetch one scoped raw Reservation by allocation address."""
        response = self.command(
            "reservation-get",
            service=[f"dhcp{version}"],
            arguments={"subnet-id": scope.subnet.subnet_id, "ip-address": address},
            check=(0, 3),
        )
        return _reservation_get_arguments(response)

    def reservation_by_address(
        self,
        version: int,
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
        version: int,
        catalogue,
        hostname: str,
    ) -> ReservationSnapshot:
        """Return the typed Reservations that match one exact hostname."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if not isinstance(hostname, str) or not hostname:
            raise ValueError("hostname must be a non-empty string.")
        response = self.command(
            "reservation-get-by-hostname",
            service=[f"dhcp{version}"],
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
        version: int,
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
            family=cast(Family, version),
            records=tuple(records),
            diagnostics=tuple(diagnostics),
            complete=not diagnostics,
            next_cursor=None,
        )

    def _reservation_mutation_command(self, command: str, version: int, arguments: dict[str, Any]) -> None:
        """Apply one Reservation command and validate its success envelope."""
        response = self.command(command, service=[f"dhcp{version}"], arguments=arguments)
        if not response or not isinstance(response[0], dict) or response[0].get("result") != 0:
            raise RuntimeError(f"{command} returned a malformed success response.")

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
            "reservation-add",
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
            "reservation-update",
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
            "reservation-del",
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

    def configured_subnet_id_from_cidr(self, version: int, cidr: str) -> int | None:
        """Resolve a CIDR from the running config without requiring ``subnet_cmds``."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        try:
            network = ipaddress.ip_network(cidr, strict=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"subnet must be a valid IPv{version} CIDR.") from exc
        if network.version != version:
            raise ValueError(f"Subnet family IPv{network.version} does not match DHCPv{version}.")

        response = self.command("config-get", service=[f"dhcp{version}"])
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
        command = f"subnet{version}-add"
        service = f"dhcp{version}"
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
        response = self._config_mutation_command(command, service, {f"subnet{version}": [subnet]}, check=None)
        _one_reply(command, service, response)

    def subnet_del(self, version: Family, subnet_id: int) -> None:
        """Send one ``subnet{v}-del``. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        command = f"subnet{version}-del"
        service = f"dhcp{version}"
        response = self._config_mutation_command(command, service, {"id": subnet_id}, check=None)
        _one_reply(command, service, response)

    def shared_network_exists(self, version: Family, name: str) -> bool:
        """Return whether the daemon has a Shared Network named *name*.

        Raises:
            KeaException: If Kea returns a result other than 0 (found) or 3 (not found).
            RuntimeError: If the reply is malformed or names another Shared Network.

        """
        command = f"network{version}-get"
        response = self.command(command, service=[f"dhcp{version}"], arguments={"name": name}, check=None)
        reply = _one_reply(command, f"dhcp{version}", response, (0, 3))
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
            raise RuntimeError(f"{command} returned a malformed Shared Network for {name!r}.")
        return True

    def network_add(self, version: Family, name: str) -> None:
        """Send one ``network{v}-add`` for an empty Shared Network. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        command = f"network{version}-add"
        service = f"dhcp{version}"
        response = self._config_mutation_command(command, service, {"shared-networks": [{"name": name}]}, check=None)
        _one_reply(command, service, response)

    def network_del(self, version: Family, name: str) -> None:
        """Send one ``network{v}-del``. Member Subnets stay and leave the Shared Network. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        command = f"network{version}-del"
        service = f"dhcp{version}"
        response = self._config_mutation_command(command, service, {"name": name}, check=None)
        _one_reply(command, service, response)

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
        command = f"network{version}-subnet-add"
        service = f"dhcp{version}"
        response = self._config_mutation_command(command, service, {"name": name, "id": subnet_id}, check=None)
        _one_reply(command, service, response)

    def network_subnet_del(self, version: Family, name: str, subnet_id: int) -> None:
        """Send one ``network{v}-subnet-del``. The Subnet stays, outside any Shared Network. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        command = f"network{version}-subnet-del"
        service = f"dhcp{version}"
        response = self._config_mutation_command(command, service, {"name": name, "id": subnet_id}, check=None)
        _one_reply(command, service, response)

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

    def subnet_update(self, definition: SubnetDefinition, edit: SubnetEdit) -> None:
        """Send one ``subnet{v}-update`` of *definition* with the fields of *edit*. It does not persist.

        ``subnet{v}-update`` replaces the whole Subnet, so the update sends every field that the read returned.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        command = f"subnet{definition.family}-update"
        service = f"dhcp{definition.family}"
        arguments = {f"subnet{definition.family}": [definition.edited(edit)]}
        response = self._config_mutation_command(command, service, arguments, check=None)
        _one_reply(command, service, response)

    def lease_wipe(self, version: int, subnet_id: int) -> None:
        """Delete all leases in a subnet using the ``lease{v}-wipe`` command.

        Requires the ``lease_cmds`` hook to be loaded on the Kea server.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID whose leases should be wiped.

        Raises:
            KeaException: If Kea returns a non-zero result code (including result=1
                when ``lease_cmds`` is not loaded).

        """
        self.command(
            f"lease{version}-wipe",
            service=[f"dhcp{version}"],
            arguments={"subnet-id": subnet_id},
        )

    def lease_add(self, version: int, lease: dict) -> None:
        """Create a new lease in the Kea lease database using ``lease{v}-add``.

        Args:
            version: DHCP version (4 or 6).
            lease: Full lease dict as expected by the Kea API. For v4, ``ip-address``
                is required. For v6, ``ip-address``, ``duid``, and ``iaid`` are required.

        Raises:
            KeaException: If Kea returns a non-zero result code (e.g. address already
                in use, subnet not found).

        """
        self.command(
            f"lease{version}-add",
            service=[f"dhcp{version}"],
            arguments=lease,
        )

    def lease_update(
        self,
        version: int,
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
        service = f"dhcp{version}"
        resp = self.command(
            f"lease{version}-get",
            service=[service],
            arguments={"ip-address": ip_address},
        )
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
        self.command(
            f"lease{version}-update",
            service=[service],
            arguments=lease,
        )

    def lease_get_by_ip(self, version: int, ip_address: str) -> dict | None:
        """Fetch a single lease through the canonical lease-search interface.

        Args:
            version: DHCP version (4 or 6).
            ip_address: IP address to look up.

        Returns:
            The first lease returned by :meth:`lease_search`, or ``None`` when no lease matches.

        Raises:
            ValueError: If the DHCP version is invalid or the address value is empty.
            KeaException: If the Kea lease search fails.
            RuntimeError: If Kea returns a malformed lease response.

        """
        leases = self.lease_search(version, constants.BY_IP, ip_address)
        return leases[0] if leases else None

    def lease_search(
        self,
        version: int,
        selector: str,
        value: Any,
        *,
        state: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return raw leases that match one supported selector."""
        selector_specs = {
            constants.BY_IP: ("", "ip-address", False, {4, 6}),
            constants.BY_HW_ADDRESS: ("-by-hw-address", "hw-address", True, {4}),
            constants.BY_HOSTNAME: ("-by-hostname", "hostname", True, {4, 6}),
            constants.BY_CLIENT_ID: ("-by-client-id", "client-id", True, {4}),
            constants.BY_SUBNET: ("-all", "subnets", True, {4, 6}),
            constants.BY_SUBNET_ID: ("-all", "subnets", True, {4, 6}),
            constants.BY_DUID: ("-by-duid", "duid", True, {6}),
        }
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        spec = selector_specs.get(selector)
        if spec is None or version not in spec[3]:
            raise ValueError(f"Lease selector {selector!r} is not supported for DHCPv{version}.")

        command_suffix, argument_name, multiple, _supported_versions = spec
        if selector in (constants.BY_SUBNET, constants.BY_SUBNET_ID):
            if selector == constants.BY_SUBNET:
                if not isinstance(value, str) or not value:
                    raise ValueError("subnet must be a non-empty CIDR string.")
                subnet_id = self.configured_subnet_id_from_cidr(version, value)
                if subnet_id is None:
                    return []
                value = subnet_id
            command_suffix, arguments = self._subnet_lease_search_spec(version, value, state)
        else:
            if state is not None:
                raise ValueError("state can only be combined with a Subnet ID search.")
            if not isinstance(value, str) or not value:
                raise ValueError(f"{selector} must be a non-empty string.")
            arguments = {argument_name: value}

        command, response = self._lease_search_response(
            version,
            command_suffix,
            arguments,
        )
        if not response or not isinstance(response[0], dict):
            raise RuntimeError(f"{command} returned a malformed response.")
        if response[0].get("result") == 3:
            return []
        response_arguments = response[0].get("arguments")
        if not isinstance(response_arguments, dict):
            raise RuntimeError(f"{command} returned malformed arguments.")
        raw_leases = response_arguments.get("leases") if multiple else [response_arguments]
        if not isinstance(raw_leases, list):
            raise RuntimeError(f"{command} returned a malformed leases collection.")
        _validated_lease_addresses(raw_leases, version, command)
        return raw_leases

    def _lease_search_response(
        self,
        version: int,
        command_suffix: str,
        arguments: dict[str, Any],
    ) -> tuple[str, list[KeaResponse]]:
        """Run one scoped lease query and fail closed if the state command is unavailable."""
        command = f"lease{version}-get{command_suffix}"
        try:
            response = self.command(
                command,
                service=[f"dhcp{version}"],
                arguments=arguments,
                check=(0, 3),
            )
        except KeaException as exc:
            if command_suffix != "-by-state" or not exc.unsupported_command:
                raise
            raise LeaseQueryPreflightUnavailable("state-command") from exc
        return command, response

    def _subnet_lease_search_spec(self, version: int, value: Any, state: int | None) -> tuple[str, dict[str, Any]]:
        """Validate and guard one Subnet lease query before selecting its command."""
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("subnet_id must be a positive integer.")
        try:
            subnet_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("subnet_id must be a positive integer.") from exc
        if subnet_id < 1:
            raise ValueError("subnet_id must be a positive integer.")
        if state is not None and (isinstance(state, bool) or state not in (0, 1)):
            raise LeaseQueryNotMeasurable(state)
        if self.max_unpaged_leases is None:
            if state is None:
                return "-all", {"subnets": [subnet_id]}
            return "-by-state", {"subnet-id": subnet_id, "state": state}

        try:
            counts = self._subnet_lease_counts(version, subnet_id)
        except KeaException as exc:
            if exc.unsupported_command:
                raise LeaseQueryPreflightUnavailable from exc
            raise
        if state is None:
            observed_leases = counts.covered
            command_suffix = "-all"
            arguments = {"subnets": [subnet_id]}
        else:
            observed_leases = counts.active if state == 0 else counts.declined
            command_suffix = "-by-state"
            arguments = {"subnet-id": subnet_id, "state": state}
        if observed_leases > self.max_unpaged_leases:
            raise LeaseQueryTooBroad(observed_leases, self.max_unpaged_leases)
        return command_suffix, arguments

    def _subnet_lease_counts(self, version: int, subnet_id: int) -> _SubnetLeaseCounts:
        """Return the covered per-Subnet lease counts from ``stat_cmds``.

        Raises:
            LeaseQueryPreflightUnavailable: If Kea reports no statistics for the Subnet.
                The guard cannot size the query then, so the caller must fail closed
                rather than treat an unmeasured Subnet as an empty one.

        """
        command = f"stat-lease{version}-get"
        response = self.command(
            command,
            service=[f"dhcp{version}"],
            arguments={"subnet-id": subnet_id},
            check=(0, 3),
        )
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
        version: int,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> LeasePage:
        """Return one validated global lease page."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")

        return self._request_lease_page(
            version,
            limit=limit,
            cursor=_lease_page_start(version, cursor),
        )

    def _request_lease_page(self, version: int, *, limit: int, cursor: str) -> LeasePage:
        """Request one structurally valid lease page from Kea."""
        command = f"lease{version}-get-page"
        response = self.command(
            command,
            service=[f"dhcp{version}"],
            arguments={"from": cursor, "limit": limit},
            check=(0, 3),
        )
        if not response or not isinstance(response[0], dict):
            raise RuntimeError(f"{command} returned a malformed response.")
        if response[0].get("result") == 3:
            return LeasePage(leases=[], next_cursor=None)
        arguments = response[0].get("arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError(f"{command} returned malformed arguments.")
        leases = arguments.get("leases")
        if not isinstance(leases, list):
            raise RuntimeError(f"{command} returned a malformed leases collection.")
        count = arguments.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > limit or count != len(leases):
            raise RuntimeError(f"{command} returned an invalid count.")

        lease_address_values = _lease_address_values(leases, command)
        next_cursor = None
        if count == limit and lease_address_values:
            try:
                last_address = ipaddress.ip_address(lease_address_values[-1])
            except ValueError as exc:
                raise RuntimeError(f"{command} returned an invalid final ip-address.") from exc
            if last_address.version != version:
                raise RuntimeError(f"{command} returned a final lease for the wrong address family.")
            next_cursor = str(last_address)
        _validated_lease_addresses(leases, version, command)
        return LeasePage(leases=leases, next_cursor=next_cursor)

    def lease_get_all(self, version: int, *, per_page: int = 250, max_leases: int | None = None) -> LeaseCollection:
        """Return a bounded collection of all leases on the daemon.

        Uses ``lease{v}-get-page`` under the hood so it works with very large
        lease tables without loading everything into RAM at once.

        Args:
            version: DHCP version (4 or 6).
            per_page: Number of leases to fetch per API call (default 250).
            max_leases: Optional cap on the total number of leases returned.
                ``None`` means no cap.

        Returns:
            A validated LeaseCollection. Its ``truncated`` field is true only
            when the cap omitted leases or a full page indicates more can exist.

        Raises:
            KeaException: On a non-0/3 result code.
            RuntimeError: On a malformed response envelope.
            ValueError: If *per_page* is less than 1 or *max_leases* is less than 1.

        """
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if isinstance(per_page, bool) or not isinstance(per_page, int) or per_page < 1:
            raise ValueError(f"per_page must be >= 1, got {per_page!r}")
        if max_leases is not None and (
            isinstance(max_leases, bool) or not isinstance(max_leases, int) or max_leases < 1
        ):
            raise ValueError(f"max_leases must be >= 1 (or None for no cap), got {max_leases!r}")
        cursor: str | None = None
        all_leases: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()

        while True:
            page = self._request_lease_page(
                version,
                limit=per_page,
                cursor=_lease_page_start(version, cursor),
            )
            all_leases.extend(page.leases)
            if max_leases is not None and len(all_leases) >= max_leases:
                truncated = len(all_leases) > max_leases
                if not truncated and page.next_cursor is not None:
                    overflow_page = self._request_lease_page(version, limit=1, cursor=page.next_cursor)
                    truncated = bool(overflow_page.leases)
                all_leases = all_leases[:max_leases]
                return LeaseCollection(leases=all_leases, truncated=truncated)
            if page.next_cursor is None:
                return LeaseCollection(leases=all_leases, truncated=False)
            if page.next_cursor in seen_cursors:
                raise RuntimeError("Lease page cursor did not advance.")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

    def dhcp_disable(self, service: str, max_period: int | None = None) -> None:
        """Temporarily disable DHCP processing on *service*.

        The daemon continues running but stops responding to DHCP requests.
        Pass *max_period* (in seconds) to automatically re-enable after that time;
        omit it to keep the service disabled until :meth:`dhcp_enable` is called.

        Args:
            service: Kea service name, e.g. ``"dhcp4"`` or ``"dhcp6"``.
            max_period: Optional number of seconds before the service auto-re-enables.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        arguments: dict[str, Any] | None = None
        if max_period is not None:
            arguments = {"max-period": max_period}
        self.command("dhcp-disable", service=[service], arguments=arguments)

    def dhcp_enable(self, service: str) -> None:
        """Re-enable DHCP processing on *service* after a :meth:`dhcp_disable` call.

        Args:
            service: Kea service name, e.g. ``"dhcp4"`` or ``"dhcp6"``.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        self.command("dhcp-enable", service=[service])

    def pool_change(self, version: Family, action: PoolAction, subnet_id: int, declared_cidr: str, pool: str) -> None:
        """Send one ``subnet{v}-delta-{action}`` for the Pool of the Subnet. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        command = f"subnet{version}-delta-{action}"
        service = f"dhcp{version}"
        subnet = {"id": subnet_id, "subnet": declared_cidr, "pools": [{"pool": pool}]}
        response = self._config_mutation_command(command, service, {f"subnet{version}": [subnet]}, check=None)
        _one_reply(command, service, response)

    def _config_phase_command(self, command: str, service: str, arguments: dict[str, Any] | None = None) -> None:
        """Require one well-formed success reply for a single-service config phase."""
        if command == "config-set":
            if arguments is None:
                raise ValueError("config-set requires an explicit configuration.")
            response = self._config_mutation_command(command, service, arguments, check=None)
        else:
            response = self.command(command, service=[service], arguments=arguments, check=None)
        _one_reply(command, service, response)

    def config_candidate(self, version: Family) -> CandidateConfiguration:
        """Send one ``config-get`` and return the running configuration, for a read-modify-write.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.
            MalformedConfiguration: If the ``Dhcp{v}`` block is not an object.

        """
        command, service = "config-get", f"dhcp{version}"
        reply = _one_reply(command, service, self.command(command, service=[service], check=None))
        arguments = reply.get("arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError(f"{command} returned no configuration object for {service}.")
        # Kea 2.4+ adds "hash"; config-test and config-set reject it.
        arguments.pop("hash", None)
        return CandidateConfiguration(version, arguments)

    def config_test(self, candidate: CandidateConfiguration) -> None:
        """Send one ``config-test`` of *candidate*. It changes nothing.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_phase_command("config-test", candidate.service, candidate.arguments)

    def config_set(self, candidate: CandidateConfiguration) -> None:
        """Send one ``config-set`` of *candidate*. It does not persist.

        Raises:
            KeaException: If Kea returns a failure result.
            RuntimeError: If the reply is malformed.

        """
        self._config_phase_command("config-set", candidate.service, candidate.arguments)

    def persist(self, version: Family) -> PersistResult:
        """Write the running configuration to disk: ``config-get``, ``config-test`` of it, then ``config-write``.

        It never raises. ``failed`` means that NetBox cannot confirm that the disk copy holds the running
        configuration. A failed phase stops the step, so NetBox never writes a configuration it could not test.
        """
        if not self.persist_config:
            return PersistResult("not-requested")
        service = f"dhcp{version}"
        # requests errors are OSError subclasses; a missing TLS file raises a plain OSError.
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
            self._config_phase_command("config-write", service)
        except KeaException as exc:
            logger.warning("config-write failed for %s: %s", service, exc)
            return PersistResult("failed", (f"config-write failed: {exc.reply_text}",))
        except (OSError, ValueError, RuntimeError):
            logger.warning("The config-write reply of %s was lost or unreadable", service, exc_info=True)
            return PersistResult("failed", ("The reply to config-write was lost or unreadable.",))
        return PersistResult("persisted")

    def subnet_get(self, version: int, subnet_id: int) -> dict:
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
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        resp = self.command(
            f"subnet{version}-get",
            service=[service],
            arguments={"id": subnet_id},
        )
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


def _one_reply(command: str, service: str, response: list[KeaResponse], ok_codes: Sequence[int] = (0,)) -> KeaResponse:
    """Return the one reply of a single-service command, or raise when it is malformed or its result is not ok."""
    if (
        len(response) != 1
        or not isinstance(response[0], dict)
        or not isinstance(response[0].get("result"), int)
        or isinstance(response[0].get("result"), bool)
    ):
        raise RuntimeError(f"{command} did not return one valid result for {service}.")
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
