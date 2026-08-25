import csv
import io
import ipaddress
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import requests
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect
from django_tables2 import Table
from django_tables2.export import TableExport
from netaddr import AddrFormatError, IPNetwork
from utilities.views import ViewTab

from . import constants
from .kea import KeaException
from .models import Server

logger = logging.getLogger(__name__)


def format_duration(s: int | None) -> str | None:
    """Format a duration in seconds as ``HH:MM:SS``, or ``None`` if input is ``None``."""
    if s is None:
        return None
    hours, rest = divmod(s, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def subnet_sort_key(choice: tuple[str, Any]) -> tuple[int, Any]:
    """Sort key that orders ``(cidr, ...)`` subnet choices by network (address then prefix).

    Falls back to the CIDR string for anything netaddr can't parse, kept in a separate
    bucket so the two key types are never compared against each other.
    """
    cidr = choice[0]
    if not isinstance(cidr, str):
        return (1, str(cidr))
    try:
        return (0, IPNetwork(cidr))
    except (AddrFormatError, ValueError, TypeError):
        return (1, cidr)


def _subnet_choices_cache_key(server: Server, version: int) -> str:
    """Cache key for one server's subnet list, scoped per DHCP version.

    A dual-stack server routes v4 and v6 to different daemons with different subnets,
    so the version must be part of the key.
    """
    return f"netbox_kea:subnet_choices:{server.pk}:{version}"


def fetch_subnet_choices(server: Server, version: int) -> tuple[list[tuple[str, int]], bool]:
    """Return ``(choices, subnet_cmds_available)`` for the server's subnet datalists.

    ``choices`` is ``[(cidr, subnet_id), ...]`` in network order. Both datalists that
    offer subnets read it, so both describe the same set:

    * the lease-search Search combobox, where the CIDR drives a ``by=subnet`` search
      and the id a ``by=subnet_id`` search, so both halves are needed;
    * the reservation add form's Subnet CIDR field, whose POST resolves the submitted
      CIDR back to an id through the same ``subnet_cmds`` source, so the form cannot
      suggest a subnet that saving would then fail to resolve.

    ``subnet_cmds_available`` is False only when Kea reports ``subnet{v}-list``
    unsupported, which is what the two templates warn about. Any other failure degrades
    to no suggestions and leaves the field usable by typing, because a suggestion list is
    a convenience and must never break the page.

    Successful results (including a legitimately empty list) are cached for
    ``constants.SUBNET_CHOICES_TTL`` seconds per server+version, so HTMX paginations and
    form re-renders reuse them. Failures are not cached, so loading the hook takes effect
    on the next render instead of after the TTL.
    """
    cache_key = _subnet_choices_cache_key(server, version)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        choices = server.get_client(version=version).list_subnets(version)
    except KeaException as exc:
        if exc.response.get("result") == 2:
            return [], False
        logger.debug("Could not list dhcp%s subnets on server %s", version, server.pk, exc_info=True)
        return [], True
    except (requests.RequestException, ValueError, RuntimeError):
        logger.debug("Could not list dhcp%s subnets on server %s", version, server.pk, exc_info=True)
        return [], True
    choices.sort(key=subnet_sort_key)
    result = (choices, True)
    cache.set(cache_key, result, constants.SUBNET_CHOICES_TTL)
    return result


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


def _parse_int_row_field(row: dict, field: str, row_num: int) -> int:
    """Parse ``row[field]`` as int, raising ``ValueError`` with row context on failure."""
    try:
        return int(row[field])
    except (ValueError, KeyError):
        raise ValueError(f"Row {row_num}: '{field}' must be an integer, got '{row.get(field, '')}'") from None


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
