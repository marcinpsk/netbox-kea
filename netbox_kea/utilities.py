import csv
import io
import ipaddress
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from django.http import HttpResponse
from django.shortcuts import redirect
from django_tables2 import Table
from django_tables2.export import TableExport
from netaddr import EUI, AddrFormatError
from utilities.views import ViewTab

from . import constants
from .models import Server

logger = logging.getLogger(__name__)


def format_duration(s: int | None) -> str | None:
    """Format a duration in seconds as ``HH:MM:SS``, or ``None`` if input is ``None``."""
    if s is None:
        return None
    hours, rest = divmod(s, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def _enrich_reservation_sort_key(reservation: dict[str, Any]) -> dict[str, Any]:
    """Inject a numeric _ip_sort_key into a raw Kea reservation dict (in-place + return).

    The Kea API returns reservation dicts with hyphenated keys (``ip-address``
    for DHCPv4, ``ip-addresses`` for DHCPv6).
    We inject an integer sort key so django-tables2 sorts IPs numerically.
    """
    ip_str = reservation.get("ip-address")
    if not ip_str:
        ip_list = reservation.get("ip-addresses")
        if ip_list and isinstance(ip_list, list):
            ip_str = ip_list[0]
    if ip_str:
        try:
            reservation["_ip_sort_key"] = int(ipaddress.ip_address(ip_str))
        except ValueError:
            pass
    return reservation


def _enrich_lease(now: datetime, lease: dict[str, Any]) -> dict[str, Any]:
    """Add expires at, expires in, state_label, _ip_sort_key, and expiry_class to a lease."""
    # Need to replace "-" so we can access the values in a template
    lease = {k.replace("-", "_"): v for k, v in lease.items()}

    # Human-readable state label — map Kea state int to text.
    lease["state_label"] = constants.LEASE_STATE_LABELS.get(lease.get("state"), "Unknown")

    # F1: inject numeric sort key so django-tables2 sorts IPs as integers, not strings.
    if ip_str := lease.get("ip_address"):
        try:
            lease["_ip_sort_key"] = int(ipaddress.ip_address(ip_str))
        except ValueError:
            pass

    # F10: default expiry CSS class; updated below once we know the expiry time.
    lease["expiry_class"] = ""

    if "cltt" not in lease or "valid_lft" not in lease:
        return lease

    # https://kea.readthedocs.io/en/kea-2.2.0/arm/hooks.html?highlight=cltt#the-lease4-get-lease6-get-commands
    cltt = lease["cltt"]
    valid_lft = lease["valid_lft"]
    if not isinstance(cltt, int) or not isinstance(valid_lft, int):
        logger.warning("Unexpected non-integer cltt/valid_lft in lease: %s", lease.get("ip_address", "?"))
        return lease
    expires_at = datetime.fromtimestamp(cltt + valid_lft)
    lease["expires_at"] = expires_at
    lease["expires_in"] = max(0, int((expires_at - now).total_seconds()))
    lease["cltt"] = datetime.fromtimestamp(cltt)

    # F10: set expiry_class based on how close the lease is to expiring.
    if expires_at < now:
        lease["expiry_class"] = "text-danger"
    elif lease["expires_in"] < 300:
        lease["expiry_class"] = "text-warning"

    return lease


def format_leases(leases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich a list of raw Kea lease dicts with expiry metadata."""
    now = datetime.now()
    return [_enrich_lease(now, ls) for ls in leases]


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


def validate_reservation_identifier(identifier_type: str, value: str) -> str:
    """Validate one client identifier against the syntax its type requires.

    Shared by the reservation forms and the CSV importer so an identifier the
    interactive form rejects cannot be imported through a file instead.  Returns the
    stripped value; the type is assumed to already be valid for the DHCP version.

    Raises:
        ValueError: With a message written for the operator.

    """
    value = value.strip()
    if not value:
        raise ValueError("Enter an identifier value.")
    # Type-aware: a hex identifier's real bound is its octet count, checked below.
    max_length = constants.max_identifier_length(identifier_type)
    if len(value) > max_length:
        raise ValueError(f"Identifier is too long (limit {max_length} characters).")
    if identifier_type == "hw-address":
        try:
            EUI(value, version=48)
        except (AddrFormatError, ValueError) as exc:
            raise ValueError("Enter a valid hardware address (e.g. aa:bb:cc:dd:ee:ff).") from exc
        # netaddr widens a short value instead of rejecting it — 'aa:bb:cc' parses as
        # 00-AA-00-BB-00-CC — so check the operator actually typed 48 bits.
        if len(re.sub(r"[:.-]", "", value)) != 12:
            raise ValueError("Enter a valid hardware address (e.g. aa:bb:cc:dd:ee:ff).")
    elif identifier_type == "duid":
        if not is_hex_string(value, constants.DUID_MIN_OCTETS, constants.DUID_MAX_OCTETS):
            raise ValueError("Enter a valid DUID as colon-separated hex octets.")
    elif identifier_type == "client-id":
        if not is_hex_string(value, constants.CLIENT_ID_MIN_OCTETS, constants.CLIENT_ID_MAX_OCTETS):
            raise ValueError("Enter a valid client-id as colon-separated hex octets (e.g. 01:aa:bb:cc:dd:ee:ff).")
    # circuit-id, flex-id and remote-id are opaque to Kea — length is the only check.
    return value


#: Bounds on a DHCPv6 reservation's delegated-prefix list.  Kea imposes no count
#: limit of its own; these keep a pasted blob from reaching the Kea API as one host.
MAX_DELEGATED_PREFIXES = 16
MAX_PREFIX_INPUT_LENGTH = 1024


def parse_delegated_prefixes(value: str, separator: str = ",") -> list[str]:
    """Parse and validate a delimited list of DHCPv6 delegated prefixes.

    Shared by the reservation form (comma-separated) and the CSV importer
    (semicolon-separated) so both accept exactly the same thing.  Entries are
    canonicalised and de-duplicated, keeping first-seen order.

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


def format_option_data(option_list: list[dict[str, Any]], version: int = 4) -> dict[str, str]:
    """Parse a Kea ``option-data`` list into a friendly ``{name: value}`` dict.

    Well-known DHCP option codes are mapped to canonical names using a
    version-specific lookup table (v4 and v6 share some code numbers with
    different meanings, so the caller must pass the DHCP version).  Unknown codes
    use the option's own ``name`` field (dashes converted to underscores) or
    fall back to ``option_<code>`` when no name is present.

    Args:
        option_list: Raw ``option-data`` list from a Kea response.
        version: DHCP version (4 or 6). Defaults to 4 for backward compatibility.

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


def parse_subnet_stats(stat_response: list[dict[str, Any]], version: int) -> dict[int, dict[str, Any]]:
    """Parse a ``stat-lease{4|6}-get`` response into a per-subnet stats dict.

    Args:
        stat_response: Raw Kea API response list from ``stat-lease4-get`` /
            ``stat-lease6-get``.
        version: DHCP version (4 or 6) — determines which column names to look for.

    Returns:
        ``{subnet_id: {"total": N, "assigned": M, "utilization": "X%"}}`` mapping.
        Returns an empty dict when the response is missing or malformed.

    """
    if not isinstance(stat_response, list) or not stat_response or not isinstance(stat_response[0], dict):
        return {}
    if stat_response[0].get("result") != 0:
        return {}
    arguments = stat_response[0].get("arguments")
    if not isinstance(arguments, dict):
        return {}
    result_set = arguments.get("result-set")
    if not isinstance(result_set, dict):
        return {}
    columns_raw = result_set.get("columns")
    columns: list[str] = columns_raw if isinstance(columns_raw, list) else []
    rows_raw = result_set.get("rows")
    rows: list[list] = rows_raw if isinstance(rows_raw, list) else []

    total_col = "total-addresses" if version == 4 else "total-nas"
    assigned_col = "assigned-addresses" if version == 4 else "assigned-nas"

    try:
        id_idx = columns.index("subnet-id")
        total_idx = columns.index(total_col)
        assigned_idx = columns.index(assigned_col)
    except ValueError:
        return {}

    stats: dict[int, dict[str, Any]] = {}
    min_len = max(id_idx, total_idx, assigned_idx) + 1
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < min_len:
            continue
        try:
            subnet_id = int(row[id_idx])
        except (TypeError, ValueError):
            continue
        try:
            total = int(row[total_idx])
        except (TypeError, ValueError):
            total = 0
        try:
            assigned = int(row[assigned_idx])
        except (TypeError, ValueError):
            assigned = 0
        pct = round(assigned / total * 100) if total > 0 else 0
        stats[subnet_id] = {"total": total, "assigned": assigned, "utilization": f"{pct}%", "utilization_pct": pct}
    return stats


def check_dhcp_enabled(instance: Server, version: Literal[6, 4]) -> HttpResponse | None:
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
    if result == 128:
        return "Cannot reach the Kea daemon. Check that the service is running and the server URL is reachable."
    if result == 0:
        return "Operation reported success."
    if result == 1:
        # host_cmds' reservationAddHandler raises this exact text (see
        # HostCmdsImpl::validateHostForSubnet4/6 in Kea's host_cmds hook) when the
        # reserved address falls outside the subnet's CIDR range. The address and
        # subnet in the message are what the user just submitted, not server internals,
        # so it's safe to surface — unlike the generic str(exc) case this hint avoids.
        text = getattr(exc, "response", {}).get("text", "") or ""
        if (
            "is not matching the ipv4 subnet prefix" in text.lower()
            or "is not matching the ipv6 subnet prefix" in text.lower()
        ):
            return f"{text} — the reserved address must fall inside the subnet's CIDR range."
        return "Kea reported an error. Check the server logs for details."
    return f"Kea returned an unexpected result code ({result}). Check the server logs for details."


def _parse_int_row_field(row: dict, field: str, row_num: int) -> int:
    """Parse ``row[field]`` as int, raising ``ValueError`` with row context on failure."""
    try:
        return int(row[field])
    except (ValueError, KeyError):
        raise ValueError(f"Row {row_num}: '{field}' must be an integer, got '{row.get(field, '')}'") from None


#: Reservation CSV columns that are not identifier columns, per DHCP version.
_RESERVATION_CSV_COLUMNS: dict[int, frozenset[str]] = {
    4: frozenset({"subnet-id", "hostname", "ip-address"}),
    6: frozenset({"subnet-id", "hostname", "ip-addresses", "prefixes"}),
}


def _reservation_csv_identifier(
    row: dict[str, str], row_num: int, identifier_types: tuple[str, ...]
) -> tuple[str, str]:
    """Return the one identifier a reservation row supplies, validated."""
    supplied = [name for name in identifier_types if row.get(name)]
    if not supplied:
        raise ValueError(f"Row {row_num}: no client identifier — supply exactly one of {', '.join(identifier_types)}")
    if len(supplied) > 1:
        # The edit view writes a single identifier key, so importing two would
        # create a host the UI cannot round-trip.
        raise ValueError(
            f"Row {row_num}: ambiguous identifier — {' and '.join(supplied)} were both given, "
            "but a reservation carries exactly one"
        )
    identifier_type = supplied[0]
    try:
        return identifier_type, validate_reservation_identifier(identifier_type, row[identifier_type])
    except ValueError as exc:
        raise ValueError(f"Row {row_num}: invalid {identifier_type} '{row[identifier_type]}': {exc}") from exc


def _reservation_csv_reserved(row: dict[str, str], row_num: int, version: int) -> dict[str, Any]:
    """Return the address/prefix keys a reservation row reserves, if any.

    Empty when the row reserves nothing but a hostname, options or client classes;
    the caller omits the keys rather than sending Kea an empty value.
    """
    if version == 4:
        if not row.get("ip-address"):
            return {}
        try:
            addr = ipaddress.ip_address(row["ip-address"])
        except ValueError:
            raise ValueError(f"Row {row_num}: invalid IPv4 address '{row['ip-address']}'") from None
        if addr.version != 4:
            raise ValueError(f"Row {row_num}: '{row['ip-address']}' is not an IPv4 address")
        return {"ip-address": row["ip-address"]}

    reserved: dict[str, Any] = {}
    ip_addresses = [entry.strip() for entry in row.get("ip-addresses", "").split(";") if entry.strip()]
    for raw_addr in ip_addresses:
        try:
            addr = ipaddress.ip_address(raw_addr)
        except ValueError:
            raise ValueError(f"Row {row_num}: invalid IPv6 address '{raw_addr}'") from None
        if addr.version != 6:
            raise ValueError(f"Row {row_num}: '{raw_addr}' is not an IPv6 address")
    if ip_addresses:
        reserved["ip-addresses"] = ip_addresses
    try:
        prefixes = parse_delegated_prefixes(row.get("prefixes", ""), separator=";")
    except ValueError as exc:
        raise ValueError(f"Row {row_num}: {exc}") from exc
    if prefixes:
        reserved["prefixes"] = prefixes
    return reserved


def parse_reservation_csv(content: str, version: int) -> list[dict[str, Any]]:
    """Parse a CSV string into a list of reservation dicts ready for ``reservation_add``.

    Strips UTF-8 BOM, skips blank lines and lines starting with ``#``.  Every error
    message names the 1-indexed row it came from.

    Each row needs ``subnet-id`` and **exactly one** identifier column from the set
    its DHCP version supports (``hw-address``, ``client-id``, ``circuit-id``,
    ``flex-id``, ``remote-id`` for v4; ``duid`` in place of ``circuit-id`` for v6).
    Exactly one, not at least one: the edit view strips every other identifier key
    before writing, so a multi-identifier host would import into a shape the UI
    cannot round-trip.

    The address is optional.  A v4 host may reserve only a hostname, options or
    client classes, and a v6 host may delegate prefixes without reserving an
    address — Kea accepts both, and demanding one here would make the importer
    stricter than the server it writes to.

    Optional columns: ``hostname``; ``ip-address`` (v4); ``ip-addresses`` and
    ``prefixes`` (v6, both semicolon-separated).  Identifier and prefix validation
    are shared with the interactive forms, so a file cannot import what the form
    would reject.

    Args:
        content: Raw CSV text (may include BOM).
        version: DHCP version — ``4`` or ``6``.

    Returns:
        List of dicts suitable for passing to :py:meth:`KeaClient.reservation_add`.

    Raises:
        ValueError: On an unsupported version, or any row that is missing
            ``subnet-id``, does not carry exactly one identifier, fails validation,
            or supplies a column this parser does not understand.

    """
    if version not in _RESERVATION_CSV_COLUMNS:
        raise ValueError(f"Unsupported DHCP version {version!r} — expected 4 or 6.")

    identifier_types = constants.RESERVATION_IDENTIFIER_TYPES[version]
    known_columns = _RESERVATION_CSV_COLUMNS[version] | set(identifier_types)

    content = content.lstrip("\ufeff")  # strip UTF-8 BOM
    reader = csv.DictReader(
        line.strip() for line in io.StringIO(content) if line.strip() and not line.strip().startswith("#")
    )

    rows: list[dict[str, Any]] = []
    for row_num, raw in enumerate(reader, start=2):  # header is row 1
        # DictReader files cells with no header under the None key.  A filled one carries
        # data this parser never reads, so reject the row; an empty one (trailing comma)
        # loses nothing.
        if any((v or "").strip() for v in raw.get(None) or []):
            raise ValueError(f"Row {row_num}: more values than the header has columns")
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k is not None}

        unknown = sorted(name for name, value in row.items() if value and name not in known_columns)
        if unknown:
            raise ValueError(
                f"Row {row_num}: unrecognised column(s) {', '.join(unknown)} — "
                f"accepted: {', '.join(sorted(known_columns))}"
            )

        if not row.get("subnet-id"):
            raise ValueError(f"Row {row_num}: missing required field 'subnet-id'")
        result: dict[str, Any] = {"subnet-id": _parse_int_row_field(row, "subnet-id", row_num)}

        identifier_type, identifier = _reservation_csv_identifier(row, row_num, identifier_types)
        result[identifier_type] = identifier
        result.update(_reservation_csv_reserved(row, row_num, version))

        if row.get("hostname"):
            result["hostname"] = row["hostname"]

        rows.append(result)

    return rows


def parse_lease_csv(version: int, content: str) -> list[dict[str, Any]]:
    """Parse a CSV string into a list of lease dicts ready for ``lease_add``.

    Strips UTF-8 BOM, skips blank lines and lines starting with ``#``.
    Raises ``ValueError`` on missing required fields.

    **v4 required columns**: ``ip-address``
    Optional: ``hw-address``, ``subnet-id``, ``valid-lft``, ``hostname``

    **v6 required columns**: ``ip-address``, ``duid``, ``iaid``
    Optional: ``subnet-id``, ``valid-lft``, ``hostname``

    Args:
        version: DHCP version — ``4`` or ``6``.
        content: Raw CSV text (may include BOM).

    Returns:
        List of dicts suitable for passing to :py:meth:`KeaClient.lease_add`.

    Raises:
        ValueError: If a required field is missing or empty for any row.

    """
    if version == 4:
        required = {"ip-address"}
    else:
        required = {"ip-address", "duid", "iaid"}

    content = content.lstrip("\ufeff")
    reader = csv.DictReader(
        line.strip() for line in io.StringIO(content) if line.strip() and not line.strip().startswith("#")
    )

    rows: list[dict[str, Any]] = []
    for row_num, raw in enumerate(reader, start=2):
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k is not None}

        for field in required:
            if not row.get(field):
                raise ValueError(f"Row {row_num}: missing required field '{field}'")

        result: dict[str, Any] = {"ip-address": row["ip-address"]}

        try:
            addr = ipaddress.ip_address(row["ip-address"])
        except ValueError:
            raise ValueError(f"Row {row_num}: invalid IP address '{row['ip-address']}'")
        if addr.version != version:
            raise ValueError(f"Row {row_num}: '{row['ip-address']}' is not an IPv{version} address")

        if version == 6:
            if not is_hex_string(row["duid"], constants.DUID_MIN_OCTETS, constants.DUID_MAX_OCTETS):
                raise ValueError(f"Row {row_num}: invalid DUID '{row['duid']}'")
            result["duid"] = row["duid"]
            result["iaid"] = _parse_int_row_field(row, "iaid", row_num)

        if row.get("hw-address") and version == 4:
            if not is_hex_string(row["hw-address"], 6, 6):
                raise ValueError(f"Row {row_num}: invalid MAC address '{row['hw-address']}'")
            result["hw-address"] = row["hw-address"]
        if row.get("subnet-id"):
            result["subnet-id"] = _parse_int_row_field(row, "subnet-id", row_num)
        if row.get("valid-lft"):
            result["valid-lft"] = _parse_int_row_field(row, "valid-lft", row_num)
        if row.get("hostname"):
            result["hostname"] = row["hostname"]

        rows.append(result)

    return rows


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
