import base64
import copy
import ipaddress
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any, Literal, NamedTuple, TypedDict, cast

import requests
from requests.models import HTTPBasicAuth

from . import constants
from .dhcp_options import DHCPOption
from .reservations import (
    RESERVATION_PAGE_FETCH_FAILED,
    RESERVATION_PAGE_LIMIT_REACHED,
    RESERVATION_PAGINATION_STALLED,
    Family,
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
    ReservationPersistence,
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


def _merge_reservation_options(
    raw_options: list[dict[str, Any]],
    current_options: tuple[DHCPOption, ...],
    intended_options: tuple[DHCPOption, ...],
) -> list[dict[str, Any]]:
    """Overlay managed option facts while preserving matching Kea extension fields."""
    remaining = list(zip(current_options, raw_options, strict=True))
    merged_options: list[dict[str, Any]] = []
    for intended_option in intended_options:
        raw_option = None
        for index, (current_option, candidate) in enumerate(remaining):
            if current_option.match_key == intended_option.match_key:
                _, raw_option = remaining.pop(index)
                break
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


def _configured_subnet_network(subnet: Any, version: int) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Return one validated configured Subnet network for the requested family."""
    if not isinstance(subnet, dict):
        raise RuntimeError("config-get returned a malformed Subnet entry.")
    raw_cidr = subnet.get("subnet")
    if not isinstance(raw_cidr, str):
        raise RuntimeError("config-get returned a Subnet without a valid CIDR.")
    try:
        candidate = ipaddress.ip_network(raw_cidr, strict=True)
    except ValueError as exc:
        raise RuntimeError("config-get returned a Subnet without a valid CIDR.") from exc
    if candidate.version != version:
        raise RuntimeError(f"config-get returned an IPv{candidate.version} Subnet in Dhcp{version}.")
    return candidate


def _configured_subnet_id_for_network(
    subnet_collections: list[list[Any]],
    version: int,
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> int | None:
    """Find one Subnet ID while retaining malformed-entry evidence if no match exists."""
    malformed_entry_error: RuntimeError | None = None
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
            return subnet_id
    if malformed_entry_error is not None:
        raise malformed_entry_error
    return None


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
        check: None | Sequence[int] = (0,),
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
        except Exception:  # noqa: BLE001
            logger.exception("Configuration changed for %s, but cache invalidation failed", service)

    def _config_mutation_command(
        self,
        command: str,
        service: str,
        arguments: dict[str, Any],
    ) -> list[KeaResponse]:
        """Send one live configuration mutation between invalidation notifications."""
        self._notify_config_change(service)
        try:
            return self.command(command, service=[service], arguments=arguments)
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
    ) -> ReservationSnapshot:
        """Return one bounded, typed Reservation Snapshot, optionally for one subnet."""
        if version not in (4, 6):
            raise ValueError(f"version must be 4 or 6, got {version!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer.")
        source_index, from_index = _decode_reservation_cursor(cursor)
        hosts: list[dict[str, Any]] = []
        next_source, next_from = source_index, from_index
        seen_cursors = {(next_source, next_from)}
        empty_pages = 0
        while len(hosts) < limit:
            remaining = limit - len(hosts)
            page, candidate_from, candidate_source = self._reservation_raw_page(
                f"dhcp{version}",
                source_index=next_source,
                from_index=next_from,
                limit=remaining,
                subnet_id=subnet_id,
            )
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
            if len(hosts) == limit:
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

    def _reservation_mutation_persistence(
        self,
        version: int,
    ) -> ReservationPersistence:
        """Persist one confirmed Reservation mutation and report its outcome."""
        if not self.persist_config:
            return "not-requested"
        try:
            self._persist_config(f"dhcp{version}")
        except (KeaConfigPersistError, PartialPersistError, RuntimeError):
            logger.warning("Could not persist a confirmed DHCPv%s Reservation mutation", version, exc_info=True)
            return "failed"
        return "persisted"

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
        persistence = self._reservation_mutation_persistence(reservation.family)
        return ReservationMutationResult(
            previous=None,
            intended=reservation,
            application="applied",
            persistence=persistence,
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
        persistence = self._reservation_mutation_persistence(target.family)
        return ReservationMutationResult(
            previous=current,
            intended=intended,
            application="applied",
            persistence=persistence,
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
        persistence = self._reservation_mutation_persistence(target.family)
        return ReservationMutationResult(
            previous=current,
            intended=None,
            application="applied",
            persistence=persistence,
            verification=self._verify_reservation(None, target, catalogue),
        )

    def _subnet_list_entries(self, version: int) -> list[Any]:
        """Return the raw ``subnet{version}-list`` entries, or ``[]`` when Kea has no subnets.

        The single place that issues the command and validates its envelope. Each
        Each caller applies its own per-entry validation policy.

        Requires the ``subnet_cmds`` hook library.

        Raises:
            KeaException: If the command itself fails (result 2 when ``subnet_cmds``
                is not loaded).
            RuntimeError: If the envelope doesn't have the expected shape.

        """
        service = f"dhcp{version}"
        resp = self.command(f"subnet{version}-list", service=[service], check=(0, 3))
        if not resp or not isinstance(resp[0], dict):
            raise RuntimeError(f"subnet{version}-list returned malformed response: {resp!r}")
        if resp[0].get("result") == 3:
            return []
        arguments = resp[0].get("arguments")
        if not isinstance(arguments, dict) or not isinstance(arguments.get("subnets"), list):
            raise RuntimeError(f"subnet{version}-list returned malformed arguments: {resp[0]!r}")
        return arguments["subnets"]

    def list_subnets(self, version: int) -> list[tuple[str, int]]:
        """Return ``[(cidr, subnet_id), ...]`` for every subnet Kea has configured.

        One source for both the reservation form's Subnet CIDR suggestions and
        :meth:`subnet_id_from_cidr`, so the form cannot offer a CIDR that submitting
        would then fail to resolve. Kea keeps the subnets of a shared network in the
        same collection, so those are listed too.

        Requires the ``subnet_cmds`` hook library.

        Args:
            version: DHCP protocol version (``4`` or ``6``).

        Returns:
            One pair per configured subnet, in the order Kea reports them.

        Raises:
            KeaException: If the ``subnet{version}-list`` command itself fails.
            RuntimeError: If the response or any entry doesn't have the expected shape.

        """
        subnets: list[tuple[str, int]] = []
        for entry in self._subnet_list_entries(version):
            if not isinstance(entry, dict):
                raise RuntimeError(f"subnet{version}-list returned a non-dict subnet entry: {entry!r}")
            cidr = entry.get("subnet")
            if not isinstance(cidr, str) or not cidr:
                raise RuntimeError(f"subnet{version}-list returned an entry without a CIDR: {entry!r}")
            if "id" not in entry:
                raise RuntimeError(f"subnet{version}-list entry {cidr!r} has no id: {entry!r}")
            subnet_id = entry["id"]
            # bool is an int subclass, so `True` would otherwise pass as a subnet id.
            if isinstance(subnet_id, bool) or not isinstance(subnet_id, int):
                raise RuntimeError(f"subnet{version}-list returned a non-integer id: {subnet_id!r}")
            subnets.append((cidr, subnet_id))
        return subnets

    def subnet_id_from_cidr(self, version: int, cidr: str) -> int | None:
        """Return the Kea subnet ID for the subnet whose CIDR matches *cidr* exactly.

        Args:
            version: DHCP protocol version (``4`` or ``6``).
            cidr: Exact subnet CIDR string, e.g. ``"10.0.0.0/24"``.

        Returns:
            The integer subnet ID, or ``None`` if no subnet matches or Kea has none configured.
            Returns the first exact string match; if two subnets ever carried the
            same prefix the pick would be arbitrary — Kea's own config doesn't allow
            duplicate subnet CIDRs within a version, so this shouldn't occur.

        Raises:
            KeaException: If the ``subnet{version}-list`` command itself fails.
            RuntimeError: If the response doesn't have the expected shape.

        """
        for subnet_cidr, subnet_id in self.list_subnets(version):
            if subnet_cidr == cidr:
                return subnet_id
        return None

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

    def subnet_add(  # noqa: C901
        self,
        version: int,
        subnet_cidr: str,
        subnet_id: int | None = None,
        pools: list[str] | None = None,
        gateway: str | None = None,
        dns_servers: list[str] | None = None,
        ntp_servers: list[str] | None = None,
        ddns_qualifying_suffix: str | None = None,
    ) -> int | None:
        """Add a new subnet to Kea and persist the change.

        Args:
            version: DHCP version (4 or 6).
            subnet_cidr: Subnet in CIDR notation, e.g. ``"10.0.0.0/24"``.
            subnet_id: Optional Kea subnet ID. If ``None``, Kea auto-assigns.
            pools: Optional list of initial pool ranges (e.g. ``["10.0.0.100-10.0.0.200"]``).
            gateway: Optional default gateway IP (sets option ``routers``; DHCPv4 only).
            dns_servers: Optional list of DNS server IPs.
            ntp_servers: Optional list of NTP server hostnames/IPs.
            ddns_qualifying_suffix: Optional DDNS qualifying suffix for dynamic DNS updates.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        subnet_def: dict[str, Any] = {"subnet": subnet_cidr}
        if subnet_id is not None:
            subnet_def["id"] = subnet_id
        else:
            # Kea 3.x requires an explicit id — auto-assign max + 1
            try:
                list_resp = self.command(
                    f"subnet{version}-list",
                    service=[service],
                    check=(0, 3),  # result=3 means no subnets yet — treat as empty list
                )
                if not list_resp or not isinstance(list_resp[0], dict):
                    raise RuntimeError(f"subnet{version}-list returned malformed response: {list_resp!r}")
                if list_resp[0].get("result") == 3:
                    existing = []
                else:
                    arguments = list_resp[0].get("arguments")
                    if not isinstance(arguments, dict) or not isinstance(arguments.get("subnets"), list):
                        raise RuntimeError(f"subnet{version}-list returned malformed arguments: {list_resp[0]!r}")
                    existing = arguments["subnets"]
                max_id = max((s.get("id", 0) for s in existing), default=0)
                subnet_def["id"] = max_id + 1
            except KeaException:
                logger.warning("subnet%s-list failed; falling back to no explicit ID", version)
        if pools:
            subnet_def["pools"] = [{"pool": p} for p in pools]
        option_data: list[dict[str, str]] = []
        if gateway and version == 4:
            option_data.append({"name": "routers", "data": gateway})
        if dns_servers:
            option_data.append(
                {
                    "name": "domain-name-servers" if version == 4 else "dns-servers",
                    "data": ", ".join(dns_servers),
                }
            )
        if ntp_servers:
            option_data.append(
                {
                    "name": "ntp-servers" if version == 4 else "sntp-servers",
                    "data": ", ".join(ntp_servers),
                }
            )
        if option_data:
            subnet_def["option-data"] = option_data
        if ddns_qualifying_suffix:
            subnet_def["ddns-qualifying-suffix"] = ddns_qualifying_suffix
        try:
            last_exc: KeaException | None = None
            add_resp: list | None = None
            auto_assigned_id = subnet_id is None and "id" in subnet_def
            for _attempt in range(3):
                try:
                    add_resp = self._config_mutation_command(
                        f"subnet{version}-add",
                        service,
                        {subnet_key: [dict(subnet_def)]},
                    )
                    last_exc = None
                    break
                except KeaException as exc:
                    if auto_assigned_id and "duplicate" in str(exc).lower() and "id" in subnet_def:
                        subnet_def["id"] += 1
                        last_exc = exc
                    else:
                        raise
            if last_exc is not None:
                raise last_exc
        except (requests.RequestException, ValueError) as transport_exc:
            found_id = self._find_subnet_id_by_cidr(version, subnet_def["subnet"])
            if found_id is not None:
                err = PartialPersistError(service, transport_exc, subnet_id=found_id)
                raise err from transport_exc
            raise
        # Prefer the authoritative ID Kea echoes back in the add response — it is
        # the only source of truth when subnet{v}-list failed and no explicit id was
        # provided (subnet_def would have no "id" key in that case → returns None).
        if add_resp:
            subnets = (add_resp[0].get("arguments") or {}).get("subnets")
            if subnets:
                kea_id = subnets[0].get("id")
                if kea_id is not None:
                    subnet_def["id"] = kea_id
        try:
            self._persist_config(service)
        except KeaConfigPersistError as exc:
            exc.subnet_id = subnet_def.get("id")
            raise
        except PartialPersistError as exc:
            # Subnet is live; re-raise with the known ID so callers can still
            # perform follow-up operations (e.g. assign to a shared network).
            exc.subnet_id = subnet_def.get("id")
            raise
        return subnet_def.get("id")

    def subnet_del(self, version: int, subnet_id: int) -> None:
        """Delete an existing subnet from Kea and persist the change.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to delete.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        self._config_mutation_command(
            f"subnet{version}-del",
            service,
            {"id": subnet_id},
        )
        self._persist_config(service)

    def network_add(self, version: int, name: str, options: list[dict] | None = None) -> None:
        """Create a new shared network in Kea and persist the change.

        Args:
            version: DHCP version (4 or 6).
            name: Unique name for the shared network.
            options: Optional list of option-data dicts.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        network_def: dict[str, Any] = {"name": name}
        if options:
            network_def["option-data"] = options
        self._config_mutation_command(
            f"network{version}-add",
            service,
            {"shared-networks": [network_def]},
        )
        self._persist_config(service)

    def network_del(self, version: int, name: str) -> None:
        """Delete a shared network from Kea and persist the change.

        Subnets that were members of the deleted network fall back to the global
        address pool (Kea behaviour).

        Args:
            version: DHCP version (4 or 6).
            name: Name of the shared network to delete.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        self._config_mutation_command(
            f"network{version}-del",
            service,
            {"name": name},
        )
        self._persist_config(service)

    def network_update(
        self,
        version: int,
        name: str,
        description: str | None = None,
        interface: str | None = None,
        relay_addresses: list[str] | None = None,
        options: list[dict] | None = None,
    ) -> None:
        """Update a shared network's properties via config-get → config-test → config-set → config-write.

        Only provided (non-None) fields are modified; others are left unchanged.
        Raises ``KeaException`` if *name* is not found in the config.
        Raises ``KeaConfigTestError`` if config-test validation fails.
        Raises ``PartialPersistError`` if config-write fails after a successful config-set (change
        is live but will not survive restart).
        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"

        resp = self.command("config-get", service=[service])
        # Strip the "hash" key that Kea 2.4+ includes — config-test and config-set reject it.
        raw = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = {k: v for k, v in raw.items() if k != "hash"}

        network: dict[str, Any] | None = None
        for sn in config.get(dhcp_key, {}).get("shared-networks", []):
            if sn.get("name") == name:
                network = sn
                break
        if network is None:
            raise KeaException({"result": 3, "text": f"Shared network '{name}' not found in config"})

        if description is not None:
            network["description"] = description
        if interface is not None:
            if interface:
                network["interface"] = interface
            else:
                network.pop("interface", None)
        if relay_addresses is not None:
            if relay_addresses:
                network["relay"] = {"ip-addresses": relay_addresses}
            else:
                network.pop("relay", None)
        if options is not None:
            network["option-data"] = options

        self._apply_config(service, config)

    def network_subnet_add(self, version: int, name: str, subnet_id: int) -> None:
        """Move an existing subnet into a shared network.

        Args:
            version: DHCP version (4 or 6).
            name: Shared network name.
            subnet_id: Kea subnet ID to assign.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        self._config_mutation_command(
            f"network{version}-subnet-add",
            service,
            {"name": name, "id": subnet_id},
        )
        self._persist_config(service)

    def network_subnet_del(self, version: int, name: str, subnet_id: int) -> None:
        """Remove a subnet from a shared network (subnet remains, reverts to global pool).

        Args:
            version: DHCP version (4 or 6).
            name: Shared network name.
            subnet_id: Kea subnet ID to remove from the network.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        self._config_mutation_command(
            f"network{version}-subnet-del",
            service,
            {"name": name, "id": subnet_id},
        )
        self._persist_config(service)

    def subnet_update(
        self,
        version: int,
        subnet_id: int,
        subnet_cidr: str,
        pools: list[str] | None = None,
        gateway: str | None = None,
        dns_servers: list[str] | None = None,
        ntp_servers: list[str] | None = None,
        ddns_qualifying_suffix: str | None = None,
        valid_lft: int | None = None,
        min_valid_lft: int | None = None,
        max_valid_lft: int | None = None,
        renew_timer: int | None = None,
        rebind_timer: int | None = None,
    ) -> None:
        """Update an existing subnet's configuration in Kea and persist the change.

        Performs a read-modify-write: fetches the live subnet via ``subnet_get()``, merges
        only the form-managed fields onto it, then sends the complete merged object to
        ``subnet{v}-update``.  This preserves Kea-managed fields — relay config, allocator
        settings, client-class, reservations, and any ``option-data`` entries not owned by
        this form — that Kea would otherwise clear if we sent a partial object.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID of the subnet to update.
            subnet_cidr: Subnet in CIDR notation (immutable identifier, still required by Kea).
            pools: List of pool range strings.  ``None`` = omit (Kea keeps existing);
                ``[]`` = explicitly clear all pools.
            gateway: Default gateway IP (option ``routers``, DHCPv4 only).
            dns_servers: List of DNS server IP strings.
            ntp_servers: List of NTP server hostnames/IPs.
            ddns_qualifying_suffix: DDNS qualifying suffix.  ``None`` = omit (Kea keeps
                existing); ``""`` = explicitly clear; a value sets it.
            valid_lft: Preferred lease lifetime in seconds.
            min_valid_lft: Minimum lease lifetime in seconds.
            max_valid_lft: Maximum lease lifetime in seconds.
            renew_timer: T1 renew timer in seconds (sent as ``renew-timer``).
            rebind_timer: T2 rebind timer in seconds (sent as ``rebind-timer``).

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        # Read live subnet so we can merge — Kea's subnet{v}-update replaces the full
        # object, so we must send ALL fields to avoid silently clearing relay, allocator,
        # client-class, reservations, and any option-data not managed by this form.
        subnet_def = self.subnet_get(version, subnet_id)
        subnet_def.pop("metadata", None)  # Kea adds a read-only metadata key in some responses

        # Identity: always authoritative from params
        subnet_def["id"] = subnet_id
        subnet_def["subnet"] = subnet_cidr

        # option-data: preserve entries NOT owned by this form (e.g. domain-name, tftp-server)
        # while replacing/adding/removing the ones the form manages.
        _managed_option_names = {
            "routers",
            "domain-name-servers",
            "dns-servers",
            "ntp-servers",
            "sntp-servers",
        }
        preserved_opts = [o for o in subnet_def.get("option-data", []) if o.get("name") not in _managed_option_names]
        new_opts: list[dict[str, str]] = []
        if gateway and version == 4:
            new_opts.append({"name": "routers", "data": gateway})
        if dns_servers:
            new_opts.append(
                {
                    "name": "domain-name-servers" if version == 4 else "dns-servers",
                    "data": ", ".join(dns_servers),
                }
            )
        if ntp_servers:
            new_opts.append(
                {
                    "name": "ntp-servers" if version == 4 else "sntp-servers",
                    "data": ", ".join(ntp_servers),
                }
            )
        subnet_def["option-data"] = preserved_opts + new_opts

        # ddns-qualifying-suffix: None = omit (Kea keeps existing); "" = explicitly clear; a value sets it.
        if ddns_qualifying_suffix is not None:
            if ddns_qualifying_suffix:
                subnet_def["ddns-qualifying-suffix"] = ddns_qualifying_suffix
            else:
                subnet_def.pop("ddns-qualifying-suffix", None)

        # pools: replace only when the caller explicitly passes a value
        if pools is not None:
            subnet_def["pools"] = [{"pool": p} for p in pools]

        # Lifetime / timer fields: override only when explicitly provided, otherwise
        # the live value (already present in subnet_def from subnet_get) is kept.
        for value, kea_key in [
            (valid_lft, "valid-lft"),
            (min_valid_lft, "min-valid-lft"),
            (max_valid_lft, "max-valid-lft"),
            (renew_timer, "renew-timer"),
            (rebind_timer, "rebind-timer"),
        ]:
            if value is not None:
                subnet_def[kea_key] = value

        self._config_mutation_command(
            f"subnet{version}-update",
            service,
            {subnet_key: [subnet_def]},
        )
        self._persist_config(service)

    def subnet_update_options(self, version: int, subnet_id: int, options: list[dict]) -> None:
        """Update option-data for a subnet via config-get → config-test → config-write.

        Free Kea has no option-set hook, so the only supported approach is a full
        read-modify-write cycle: fetch the current config, replace the subnet's
        ``option-data`` in the Python dict, then validate and write it back using
        ``config-test`` (with the modified config as ``arguments``) followed by
        ``config-write`` (also with the modified config).

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID.
            options: New ``option-data`` list. Pass ``[]`` to remove all options.

        Raises:
            KeaException: If ``subnet_id`` is not found, or if ``config-test`` fails.
            PartialPersistError: If ``config-write`` fails after successful ``config-test``.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        subnet_key = f"subnet{version}"

        resp = self.command("config-get", service=[service])
        raw = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = raw
        config.pop("hash", None)

        subnet = None
        for s in config.get(dhcp_key, {}).get(subnet_key, []):
            if s.get("id") == subnet_id:
                subnet = s
                break
        if subnet is None:
            for sn in config.get(dhcp_key, {}).get("shared-networks", []):
                for s in sn.get(subnet_key, []):
                    if s.get("id") == subnet_id:
                        subnet = s
                        break
                if subnet is not None:
                    break
        if subnet is None:
            raise KeaException({"result": 3, "text": f"Subnet id {subnet_id} not found in config"})

        subnet["option-data"] = options
        self._apply_config(service, config)

    def server_update_options(self, version: int, options: list[dict]) -> None:
        """Update server-level option-data via config-get → config-test → config-write.

        Replaces the ``option-data`` list at the ``Dhcp{v}`` level (not per-subnet).
        Uses the same read-modify-write pipeline as :meth:`subnet_update_options`.

        Args:
            version: DHCP version (4 or 6).
            options: New ``option-data`` list. Pass ``[]`` to remove all server-level options.

        Raises:
            KeaException: If ``config-test`` fails.
            PartialPersistError: If ``config-write`` fails after successful ``config-test``.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"

        resp = self.command("config-get", service=[service])
        raw = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = raw
        config.pop("hash", None)
        config.setdefault(dhcp_key, {})["option-data"] = options
        self._apply_config(service, config)

    def option_def_list(self, version: int) -> list[dict]:
        """Return the current ``option-def`` list for a DHCP version via ``config-get``.

        Args:
            version: DHCP version (4 or 6).

        Returns:
            List of option-def dicts, or ``[]`` if none are defined.

        Raises:
            KeaException: If ``config-get`` fails.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        resp = self.command("config-get", service=[service])
        raw = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        return raw.get(dhcp_key, {}).get("option-def", [])

    def option_def_add(self, version: int, option_def: dict) -> None:
        """Append a new option-def entry via config-get → config-test → config-write.

        Args:
            version: DHCP version (4 or 6).
            option_def: A dict with keys ``name``, ``code``, ``type``, ``space``,
                and optionally ``array``, ``encapsulate``, ``record-types``.

        Raises:
            KeaException: If ``config-test`` fails.
            PartialPersistError: If ``config-write`` fails after successful ``config-test``.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        resp = self.command("config-get", service=[service])
        raw_args = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw_args, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = copy.deepcopy(raw_args)
        config.pop("hash", None)
        defs = config.setdefault(dhcp_key, {}).setdefault("option-def", [])
        defs.append(option_def)
        self._apply_config(service, config)

    def option_def_del(self, version: int, code: int, space: str) -> None:
        """Remove an option-def entry by code+space via config-get → config-test → config-write.

        Args:
            version: DHCP version (4 or 6).
            code: Option code of the entry to remove.
            space: Option space of the entry to remove.

        Raises:
            KeaConfigTestError: If ``config-test`` fails before the mutation is applied.
            PartialPersistError: If ``config-write`` fails after successful ``config-test``.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        resp = self.command("config-get", service=[service])
        raw_args = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw_args, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = copy.deepcopy(raw_args)
        config.pop("hash", None)
        defs = config.get(dhcp_key, {}).get("option-def", [])
        new_defs = [d for d in defs if not (d.get("code") == code and d.get("space") == space)]
        if len(new_defs) == len(defs):
            raise KeaException({"result": 3, "text": f"option-def code={code} space={space} not found"})
        config.setdefault(dhcp_key, {})["option-def"] = new_defs
        self._apply_config(service, config)

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

        command, response, fallback_state = self._lease_search_response(
            version,
            command_suffix,
            arguments,
            state,
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
        if fallback_state is not None:
            if any(
                isinstance(lease.get("state"), bool) or not isinstance(lease.get("state"), int) for lease in raw_leases
            ):
                raise RuntimeError(f"{command} returned a lease with an invalid state.")
            raw_leases = [lease for lease in raw_leases if lease["state"] == fallback_state]
        return raw_leases

    def _lease_search_response(
        self,
        version: int,
        command_suffix: str,
        arguments: dict[str, Any],
        state: int | None,
    ) -> tuple[str, list[KeaResponse], int | None]:
        """Run one lease query with the explicit unguarded compatibility fallback."""
        command = f"lease{version}-get{command_suffix}"
        try:
            response = self.command(
                command,
                service=[f"dhcp{version}"],
                arguments=arguments,
                check=(0, 3),
            )
        except KeaException as exc:
            if command_suffix != "-by-state" or exc.response.get("result") != 2:
                raise
            if self.max_unpaged_leases is not None:
                raise LeaseQueryPreflightUnavailable("state-command") from exc
            command = f"lease{version}-get-all"
            response = self.command(
                command,
                service=[f"dhcp{version}"],
                arguments={"subnets": [arguments["subnet-id"]]},
                check=(0, 3),
            )
            return command, response, state
        return command, response, None

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
            if exc.response.get("result") == 2:
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

    def pool_add(self, version: int, subnet_id: int, pool: str) -> None:
        """Add a pool to an existing subnet and persist the change.

        Supports both Kea 2.x (``subnet{v}-pool-add``) and Kea 3.x
        (``subnet{v}-delta-add``). The delta command requires the subnet CIDR,
        which is fetched automatically when the pool-add command is unavailable.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to add the pool to.
            pool: Pool range string (e.g. ``"10.0.0.50-10.0.0.99"`` or CIDR ``"10.0.0.0/28"``).

        Raises:
            KeaException: If Kea returns a non-zero result code for either command.
            RuntimeError: If the delta-add path's ``get_subnet_cidr`` lookup gets a
                malformed ``subnet{version}-get`` response.
            ValueError: If the delta-add path's ``get_subnet_cidr`` lookup returns a
                CIDR that doesn't match *version*'s address family.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        available = self.get_available_commands(service)
        if f"subnet{version}-pool-add" in available:
            self._config_mutation_command(
                f"subnet{version}-pool-add",
                service,
                {subnet_key: [{"id": subnet_id, "pools": [{"pool": pool}]}]},
            )
        else:
            subnet_cidr = self.get_subnet_cidr(version, subnet_id)
            self._config_mutation_command(
                f"subnet{version}-delta-add",
                service,
                {subnet_key: [{"id": subnet_id, "subnet": subnet_cidr, "pools": [{"pool": pool}]}]},
            )
        self._persist_config(service)

    def pool_del(self, version: int, subnet_id: int, pool: str) -> None:
        """Remove a pool from an existing subnet and persist the change.

        Supports both Kea 2.x (``subnet{v}-pool-del``) and Kea 3.x
        (``subnet{v}-delta-del``). The delta command requires the subnet CIDR,
        which is fetched automatically when the pool-del command is unavailable.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to remove the pool from.
            pool: Pool range string identifying the pool to delete.

        Raises:
            KeaException: If Kea returns a non-zero result code for either command.
            RuntimeError: If the delta-del path's ``get_subnet_cidr`` lookup gets a
                malformed ``subnet{version}-get`` response.
            ValueError: If the delta-del path's ``get_subnet_cidr`` lookup returns a
                CIDR that doesn't match *version*'s address family.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        available = self.get_available_commands(service)
        if f"subnet{version}-pool-del" in available:
            self._config_mutation_command(
                f"subnet{version}-pool-del",
                service,
                {subnet_key: [{"id": subnet_id, "pools": [{"pool": pool}]}]},
            )
        else:
            subnet_cidr = self.get_subnet_cidr(version, subnet_id)
            self._config_mutation_command(
                f"subnet{version}-delta-del",
                service,
                {subnet_key: [{"id": subnet_id, "subnet": subnet_cidr, "pools": [{"pool": pool}]}]},
            )
        self._persist_config(service)

    def _apply_config(self, service: str, config: dict) -> None:
        """Validate, apply, and persist a modified config dict.

        Used by read-modify-write methods (e.g. ``subnet_update_options``,
        ``server_update_options``, ``option_def_add/del``) that mutate a config
        obtained from ``config-get`` and need to push it back.

        Flow: ``config-test`` → ``config-set`` → ``config-write``.

        Args:
            service: Kea service name (e.g. ``"dhcp4"``).
            config: The full config dict (already mutated) to apply.

        Raises:
            KeaConfigTestError: If ``config-test`` fails (result != 2).
            PartialPersistError: If ``config-write`` fails after ``config-set``.

        """
        try:
            self.command("config-test", service=[service], arguments=config)
        except KeaException as exc:
            if exc.response.get("result") == 2:
                logger.debug("config-test not supported for service %s — skipping pre-flight check", service)
            else:
                logger.warning("config-test failed for service %s — aborting config-set", service)
                raise KeaConfigTestError(service, exc) from exc
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                "config-test transport/parse error for service %s — aborting config-set",
                service,
            )
            raise KeaConfigTestError(service, exc) from exc
        try:
            self._config_mutation_command("config-set", service, config)
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                "config-set transport/parse error for service %s — change may be live but unpersisted", service
            )
            raise AmbiguousConfigSetError(service, exc) from exc
        if self.persist_config:
            try:
                self.command("config-write", service=[service])
            except (KeaException, requests.RequestException, ValueError) as exc:
                logger.warning("config-write failed for service %s — change not persisted to disk", service)
                raise PartialPersistError(service, exc) from exc
        else:
            logger.debug("persist_config disabled for service %s — skipping config-write after config-set", service)

    def _persist_config(self, service: str) -> None:
        """Validate the current running config and persist it to disk.

        Flow:
        1. ``config-get`` — fetch the live in-memory config (which already reflects
           any mutation applied via Kea-native commands like ``subnet4-delta-add``).
        2. ``config-test`` with that config as ``arguments`` — validate it.  Kea
           requires the config to be passed as arguments; calling ``config-test``
           without arguments always returns result 1 "Missing mandatory 'arguments'
           parameter."  Result 2 (command not supported) is silently skipped.  Any
           other non-zero result raises :exc:`KeaConfigTestError`.
        3. ``config-write`` — persist the validated config to disk.  Failure raises
           :exc:`PartialPersistError` (change is live but will be lost on restart).
        """
        if not self.persist_config:
            logger.debug("persist_config disabled for service %s — skipping config-write", service)
            return
        # Step 1: fetch the current in-memory config so we can validate and write it.
        try:
            resp = self.command("config-get", service=[service])
        except (KeaException, requests.RequestException, ValueError):
            logger.warning("config-get failed for service %s — skipping validation, attempting config-write", service)
            resp = None

        config: dict | None = None
        if resp is not None:
            if isinstance(resp, list) and resp and isinstance(resp[0], dict):
                raw = resp[0].get("arguments")
            else:
                raw = resp.get("arguments") if isinstance(resp, dict) else None
            if isinstance(raw, dict):
                config = {k: v for k, v in raw.items() if k != "hash"}
            else:
                logger.warning(
                    "config-get for service %s returned unexpected arguments shape: %s", service, type(raw).__name__
                )

        # Step 2: config-test — pass the live config as arguments (required by Kea).
        if config is not None:
            try:
                self.command("config-test", service=[service], arguments=config)
            except KeaException as exc:
                result = exc.response.get("result")
                if result == 2:
                    logger.debug("config-test not supported for service %s — skipping pre-flight check", service)
                else:
                    logger.warning("config-test failed for service %s — aborting config-write", service)
                    raise KeaConfigPersistError(service, exc) from exc
            except (requests.RequestException, ValueError) as exc:
                logger.warning(
                    "config-test transport error for service %s — aborting config-write", service, exc_info=True
                )
                raise KeaConfigPersistError(service, exc) from exc

        # Step 3: write to disk.
        try:
            self.command("config-write", service=[service])
        except (KeaException, requests.RequestException, ValueError) as exc:
            logger.warning(
                "config-write failed for service %s — change is live but not persisted to disk",
                service,
            )
            raise PartialPersistError(service, exc) from exc

    def get_subnet_cidr(self, version: int, subnet_id: int) -> str:
        """Fetch the CIDR string for *subnet_id* from Kea (e.g. ``"10.0.0.0/24"``).

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to look up.

        Returns:
            Subnet CIDR string.

        Raises:
            KeaException: If Kea reports the subnet as not found (result code 3).
            RuntimeError: If the response itself is malformed (missing/wrong-typed
                ``arguments``, ``subnet{v}``, subnet entry, or ``subnet`` field —
                including an empty ``subnet{v}`` list despite a result-0 response).
            ValueError: If ``subnet`` is a string but not a CIDR of the requested
                family.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        resp = self.command(
            f"subnet{version}-get",
            service=[service],
            arguments={"id": subnet_id},
            check=(0, 3),
        )
        if not resp or not isinstance(resp[0], dict):
            raise RuntimeError(f"subnet{version}-get returned malformed response: {resp!r}")
        if resp[0].get("result") == 3:
            raise KeaException(resp[0], index=0)
        arguments = resp[0].get("arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError(f"subnet{version}-get returned malformed arguments: {resp[0]!r}")
        subnets = arguments.get(subnet_key)
        if not isinstance(subnets, list):
            raise RuntimeError(f"subnet{version}-get returned a non-list {subnet_key!r}: {subnets!r}")
        if not subnets:
            raise RuntimeError(f"subnet{version}-get returned an empty {subnet_key!r} despite result=0: {resp[0]!r}")
        if not isinstance(subnets[0], dict):
            raise RuntimeError(f"subnet{version}-get returned a non-dict subnet entry: {subnets[0]!r}")
        cidr = subnets[0].get("subnet")
        if not isinstance(cidr, str) or not cidr:
            raise RuntimeError(f"subnet{version}-get response missing 'subnet' field for id={subnet_id}")
        network_cls = ipaddress.IPv4Network if version == 4 else ipaddress.IPv6Network
        try:
            network_cls(cidr, strict=True)
        except ValueError as exc:
            raise ValueError(f"subnet{version}-get returned a CIDR not matching IPv{version}: {cidr!r}") from exc
        return cidr

    def subnet_get(self, version: int, subnet_id: int) -> dict:
        """Fetch the full subnet config dict for *subnet_id* from Kea.

        Unlike :meth:`get_subnet_cidr`, this method returns the complete
        subnet object (id, subnet, pools, option-data, relay, allocator, ...)
        enabling a read-modify-write cycle without losing live-only fields.

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
        args = resp[0].get("arguments") or {}
        subnets = args.get(subnet_key, []) if isinstance(args, dict) else []
        if not subnets:
            raise KeaException(
                {"result": 3, "text": f"subnet{version}-get returned no subnet for id={subnet_id}", "arguments": None},
                index=0,
            )
        return dict(subnets[0])

    def _find_subnet_id_by_cidr(self, version: int, cidr: str) -> int | None:
        """Search the running Kea config for a subnet matching *cidr*.

        Returns the Kea subnet ID if found, or ``None`` if the subnet does not
        exist or if the config-get probe itself fails.  Used as a best-effort
        disambiguation probe after a transport error on ``subnet{v}-add`` to
        detect whether the command was actually processed by Kea.

        """
        try:
            return self.configured_subnet_id_from_cidr(version, cidr)
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.debug(
                "_find_subnet_id_by_cidr: config-get failed for cidr=%s version=%s",
                cidr,
                version,
                exc_info=True,
            )
            return None


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


class KeaConfigTestError(KeaException):
    """Raised when ``config-test`` fails before any mutation has been applied.

    The Kea configuration is unchanged — no data has been written.
    The original :exc:`KeaException` from config-test is stored in ``__cause__``.

    Used by ``_apply_config`` (read-modify-write methods such as
    ``subnet_update_options`` and ``server_update_options``) where config-test
    is run *before* ``config-set``, so a failure means the running config is
    still intact.
    """

    def __init__(self, service: str, cause: Exception) -> None:
        response: KeaResponse = {
            "result": -1,
            "text": f"config-test failed for service {service!r} — mutation was not applied",
            "arguments": [],
        }
        super().__init__(response, msg=f"config-test error for {service!r}")
        self.service = service


class KeaConfigPersistError(KeaException):
    """Raised when ``_persist_config`` rejects the already-live config via ``config-test``.

    The mutation IS already applied to the running daemon (the change is live in
    memory) but config-test found the resulting config invalid, so config-write
    was skipped.  The change **will be lost on daemon restart**.

    Distinct from :exc:`PartialPersistError` (which is raised when config-write
    itself fails after a successful config-test) and from :exc:`KeaConfigTestError`
    (which is raised before any mutation is applied).
    """

    def __init__(self, service: str, cause: Exception) -> None:
        response: KeaResponse = {
            "result": -1,
            "text": (
                f"config-test rejected the running config for service {service!r} "
                "— mutation is live but config-write was skipped"
            ),
            "arguments": [],
        }
        super().__init__(response, msg=f"config persist error for {service!r}")
        self.service = service


class PartialPersistError(KeaException):
    """Raised when a Kea mutation is live but config-write failed.

    The change is applied in memory but will be lost on Kea restart.
    The original :exc:`KeaException` from config-write is stored in ``__cause__``.

    ``subnet_id`` is set when the partial write occurred during ``subnet_add`` —
    the subnet is live and this ID can still be used for follow-up operations
    (e.g. assigning to a shared network) even though config-write failed.
    """

    def __init__(self, service: str, cause: Exception, subnet_id: int | None = None) -> None:
        response: KeaResponse = {
            "result": -1,
            "text": f"config-write failed for service {service!r} — change is live but not persisted to disk",
            "arguments": [],
        }
        super().__init__(response, msg=f"partial persist error for {service!r}")
        self.service = service
        self.subnet_id: int | None = subnet_id


class AmbiguousConfigSetError(PartialPersistError):
    """Raised when a config-set reply is lost or malformed.

    The change *may* be live but we cannot confirm — the transport or JSON
    parsing failed after sending the config-set command.  Distinct from
    :exc:`PartialPersistError` where we *know* the mutation succeeded but
    config-write failed.

    Inherits from :exc:`PartialPersistError` so existing ``except
    PartialPersistError`` handlers still catch it.  Callers that need to
    distinguish ambiguous-set from definite-write-failure can catch this
    subclass first.
    """

    def __init__(self, service: str, cause: Exception) -> None:
        super().__init__(service, cause)
        ambiguous_text = f"config-set reply lost/malformed for service {service!r} — change may or may not be live"
        self.response["text"] = ambiguous_text
        self.args = (f"partial persist error for {service!r}: {ambiguous_text}",)


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
