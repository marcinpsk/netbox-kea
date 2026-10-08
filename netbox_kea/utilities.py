# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2023-2024 Devon Mar <devon-mar@users.noreply.github.com>
# SPDX-FileCopyrightText: 2026 Andrew Backeby <andrew@backeby.eu>
# SPDX-License-Identifier: Apache-2.0
import csv
import io
import ipaddress
import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from django.http import HttpResponse
from django.shortcuts import redirect
from django_tables2 import Table
from django_tables2.export import TableExport
from pydantic import ValidationError as PydanticValidationError
from utilities.views import ViewTab

from . import constants
from .constants import Family
from .decimal_text import parse_decimal
from .leases import (
    DHCPv4AddressLease,
    DHCPv4LeaseRequest,
    DHCPv6LeaseRequest,
    Lease,
    LeaseDiagnostic,
    LeaseRequest,
    LeaseSnapshot,
    lease_record_data,
    request_errors,
    shown_lease,
)
from .models import Server

logger = logging.getLogger(__name__)


def format_duration(s: int | None) -> str | None:
    """Format a duration in seconds as ``HH:MM:SS``, or ``None`` if input is ``None``."""
    if s is None:
        return None
    hours, rest = divmod(s, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def snapshot_leases(snapshot: LeaseSnapshot, state_filter: int | None) -> tuple[Lease, ...]:
    """Return the valid Leases of *snapshot*, or only those with the Kea state code *state_filter*."""
    if state_filter is None:
        return snapshot.records
    return tuple(lease for lease in snapshot.records if constants.LEASE_STATE_CODES[lease.state] == state_filter)


def snapshot_rows(snapshot: LeaseSnapshot, state_filter: int | None) -> list[dict[str, Any]]:
    """Return the presentation rows of :func:`snapshot_leases`."""
    return lease_rows(snapshot_leases(snapshot, state_filter), evaluated_at=snapshot.evaluated_at)


def diagnostic_reasons(diagnostics: Iterable[LeaseDiagnostic]) -> str:
    """Return each distinct safe reason of *diagnostics* once, in order."""
    return "; ".join(dict.fromkeys(diagnostic.message for diagnostic in diagnostics))


#: The columns of a complete Lease CSV export, by family. The README documents them.
LEASE_CSV_COLUMNS: dict[int, tuple[str, ...]] = {
    family: (
        "family",
        "kind",
        "address",
        "prefix_length",
        "subnet_id",
        "state",
        "current",
        "hostname",
        "valid_lifetime",
        "last_transaction",
        "infinite",
        "expires_at",
        *extra,
    )
    for family, extra in ((4, ("hw_address", "client_id")), (6, ("duid", "iaid", "hw_address", "preferred_lifetime")))
}


def _csv_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else value


def lease_csv_response(
    leases: Iterable[Lease], *, family: Family, evaluated_at: datetime, filename: str
) -> HttpResponse:
    """Return a complete Lease export: the public Lease facts, with no display label or rounding."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    columns = LEASE_CSV_COLUMNS[family]
    writer.writerow(columns)
    for lease in leases:
        data = lease_record_data(lease, evaluated_at=evaluated_at)
        values = {**data, **data["binding"], **data["expiration"], "hw_address": lease.hw_address}
        if not isinstance(lease, DHCPv4AddressLease):
            values["preferred_lifetime"] = lease.preferred_lifetime
        writer.writerow([_csv_value(values[column]) for column in columns])
    return response


def lease_rows(leases: Iterable[Lease], *, evaluated_at: datetime) -> list[dict[str, Any]]:
    """Return the presentation rows of typed Leases, evaluated at the aware time *evaluated_at*.

    Each row keeps its typed Lease under ``lease``; the other keys are display values only.
    """
    return [_lease_row(lease, evaluated_at) for lease in leases]


def _lease_row(lease: Lease, now: datetime) -> dict[str, Any]:
    address = lease.identity.address
    expires_at = lease.expires_at
    expires_in = None if expires_at is None else max(0, int((expires_at - now).total_seconds()))
    expiry_class = ""
    if expires_at is not None and expires_at < now:
        expiry_class = "text-danger"
    elif expires_in is not None and expires_in < 300:
        expiry_class = "text-warning"
    shown = shown_lease(lease)
    row: dict[str, Any] = {
        "lease": lease,
        # The shown facts that a delete of the row compares with a fresh read.
        "selection": shown.model_dump_json(),
        "label": shown.label,
        "ip_address": str(address),
        "_ip_sort_key": int(address),
        "family": lease.family,
        "kind": lease.kind,
        "prefix_length": lease.prefix_length,
        "subnet_id": lease.subnet_id,
        "hostname": lease.hostname,
        "hw_address": lease.hw_address,
        "state_label": constants.LEASE_STATE_LABELS[lease.state],
        "valid_lft": lease.valid_lifetime,
        "cltt": lease.last_transaction,
        "expires_at": expires_at,
        "expires_in": expires_in,
        "expiry_class": expiry_class,
    }
    if isinstance(lease, DHCPv4AddressLease):
        row["client_id"] = lease.client_id
    else:
        row.update({"duid": lease.duid, "iaid": lease.iaid, "preferred_lft": lease.preferred_lifetime})
    return row


def export_table(
    table: Table,
    filename: str,
    use_selected_columns: bool = False,
) -> HttpResponse:
    """Export a django-tables2 table as a CSV HTTP response."""
    exclude_columns = {"pk", "actions"}

    if use_selected_columns:
        exclude_columns |= {name for name, _ in table.available_columns}

    exporter = TableExport(
        export_format=TableExport.CSV,
        table=table,
        exclude_columns=exclude_columns,
    )
    return exporter.response(filename=filename)


def is_hex_string(s: str, min_octets: int, max_octets: int):
    """Return True if *s* is a colon/dash-delimited hex string within the given octet length bounds."""
    if not re.match(constants.HEX_STRING_REGEX, s):
        return False

    octets = len(s.replace(":", "").replace("-", "")) / 2
    return octets >= min_octets and octets <= max_octets


#: Bounds on a DHCPv6 reservation's delegated-prefix list.  Kea imposes no count
#: limit of its own; these keep a pasted blob from reaching the Kea API as one host.
MAX_DELEGATED_PREFIXES = 16
MAX_PREFIX_INPUT_LENGTH = 1024


def parse_delegated_prefixes(value: str, separator: str = ",") -> list[str]:
    """Parse and validate a delimited list of DHCPv6 delegated prefixes.

    Shared by the Reservation domain, forms, and structured transfer parser.
    Entries are canonicalised and de-duplicated in first-seen order.

    Whether a prefix actually belongs to the subnet or one of its PD pools is left to
    Kea: only the subnet *id* is known here, not its configuration, and Kea rejects a
    mismatch with a usable error.

    Raises:
        ValueError: On an entry that is not a canonical IPv6 network of length 1–128,
            or when the input exceeds the length/count bounds above.

    """
    if len(value) > MAX_PREFIX_INPUT_LENGTH:
        raise ValueError(f"Prefix list is too long (limit {MAX_PREFIX_INPUT_LENGTH} characters).")

    prefixes: list[str] = []
    for raw in value.split(separator):
        entry = raw.strip()
        if not entry:
            continue
        if "/" not in entry:
            raise ValueError(f"'{entry}' is not a prefix — expected an IPv6 network such as 2001:db8:1::/64.")
        try:
            # strict=True rejects a prefix with host bits set, e.g. 2001:db8::1/64.
            network = ipaddress.ip_network(entry, strict=True)
        except ValueError as exc:
            raise ValueError(f"'{entry}' is not a valid IPv6 prefix: {exc}") from exc
        if network.version != 6:
            raise ValueError(f"'{entry}' is not an IPv6 prefix.")
        if network.prefixlen == 0:
            # ::/0 is the whole address space, not something Kea can delegate.
            raise ValueError(f"'{entry}' has no prefix length — a delegated prefix is /1 to /128.")
        canonical = str(network)
        if canonical not in prefixes:
            prefixes.append(canonical)
    if len(prefixes) > MAX_DELEGATED_PREFIXES:
        raise ValueError(f"At most {MAX_DELEGATED_PREFIXES} delegated prefixes per reservation.")
    return prefixes


_KNOWN_CODES_V4: dict[int, str] = {
    1: "subnet_mask",
    3: "gateway",
    6: "dns_servers",
    15: "domain_name",
    28: "broadcast_address",
    42: "ntp_servers",
    44: "netbios_name_servers",
    119: "domain_search",
    121: "classless_static_routes",
}
_KNOWN_CODES_V6: dict[int, str] = {
    23: "dns_servers",
    24: "domain_search",
    31: "ntp_servers",
}


def format_option_data(option_list: list[dict[str, Any]], version: Family) -> dict[str, str]:
    """Parse a Kea ``option-data`` list into a friendly ``{name: value}`` dict.

    Well-known DHCP option codes are mapped to canonical names using a
    version-specific lookup table (v4 and v6 share some code numbers with
    different meanings, so the caller must pass the DHCP version).  Unknown codes
    use the option's own ``name`` field (dashes converted to underscores) or
    fall back to ``option_<code>`` when no name is present.

    Args:
        option_list: Raw ``option-data`` list from a Kea response.
        version: DHCP version (4 or 6). v4 and v6 reuse option codes with different
            meanings, so the caller must say which family the list came from.

    Returns:
        A ``{field_name: value_str}`` dict suitable for template rendering.

    """
    known_codes = _KNOWN_CODES_V6 if version == 6 else _KNOWN_CODES_V4

    result: dict[str, str] = {}
    for opt in option_list:
        code = opt.get("code")
        data = opt.get("data", "")
        if code in known_codes:
            key = known_codes[code]
        elif opt.get("name"):
            key = opt["name"].replace("-", "_")
        else:
            key = f"option_{code}"
        result[key] = data
    return result


def check_dhcp_enabled(instance: Server, version: Family) -> HttpResponse | None:
    """Return a redirect to the server detail page if the requested DHCP version is disabled, else ``None``."""
    if (version == 6 and instance.dhcp6) or (version == 4 and instance.dhcp4):
        return None
    return redirect(instance.get_absolute_url())


def kea_error_hint(exc: Any) -> str:
    """Return a human-readable hint for a :exc:`~netbox_kea.kea.KeaException`.

    Maps Kea result codes to actionable messages so users see something useful
    instead of a generic "see server logs" error.

    Result codes:
        0  — success (should not normally be an error)
        1  — generic error
        2  — command not supported (hook library not loaded)
        3  — empty result / not found
        4  — conflict with the server state (lease_cmds: the lease exists, or a concurrent change)
        128 — service not connected / daemon unreachable
    """
    result = getattr(exc, "response", {}).get("result", -1)
    if result == 2:
        return (
            "This command is not supported by the Kea server. "
            "The required hook library may not be loaded (e.g. host_cmds, lease_cmds, subnet_cmds)."
        )
    if result == 3:
        return "No matching records found in Kea."
    if result == 4:
        return (
            "The change conflicts with the current state of the Kea server: for example, the lease already exists,"
            " or another request changed it at the same time. Check the current state and try again."
        )
    if result == 128:
        return "Cannot reach the Kea daemon. Check that the service is running and the server URL is reachable."
    if result == 0:
        return "Operation reported success."
    if result == 1:
        # host_cmds' reservationAddHandler raises this text (see
        # HostCmdsImpl::validateHostForSubnet4/6 in Kea's host_cmds hook) when the
        # reserved address falls outside the subnet's CIDR range.
        text = getattr(exc, "response", {}).get("text", "") or ""
        if (
            "is not matching the ipv4 subnet prefix" in text.lower()
            or "is not matching the ipv6 subnet prefix" in text.lower()
        ):
            return "The reserved IP address is outside the subnet's CIDR range."
        return "Kea reported an error. Check the server logs for details."
    return f"Kea returned an unexpected result code ({result}). Check the server logs for details."


def parse_lease_csv(version: Family, content: str) -> list[tuple[int, LeaseRequest]]:
    """Parse a lease CSV file into typed creation requests, each with its row number.

    Strips a UTF-8 BOM, and skips blank lines and lines that start with ``#``.

    **v4 required columns**: ``ip-address``, ``hw-address``.
    **v6 required columns**: ``ip-address``, ``duid``, ``iaid``.
    **Optional columns**: ``subnet-id``, ``valid-lft``, ``hostname``.

    Raises:
        ValueError: For the first row that is not a valid request. The message names the row and the
            column, never the value.

    """
    fields = {"ip-address": "address", "subnet-id": "subnet_id", "valid-lft": "valid_lifetime", "hostname": "hostname"}
    if version == 4:
        fields["hw-address"] = "hw_address"
    else:
        fields.update({"duid": "duid", "iaid": "iaid"})
    required = ("ip-address", "hw-address") if version == 4 else ("ip-address", "duid", "iaid")
    integers = {"subnet-id", "valid-lft", "iaid"}
    columns = {field: column for column, field in fields.items()}
    model = DHCPv4LeaseRequest if version == 4 else DHCPv6LeaseRequest

    content = content.lstrip("\ufeff")
    reader = csv.DictReader(
        line.strip() for line in io.StringIO(content) if line.strip() and not line.strip().startswith("#")
    )
    parsed: list[tuple[int, LeaseRequest]] = []
    for row_num, raw in enumerate(reader, start=2):
        row = {key.strip(): (value or "").strip() for key, value in raw.items() if key is not None}
        for column in required:
            if not row.get(column):
                raise ValueError(f"Row {row_num}: missing required field '{column}'.")
        values: dict[str, Any] = {}
        for column, field in fields.items():
            text = row.get(column, "")
            if not text:
                continue
            if column in integers:
                try:
                    values[field] = parse_decimal(text)
                except ValueError:
                    raise ValueError(f"Row {row_num}: '{column}' must be an integer.") from None
            elif column == "ip-address":
                try:
                    values[field] = ipaddress.ip_address(text)
                except ValueError:
                    raise ValueError(f"Row {row_num}: '{column}' is not an IPv{version} address.") from None
            else:
                values[field] = text
        try:
            parsed.append((row_num, model.model_validate(values)))
        except PydanticValidationError as exc:
            refused, _message = request_errors(exc)[0]
            column = columns.get(refused or "", "row")
            raise ValueError(f"Row {row_num}: '{column}' is not valid for a DHCPv{version} lease.") from None
    return parsed


class OptionalViewTab(ViewTab):
    """A NetBox ViewTab that can be conditionally hidden based on a predicate."""

    def __init__(self, *args, is_enabled: Callable[[Any], bool], **kwargs) -> None:
        """Initialise with an ``is_enabled`` callable that receives the view instance."""
        self.is_enabled = is_enabled
        super().__init__(*args, **kwargs)

    def render(self, instance):
        """Return rendered tab HTML, or ``None`` if the tab is disabled for *instance*."""
        if self.is_enabled(instance):
            return super().render(instance)
        return None
