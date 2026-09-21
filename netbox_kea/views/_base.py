import logging
import re
from typing import Any, TypeVar, cast
from urllib.parse import parse_qsl, urlparse
from urllib.parse import urlencode as _urlencode

import requests
from django.contrib.auth.models import PermissionsMixin
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.http.request import HttpRequest
from netbox.tables import BaseTable

from ..constants import Family
from ..dhcp_options import DHCPOption
from ..kea import KeaException
from ..models import Server
from ..subnet_catalogue import ConfiguredSubnet, VerifiedSubnet

try:
    from utilities.views import ConditionalLoginRequiredMixin
except ImportError:
    from django.contrib.auth.mixins import (  # noqa: F401
        LoginRequiredMixin as ConditionalLoginRequiredMixin,  # type: ignore[assignment]
    )

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseTable)

# Allowed characters in a pool range/CIDR string (digits, dots, colons, letters a-f, slash, hyphen).
# Protects the <path:pool> URL parameter from injection before it reaches the Kea API.
_POOL_RE = re.compile(r"^[0-9a-fA-F.:/-]{3,100}$")


def _strip_empty_params(path: str) -> str:
    """Return *path* with blank query-string parameters removed.

    HTMX 2.x omits empty form values from the browser push URL while still
    sending them in the actual HTTP request.  Using this helper when building
    ``return_url`` ensures the URL we redirect to after bulk-delete matches
    the URL Playwright (and real browsers) see in the address bar.
    """
    parsed = urlparse(path)
    params = parse_qsl(parsed.query, keep_blank_values=False)
    query = _urlencode(params) if params else ""
    return parsed._replace(query=query).geturl()


class _KeaChangeMixin:
    """Mixin that gates a view behind ``netbox_kea.change_server``.

    Applied to all views that mutate live Kea state (reservation/pool/subnet
    add, edit, delete).  Both GET (form display) and POST (form submit) are
    protected so users without write access never see the form.
    """

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login

            return redirect_to_login(request.get_full_path())
        pk = kwargs.get("pk")
        if pk is not None:
            if not Server.objects.restrict(request.user, "view").filter(pk=pk).exists():
                raise Http404
            if not Server.objects.restrict(request.user, "change").filter(pk=pk).exists():
                return HttpResponseForbidden("You do not have permission to modify Kea server data.")
        elif not cast(PermissionsMixin, request.user).has_perm("netbox_kea.change_server"):
            return HttpResponseForbidden("You do not have permission to modify Kea server data.")
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]


def _option_payload(option: DHCPOption) -> dict[str, Any]:
    """Serialize a catalogue option for existing option display formatting."""
    return {
        "data": option.data,
        **{
            key: value
            for key, value in (
                ("code", option.code),
                ("name", option.name),
                ("space", option.space),
                ("csv-format", option.csv_format),
                ("always-send", option.always_send),
                ("never-send", option.never_send),
            )
            if value is not None
        },
    }


def _catalogue_subnet_row(
    subnet: VerifiedSubnet | ConfiguredSubnet,
    server: Server,
    version: Family,
    can_change: bool,
) -> dict[str, Any]:
    """Build one Subnet table row from typed catalogue facts."""
    from ..utilities import format_option_data

    identity = subnet.identity if isinstance(subnet, VerifiedSubnet) else subnet.candidate_identity
    configuration = subnet.configuration
    row = {
        "id": identity.subnet_id,
        "subnet": identity.cidr,
        "_subnet_sort_key": int(identity.network.network_address),
        "dhcp_version": version,
        "server_pk": server.pk,
        "server_name": server.name,
        "identity_verified": isinstance(subnet, VerifiedSubnet),
        "configuration_available": configuration is not None,
        "can_change": can_change and isinstance(subnet, VerifiedSubnet),
        "can_edit_options": can_change and configuration is not None,
        "ddns_qualifying_suffix": configuration.settings.ddns_qualifying_suffix if configuration else None,
        "options": format_option_data(
            [_option_payload(option) for option in configuration.options] if configuration else [],
            version=version,
        ),
        "pools": [pool.range for pool in configuration.pools] if configuration else [],
    }
    if subnet.shared_network is not None:
        row["shared_network"] = subnet.shared_network.name
    return row


def _enrich_subnet_statistics(rows: list[dict[str, Any]], server: Server, version: Family) -> None:
    """Add available utilization measurements to Subnet presentation rows."""
    from ..utilities import parse_subnet_stats

    try:
        client = server.get_client(version=version)
        response = client.command(f"stat-lease{version}-get", service=[f"dhcp{version}"])
        stats = parse_subnet_stats(cast(list[dict[str, Any]], response), version)
        for row in rows:
            if row["id"] in stats:
                row.update(stats[row["id"]])
    except (KeaException, requests.RequestException, KeyError, ValueError, TypeError, RuntimeError):
        logger.debug("stat_cmds hook unavailable or failed", exc_info=True)


def _subnet_option_fields(options: tuple[DHCPOption, ...]) -> dict[str, str]:
    """Project DHCP Option values onto the Subnet form fields."""
    fields = {
        "routers": "gateway",
        "domain-name-servers": "dns_servers",
        "dns-servers": "dns_servers",
        "ntp-servers": "ntp_servers",
        "sntp-servers": "ntp_servers",
    }
    return {fields[option.name]: option.data for option in options if option.name is not None and option.name in fields}


def _unsupported_command(exc: KeaException) -> bool:
    """Return whether Kea rejected an unavailable hook command."""
    return isinstance(exc.response, dict) and exc.response.get("result") == 2
