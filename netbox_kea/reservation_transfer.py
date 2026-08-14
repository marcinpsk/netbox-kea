from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Any, Literal, cast

import yaml

from .dhcp_options import DHCPOption, parse_dhcp_option
from .reservations import (
    Family,
    GlobalReservationScope,
    InSubnetReservationScope,
    IPv4Reservation,
    IPv6Reservation,
    Reservation,
    ReservationIdentity,
    reservation_identifier_types,
)
from .subnet_catalogue import SubnetIdentity

TransferFormat = Literal["yaml", "json"]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class ReservationTransferError(ValueError):
    """The transfer document cannot be decoded."""


@dataclass(frozen=True)
class ReservationTransferDiagnostic:
    """One error at an exact transfer document position."""

    code: str
    message: str
    source_position: str


@dataclass(frozen=True)
class ReservationImportProposal:
    """One validated In-Subnet Reservation ready for subnet resolution."""

    family: Family
    subnet_cidr: str
    identity: ReservationIdentity
    addresses: tuple[IPAddress, ...]
    delegated_prefixes: tuple[ipaddress.IPv6Network, ...]
    hostname: str
    options: tuple[DHCPOption, ...]


@dataclass(frozen=True)
class ReservationTransferResult:
    """All valid proposals or all document diagnostics, never both."""

    proposals: tuple[ReservationImportProposal, ...]
    diagnostics: tuple[ReservationTransferDiagnostic, ...]


def resolve_import_proposal(proposal: ReservationImportProposal, subnet: SubnetIdentity) -> Reservation:
    """Bind a validated proposal to one live-verified Subnet Identity."""
    if str(subnet.network) != proposal.subnet_cidr or subnet.network.version != proposal.family:
        raise ValueError("The live Subnet Identity does not match the import proposal.")
    scope = InSubnetReservationScope(subnet)
    if proposal.family == 4:
        return IPv4Reservation(
            scope=scope,
            identity=proposal.identity,
            addresses=cast(tuple[ipaddress.IPv4Address, ...], proposal.addresses),
            hostname=proposal.hostname,
            options=proposal.options,
        )
    return IPv6Reservation(
        scope=scope,
        identity=proposal.identity,
        addresses=cast(tuple[ipaddress.IPv6Address, ...], proposal.addresses),
        delegated_prefixes=proposal.delegated_prefixes,
        hostname=proposal.hostname,
        options=proposal.options,
    )


def _option_document(option: DHCPOption) -> dict[str, Any]:
    return {
        key: value
        for key, value in (
            ("code", option.code),
            ("name", option.name),
            ("space", option.space),
            ("data", option.data),
            ("csv_format", option.csv_format),
            ("always_send", option.always_send),
            ("never_send", option.never_send),
        )
        if value is not None
    }


def _reservation_document(reservation: Reservation) -> dict[str, Any]:
    if isinstance(reservation.scope, GlobalReservationScope):
        scope: dict[str, Any] = {"type": "global"}
    else:
        scope = {
            "type": "in-subnet",
            "subnet": {"cidr": str(reservation.scope.subnet.network)},
        }
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
        "options": [_option_document(option) for option in reservation.options],
    }


def export_reservation_document(records: tuple[Reservation, ...], format_name: str) -> str:
    """Export normalized Reservation records in one explicit format."""
    document = {"version": 1, "reservations": [_reservation_document(record) for record in records]}
    if format_name == "json":
        return json.dumps(document, indent=2) + "\n"
    if format_name == "yaml":
        return yaml.safe_dump(document, sort_keys=False)
    raise ReservationTransferError("The transfer format must be YAML or JSON.")


def _load_document(document: str, format_name: str) -> Any:
    try:
        if format_name == "json":
            return json.loads(document)
        if format_name == "yaml":
            return yaml.safe_load(document)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ReservationTransferError("The transfer document is not valid syntax for the selected format.") from exc
    raise ReservationTransferError("The transfer format must be YAML or JSON.")


def _diagnostic(code: str, message: str, position: str) -> ReservationTransferDiagnostic:
    return ReservationTransferDiagnostic(code=code, message=message, source_position=position)


def _parse_family(value: Any, position: str, diagnostics: list[ReservationTransferDiagnostic]) -> Family | None:
    if isinstance(value, bool) or value not in (4, 6):
        diagnostics.append(_diagnostic("invalid-family", "Family must be 4 or 6.", position))
        return None
    return cast(Family, value)


def _parse_scope(
    value: Any,
    family: Family | None,
    position: str,
    diagnostics: list[ReservationTransferDiagnostic],
) -> str | None:
    if not isinstance(value, dict):
        diagnostics.append(_diagnostic("invalid-scope", "Scope must be an object.", position))
        return None
    scope_type = value.get("type")
    if scope_type == "global":
        diagnostics.append(
            _diagnostic(
                "unsupported-scope",
                "Import does not support Global Reservation Scope.",
                f"{position}.type",
            )
        )
        return None
    if scope_type != "in-subnet":
        diagnostics.append(_diagnostic("invalid-scope", "Scope type must be in-subnet or global.", f"{position}.type"))
        return None
    subnet = value.get("subnet")
    cidr = subnet.get("cidr") if isinstance(subnet, dict) else None
    try:
        network = ipaddress.ip_network(cidr, strict=True)
    except (TypeError, ValueError):
        diagnostics.append(
            _diagnostic("invalid-subnet", "The subnet CIDR must be a canonical network.", f"{position}.subnet.cidr")
        )
        return None
    if family is not None and network.version != family:
        diagnostics.append(
            _diagnostic("invalid-subnet", "The subnet CIDR must use the Reservation family.", f"{position}.subnet.cidr")
        )
        return None
    return str(network)


def _parse_identity(
    value: Any,
    family: Family | None,
    position: str,
    diagnostics: list[ReservationTransferDiagnostic],
) -> ReservationIdentity | None:
    if not isinstance(value, dict):
        diagnostics.append(_diagnostic("invalid-identity", "Identity must be an object.", position))
        return None
    try:
        identity = ReservationIdentity(value.get("type"), value.get("value"))
    except (TypeError, ValueError):
        diagnostics.append(_diagnostic("invalid-identity", "The Reservation Identity is invalid.", f"{position}.value"))
        return None
    if family is not None and identity.identifier_type not in reservation_identifier_types(family):
        diagnostics.append(
            _diagnostic("invalid-identity", "The Identity type is not valid for this family.", f"{position}.type")
        )
        return None
    return identity


def _parse_addresses(
    value: Any,
    family: Family | None,
    subnet_cidr: str | None,
    position: str,
    diagnostics: list[ReservationTransferDiagnostic],
) -> tuple[IPAddress, ...] | None:
    if not isinstance(value, list):
        diagnostics.append(_diagnostic("invalid-addresses", "Addresses must be a list.", position))
        return None
    subnet = ipaddress.ip_network(subnet_cidr) if subnet_cidr is not None else None
    parsed: list[IPAddress] = []
    for index, raw in enumerate(value):
        try:
            address = ipaddress.ip_address(raw)
        except (TypeError, ValueError):
            diagnostics.append(_diagnostic("invalid-address", "The address is invalid.", f"{position}[{index}]"))
            continue
        if family is not None and address.version != family:
            diagnostics.append(
                _diagnostic("invalid-address", "The address must use the Reservation family.", f"{position}[{index}]")
            )
        elif subnet is not None and address not in subnet:
            diagnostics.append(
                _diagnostic(
                    "out-of-subnet-address",
                    "The address must belong to the selected In-Subnet Scope.",
                    f"{position}[{index}]",
                )
            )
        elif address in parsed:
            diagnostics.append(_diagnostic("duplicate-address", "The address is duplicated.", f"{position}[{index}]"))
        else:
            parsed.append(address)
    if family == 4 and len(parsed) > 1:
        diagnostics.append(_diagnostic("invalid-addresses", "DHCPv4 permits at most one address.", position))
    return tuple(parsed)


def _parse_prefixes(
    value: Any,
    family: Family | None,
    position: str,
    diagnostics: list[ReservationTransferDiagnostic],
) -> tuple[ipaddress.IPv6Network, ...] | None:
    if not isinstance(value, list):
        diagnostics.append(_diagnostic("invalid-prefixes", "Delegated prefixes must be a list.", position))
        return None
    if family == 4 and value:
        diagnostics.append(_diagnostic("invalid-prefixes", "DHCPv4 does not support delegated prefixes.", position))
        return None
    parsed: list[ipaddress.IPv6Network] = []
    for index, raw in enumerate(value):
        try:
            prefix = ipaddress.IPv6Network(raw, strict=True)
        except (TypeError, ValueError):
            diagnostics.append(
                _diagnostic("invalid-prefix", "The delegated prefix is invalid.", f"{position}[{index}]")
            )
            continue
        if prefix in parsed:
            diagnostics.append(
                _diagnostic("duplicate-prefix", "The delegated prefix is duplicated.", f"{position}[{index}]")
            )
        else:
            parsed.append(prefix)
    return tuple(parsed)


def _parse_options(
    value: Any,
    position: str,
    diagnostics: list[ReservationTransferDiagnostic],
) -> tuple[DHCPOption, ...] | None:
    if not isinstance(value, list):
        diagnostics.append(_diagnostic("invalid-options", "Options must be a list.", position))
        return None
    parsed: list[DHCPOption] = []
    for index, raw in enumerate(value):
        if isinstance(raw, dict):
            raw = {
                "code": raw.get("code"),
                "name": raw.get("name"),
                "space": raw.get("space"),
                "data": raw.get("data", ""),
                "csv-format": raw.get("csv_format"),
                "always-send": raw.get("always_send"),
                "never-send": raw.get("never_send"),
            }
        try:
            parsed.append(parse_dhcp_option(raw))
        except ValueError:
            diagnostics.append(_diagnostic("invalid-option", "The DHCP Option is invalid.", f"{position}[{index}]"))
    return tuple(parsed)


def _parse_record(
    raw: Any,
    index: int,
    diagnostics: list[ReservationTransferDiagnostic],
) -> ReservationImportProposal | None:
    base = f"reservations[{index}]"
    if not isinstance(raw, dict):
        diagnostics.append(_diagnostic("invalid-record", "A Reservation must be an object.", base))
        return None
    family = _parse_family(raw.get("family"), f"{base}.family", diagnostics)
    subnet_cidr = _parse_scope(raw.get("scope"), family, f"{base}.scope", diagnostics)
    identity = _parse_identity(raw.get("identity"), family, f"{base}.identity", diagnostics)
    addresses = _parse_addresses(raw.get("addresses"), family, subnet_cidr, f"{base}.addresses", diagnostics)
    prefixes = _parse_prefixes(raw.get("delegated_prefixes"), family, f"{base}.delegated_prefixes", diagnostics)
    hostname = raw.get("hostname")
    if not isinstance(hostname, str):
        diagnostics.append(_diagnostic("invalid-hostname", "Hostname must be a string.", f"{base}.hostname"))
        hostname = None
    options = _parse_options(raw.get("options"), f"{base}.options", diagnostics)
    if None in (family, subnet_cidr, identity, addresses, prefixes, hostname, options):
        return None
    return ReservationImportProposal(
        family=family,
        subnet_cidr=subnet_cidr,
        identity=identity,
        addresses=addresses,
        delegated_prefixes=prefixes,
        hostname=hostname,
        options=options,
    )


def parse_reservation_document(document: str, format_name: str) -> ReservationTransferResult:
    """Validate a complete transfer document before returning any proposal."""
    raw = _load_document(document, format_name)
    diagnostics: list[ReservationTransferDiagnostic] = []
    if not isinstance(raw, dict):
        return ReservationTransferResult(
            (), (_diagnostic("invalid-document", "Document root must be an object.", "$"),)
        )
    if raw.get("version") != 1:
        diagnostics.append(_diagnostic("invalid-version", "Document version must be 1.", "version"))
    records = raw.get("reservations")
    if not isinstance(records, list):
        diagnostics.append(_diagnostic("invalid-reservations", "Reservations must be a list.", "reservations"))
        return ReservationTransferResult((), tuple(diagnostics))
    indexed_proposals = [
        (index, proposal)
        for index, entry in enumerate(records)
        if (proposal := _parse_record(entry, index, diagnostics)) is not None
    ]
    seen: dict[tuple[Family, str, str, str], int] = {}
    for index, proposal in indexed_proposals:
        key = (
            proposal.family,
            proposal.subnet_cidr,
            proposal.identity.identifier_type,
            proposal.identity.value,
        )
        if key in seen:
            diagnostics.append(
                _diagnostic(
                    "duplicate-reservation",
                    "The Reservation Identity and Scope are duplicated.",
                    f"reservations[{index}].identity",
                )
            )
        else:
            seen[key] = index
    if diagnostics:
        return ReservationTransferResult((), tuple(diagnostics))
    return ReservationTransferResult(tuple(proposal for _, proposal in indexed_proposals), ())
