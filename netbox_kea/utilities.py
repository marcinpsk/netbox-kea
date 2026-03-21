import csv
import io
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from django.http import HttpResponse
from django.shortcuts import redirect
from django_tables2 import Table
from django_tables2.export import TableExport
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


def _enrich_lease(now: datetime, lease: dict[str, Any]) -> dict[str, Any]:
    """Add expires at, expires in, and state_label to a lease."""
    # Need to replace "-" so we can access the values in a template
    lease = {k.replace("-", "_"): v for k, v in lease.items()}

    # Human-readable state label — map Kea state int to text.
    lease["state_label"] = constants.LEASE_STATE_LABELS.get(lease.get("state"), "Unknown")

    if "cltt" not in lease and "valid_lft" not in lease:
        return lease

    # https://kea.readthedocs.io/en/kea-2.2.0/arm/hooks.html?highlight=cltt#the-lease4-get-lease6-get-commands
    cltt = lease["cltt"]
    valid_lft = lease["valid_lft"]
    if not isinstance(cltt, int) or not isinstance(valid_lft, int):
        logger.warning("Unexpected non-integer cltt/valid_lft in lease: %s", lease.get("ip-address", "?"))
        return lease
    expires_at = datetime.fromtimestamp(cltt + valid_lft)
    lease["expires_at"] = expires_at
    lease["expires_in"] = (expires_at - now).seconds
    lease["cltt"] = datetime.fromtimestamp(cltt)
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
    if not stat_response or stat_response[0].get("result") != 0:
        return {}
    result_set = stat_response[0].get("arguments", {}).get("result-set", {})
    columns: list[str] = result_set.get("columns", [])
    rows: list[list] = result_set.get("rows", [])

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
    result = getattr(exc, "response", {}).get("result", -1) if hasattr(exc, "response") else -1
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
        text = getattr(exc, "response", {}).get("text", "") or ""
        if text:
            return f"Kea reported an error: {text}"
        return "Kea reported an error. Check the server logs for details."
    return f"Kea returned an unexpected result code ({result}). Check the server logs for details."


def parse_reservation_csv(content: str, version: int) -> list[dict[str, Any]]:
    """Parse a CSV string into a list of reservation dicts ready for ``reservation_add``.

    Strips UTF-8 BOM, skips blank lines and lines starting with ``#``.
    Raises ``ValueError`` on missing required fields (message includes 1-indexed row number).

    **v4 required columns**: ``ip-address``, ``hw-address``, ``subnet-id``
    (``hostname`` is optional)

    **v6 required columns**: ``ip-addresses``, ``duid``, ``subnet-id``
    (``hostname`` is optional)

    Args:
        content: Raw CSV text (may include BOM).
        version: DHCP version — ``4`` or ``6``.

    Returns:
        List of dicts suitable for passing to :py:meth:`KeaClient.reservation_add`.

    Raises:
        ValueError: If a required field is missing or empty for any row.

    """
    if version == 4:
        required = {"ip-address", "hw-address", "subnet-id"}
    else:
        required = {"ip-addresses", "duid", "subnet-id"}

    content = content.lstrip("\ufeff")  # strip UTF-8 BOM
    reader = csv.DictReader(
        line.strip() for line in io.StringIO(content) if line.strip() and not line.strip().startswith("#")
    )

    rows: list[dict[str, Any]] = []
    for row_num, raw in enumerate(reader, start=2):  # header is row 1
        row = {k.strip(): v.strip() for k, v in raw.items() if k is not None}

        for field in required:
            if not row.get(field):
                raise ValueError(f"Row {row_num}: missing required field '{field}'")

        result: dict[str, Any] = {"subnet-id": int(row["subnet-id"])}

        if version == 4:
            result["ip-address"] = row["ip-address"]
            result["hw-address"] = row["hw-address"]
        else:
            result["ip-addresses"] = [addr.strip() for addr in row["ip-addresses"].split(";") if addr.strip()]
            result["duid"] = row["duid"]

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
