import concurrent.futures
import ipaddress
import logging
from typing import Any
from urllib.parse import urlencode as _urlencode

from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from .. import constants, forms, tables
from ..models import Server
from ..utilities import (
    _enrich_reservation_sort_key,
    export_table,
    format_leases,
)
from ._base import ConditionalLoginRequiredMixin
from .leases import _enrich_leases_with_badges
from .reservations import _enrich_reservations_with_badges, _filter_reservations

logger = logging.getLogger(__name__)


def _fetch_leases_from_server(server: Server, q: Any, by: str, version: int) -> list[dict[str, Any]]:
    """Fetch leases matching *q*/*by* from a single server and tag with server info.

    Mirrors the logic in ``BaseServerLeasesView.get_leases`` but is a plain
    function so it can be submitted to ``ThreadPoolExecutor`` directly.
    Returns an empty list when the server reports no matching leases (result=3).
    Raises any other exception so the caller can display a per-server error.
    """
    client = server.get_client(version=version)

    arguments: dict[str, Any]
    command_suffix = ""
    multiple = True

    if by == constants.BY_IP:
        arguments = {"ip-address": q}
        multiple = False
    elif by == constants.BY_HW_ADDRESS:
        arguments = {"hw-address": q}
        command_suffix = "-by-hw-address"
    elif by == constants.BY_HOSTNAME:
        arguments = {"hostname": q}
        command_suffix = "-by-hostname"
    elif by == constants.BY_CLIENT_ID:
        arguments = {"client-id": q}
        command_suffix = "-by-client-id"
    elif by == constants.BY_SUBNET_ID:
        command_suffix = "-all"
        arguments = {"subnets": [int(q)]}
    elif by == constants.BY_DUID:
        command_suffix = "-by-duid"
        arguments = {"duid": q}
    else:
        return []

    resp = client.command(
        f"lease{version}-get{command_suffix}",
        service=[f"dhcp{version}"],
        arguments=arguments,
        check=(0, 3),
    )

    if resp[0]["result"] == 3:
        return []

    args = resp[0]["arguments"]
    if args is None:
        return []

    raw = args["leases"] if multiple else [args]
    leases = format_leases(raw)
    for lease in leases:
        lease["server_name"] = server.name
        lease["server_pk"] = server.pk
    return leases


def _fetch_all_leases_from_server(
    server: "Server", version: int, max_leases: int = 1000
) -> tuple[list[dict[str, Any]], bool]:
    """Enumerate all leases on *server* via ``lease{v}-get-page``.

    Paginates from the start address until all leases are fetched or *max_leases*
    is reached.  Leases are tagged with ``server_name`` and ``server_pk``.

    Args:
        server: The Kea server to query.
        version: DHCP version (4 or 6).
        max_leases: Cap on leases collected per server; returns ``truncated=True``
            if more leases exist.

    Returns:
        Tuple of ``(leases, truncated)`` where ``truncated`` is ``True`` when
        the cap was hit and some leases were omitted.

    """
    client = server.get_client(version=version)
    start_ip = "0.0.0.0" if version == 4 else "::"
    per_page = 250

    all_leases: list[dict[str, Any]] = []
    cursor = start_ip
    truncated = False

    while True:
        resp = client.command(
            f"lease{version}-get-page",
            service=[f"dhcp{version}"],
            arguments={"from": cursor, "limit": per_page},
            check=(0, 3),
        )
        if resp[0]["result"] == 3:
            break
        args = resp[0]["arguments"]
        if args is None:
            break
        raw_leases = args["leases"]
        all_leases += format_leases(raw_leases)
        if len(all_leases) >= max_leases:
            truncated = True
            all_leases = all_leases[:max_leases]
            break
        if args["count"] < per_page:
            break
        cursor = raw_leases[-1]["ip-address"]

    for lease in all_leases:
        lease["server_name"] = server.name
        lease["server_pk"] = server.pk
    return all_leases, truncated


class _CombinedViewMixin(ConditionalLoginRequiredMixin, View):
    """Shared mixin for all combined multi-server views.

    Provides:
    - ``active_tab`` class attribute used by the template tab bar
    - ``_combined_context`` — injects all_servers, selected_server_pks, server_qs, active_tab
    - ``_get_servers`` — returns servers to query (all, or selected via ?server=)
    """

    active_tab: str = "overview"

    def _combined_context(self, request: HttpRequest) -> dict[str, Any]:
        """Build context vars shared by every combined view."""
        all_servers = list(Server.objects.restrict(request.user, "view").order_by("name"))
        server_id_strs = request.GET.getlist("server")
        selected_server_pks = {int(pk) for pk in server_id_strs if pk.isdigit()}
        server_qs = "&".join(f"server={pk}" for pk in sorted(selected_server_pks))
        return {
            "all_servers": all_servers,
            "selected_server_pks": selected_server_pks,
            "server_qs": server_qs,
            "active_tab": self.active_tab,
        }

    def _get_servers(self, request: HttpRequest, dhcp_version: int) -> list["Server"]:
        """Return servers to query: selected ones if ?server= provided, else all dhcp-flagged."""
        dhcp_kwarg = f"dhcp{dhcp_version}"
        server_id_strs = request.GET.getlist("server")
        selected_pks = {int(pk) for pk in server_id_strs if pk.isdigit()}
        base_qs = Server.objects.restrict(request.user, "view").filter(**{dhcp_kwarg: True})
        if selected_pks:
            return list(base_qs.filter(pk__in=selected_pks))
        return list(base_qs)


class CombinedDashboardView(_CombinedViewMixin):
    """Combined overview: lists all Kea servers with their configuration summary.

    Intentionally makes no live Kea API calls so the page loads quickly
    regardless of server availability.
    """

    active_tab = "overview"
    template_name = "netbox_kea/combined_overview.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the overview with all configured servers."""
        ctx = self._combined_context(request)
        ctx["page_title"] = "All Kea Servers"
        return render(request, self.template_name, ctx)


def _filter_subnets(subnets: list[dict[str, Any]], q: str, subnet_id: int | None) -> list[dict[str, Any]]:
    """Filter a list of subnet dicts by free-text CIDR query and/or exact subnet ID.

    Filtering is done in-memory because subnets are fetched via config-get (no server-side search).

    Args:
        subnets: List of subnet dicts (keys: id, subnet, server_name, ...).
        q: Free-text query; matched case-insensitively against the ``subnet`` CIDR string.
        subnet_id: If non-None, only subnets with this exact ``id`` are returned.

    """
    result = subnets
    if subnet_id is not None:
        result = [s for s in result if s.get("id") == subnet_id]
    if q:
        q_lower = q.lower()
        result = [s for s in result if q_lower in s.get("subnet", "").lower()]
    return result


def _fetch_subnets_from_server(server: "Server", version: int) -> list[dict[str, Any]]:
    """Fetch all subnets from a single server's config-get response and tag with server info."""
    from ..utilities import format_option_data

    client = server.get_client(version=version)
    config = client.command("config-get", service=[f"dhcp{version}"])
    if config[0]["arguments"] is None:
        raise RuntimeError(f"Unexpected None arguments from config-get for dhcp{version}")
    dhcp_key = f"Dhcp{version}"
    subnet_key = f"subnet{version}"
    args = config[0]["arguments"].get(dhcp_key, {})
    result = [
        {
            "id": s["id"],
            "subnet": s["subnet"],
            "_subnet_sort_key": int(ipaddress.ip_network(s["subnet"], strict=False).network_address),
            "dhcp_version": version,
            "server_pk": server.pk,
            "server_name": server.name,
            "options": format_option_data(s.get("option-data", []), version=version),
            "pools": [p.get("pool", "") for p in s.get("pools", []) if p.get("pool")],
        }
        for s in args.get(subnet_key, [])
        if "id" in s and "subnet" in s
    ]
    for sn in args.get("shared-networks", []):
        result.extend(
            {
                "id": s["id"],
                "subnet": s["subnet"],
                "_subnet_sort_key": int(ipaddress.ip_network(s["subnet"], strict=False).network_address),
                "shared_network": sn["name"],
                "dhcp_version": version,
                "server_pk": server.pk,
                "server_name": server.name,
                "options": format_option_data(s.get("option-data", []), version=version),
                "pools": [p.get("pool", "") for p in s.get("pools", []) if p.get("pool")],
            }
            for s in sn.get(subnet_key, [])
            if "id" in s and "subnet" in s
        )
    # Enrich with utilisation stats when stat_cmds hook is available.
    try:
        stat_resp = client.command(
            f"stat-lease{version}-get",
            service=[f"dhcp{version}"],
        )
        from ..utilities import parse_subnet_stats

        stats = parse_subnet_stats(stat_resp, version)
        for s in result:
            if s["id"] in stats:
                s.update(stats[s["id"]])
    except Exception:  # noqa: BLE001
        logger.debug("stat_cmds hook unavailable or failed", exc_info=True)
    return result


class _CombinedSubnetsView(_CombinedViewMixin):
    """Base view: fetch subnets from all selected servers concurrently."""

    template_name = "netbox_kea/combined_subnets.html"
    dhcp_version: int = 4

    def get(self, request: HttpRequest) -> HttpResponse:
        """Merge subnet lists from all queried servers into one table."""
        ctx = self._combined_context(request)
        servers = self._get_servers(request, self.dhcp_version)

        all_subnets: list[dict[str, Any]] = []
        errors: list[tuple[str, str]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_server = {executor.submit(_fetch_subnets_from_server, s, self.dhcp_version): s for s in servers}
            for future in concurrent.futures.as_completed(future_to_server):
                server = future_to_server[future]
                try:
                    all_subnets.extend(future.result())
                except Exception:  # noqa: BLE001, PERF203
                    logger.exception("Failed to query server %s", server.name)
                    errors.append((server.name, "Failed to query server"))

        # Annotate can_change per server so subnet pool/action controls render correctly.
        writable_pks = set(
            Server.objects.restrict(request.user, "change")
            .filter(pk__in=[s.pk for s in servers])
            .values_list("pk", flat=True)
        )
        for subnet in all_subnets:
            subnet.setdefault("can_change", subnet.get("server_pk") in writable_pks)

        table_cls = tables.GlobalSubnetTable4 if self.dhcp_version == 4 else tables.GlobalSubnetTable6

        search_form = forms.SubnetSearchForm(request.GET or None)
        if search_form.is_valid():
            all_subnets = _filter_subnets(
                all_subnets,
                q=search_form.cleaned_data.get("q", ""),
                subnet_id=search_form.cleaned_data.get("subnet_id"),
            )

        table = table_cls(all_subnets, user=request.user)
        table.configure(request)

        if "export" in request.GET:
            return export_table(table, filename=f"kea-dhcpv{self.dhcp_version}-subnets.csv")

        ctx.update(
            {
                "table": table,
                "search_form": search_form,
                "errors": errors,
                "dhcp_version": self.dhcp_version,
                "page_title": f"DHCPv{self.dhcp_version} Subnets",
            }
        )
        return render(request, self.template_name, ctx)


class CombinedSubnets4View(_CombinedSubnetsView):
    """Combined DHCPv4 subnets across all selected servers."""

    dhcp_version = 4
    active_tab = "subnets4"


class CombinedSubnets6View(_CombinedSubnetsView):
    """Combined DHCPv6 subnets across all selected servers."""

    dhcp_version = 6
    active_tab = "subnets6"


def _fetch_shared_networks_from_server(server: "Server", version: int) -> list[dict[str, Any]]:
    """Fetch all shared networks from a single server's config-get and tag with server info."""
    client = server.get_client(version=version)
    config = client.command("config-get", service=[f"dhcp{version}"])
    if config[0]["arguments"] is None:
        return []
    dhcp_conf = config[0]["arguments"].get(f"Dhcp{version}", {})
    result = []
    for sn in dhcp_conf.get("shared-networks", []):
        subnets = sn.get(f"subnet{version}", [])
        subnet_links = [
            {
                "cidr": s["subnet"],
                "url": (
                    reverse(
                        f"plugins:netbox_kea:server_leases{version}",
                        args=[server.pk],
                    )
                    + "?"
                    + _urlencode({"by": "subnet", "q": s["subnet"]})
                ),
            }
            for s in subnets
            if s.get("subnet")
        ]
        result.append(
            {
                "name": sn.get("name", ""),
                "description": sn.get("description", ""),
                "subnet_count": len(subnets),
                "subnet_links": subnet_links,
                "server_pk": server.pk,
                "server_name": server.name,
                "dhcp_version": version,
            }
        )
    return result


class _CombinedSharedNetworksView(_CombinedViewMixin):
    """Base view: fetch shared networks from all selected servers concurrently."""

    template_name = "netbox_kea/combined_shared_networks.html"
    dhcp_version: int = 4

    def get(self, request: HttpRequest) -> HttpResponse:
        """Merge shared network lists from all queried servers into one table."""
        ctx = self._combined_context(request)
        servers = self._get_servers(request, self.dhcp_version)

        all_networks: list[dict[str, Any]] = []
        errors: list[tuple[str, str]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_server = {
                executor.submit(_fetch_shared_networks_from_server, s, self.dhcp_version): s for s in servers
            }
            for future in concurrent.futures.as_completed(future_to_server):
                server = future_to_server[future]
                try:
                    all_networks.extend(future.result())
                except Exception:  # noqa: BLE001, PERF203
                    logger.exception("Failed to query server %s", server.name)
                    errors.append((server.name, "Failed to query server"))

        # Annotate can_change per server so SharedNetworkTable.actions renders correctly.
        writable_pks = set(
            Server.objects.restrict(request.user, "change")
            .filter(pk__in=[s.pk for s in servers])
            .values_list("pk", flat=True)
        )
        for network in all_networks:
            network.setdefault("can_change", network.get("server_pk") in writable_pks)

        table = tables.GlobalSharedNetworkTable(all_networks, user=request.user)
        table.configure(request)

        if "export" in request.GET:
            return export_table(table, filename=f"kea-dhcpv{self.dhcp_version}-shared-networks.csv")

        ctx.update(
            {
                "table": table,
                "errors": errors,
                "dhcp_version": self.dhcp_version,
                "page_title": f"DHCPv{self.dhcp_version} Shared Networks",
            }
        )
        return render(request, self.template_name, ctx)


class CombinedSharedNetworks4View(_CombinedSharedNetworksView):
    """Combined DHCPv4 shared networks across all selected servers."""

    dhcp_version = 4
    active_tab = "shared_networks4"


class CombinedSharedNetworks6View(_CombinedSharedNetworksView):
    """Combined DHCPv6 shared networks across all selected servers."""

    dhcp_version = 6
    active_tab = "shared_networks6"


def _fetch_reservations_from_server(server: "Server", version: int) -> list[dict[str, Any]]:
    """Fetch all reservations from a single server and tag with server info.

    Paginates automatically using the ``from`` / ``source-index`` tokens returned
    by Kea until the source is exhausted (returned page smaller than the limit).
    """
    service = f"dhcp{version}"
    client = server.get_client(version=version)
    reservations: list[dict[str, Any]] = []
    from_index = 0
    source_index = 0
    while True:
        page, next_from, next_source = client.reservation_get_page(
            service, source_index=source_index, from_index=from_index, limit=100
        )
        for r in page:
            r.setdefault("subnet_id", r.get("subnet-id", 0))
            r.setdefault(
                "ip_address", r.get("ip-address", r.get("ip-addresses", [""])[0] if r.get("ip-addresses") else "")
            )
            r["server_name"] = server.name
            r["server_pk"] = server.pk
            _enrich_reservation_sort_key(r)
        reservations.extend(page)
        if next_from == 0 and next_source == 0:
            break
        from_index = next_from
        source_index = next_source
    return reservations


class _CombinedReservationsView(_CombinedViewMixin):
    """Base view: fetch reservations from all selected servers concurrently."""

    template_name = "netbox_kea/combined_reservations.html"
    dhcp_version: int = 4

    def get(self, request: HttpRequest) -> HttpResponse:
        """Merge reservation lists from all queried servers into one table."""
        ctx = self._combined_context(request)
        servers = self._get_servers(request, self.dhcp_version)

        all_records: list[dict[str, Any]] = []
        errors: list[tuple[str, str]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_server = {
                executor.submit(_fetch_reservations_from_server, s, self.dhcp_version): s for s in servers
            }
            for future in concurrent.futures.as_completed(future_to_server):
                server = future_to_server[future]
                try:
                    all_records.extend(future.result())
                except Exception:  # noqa: BLE001, PERF203
                    logger.exception("Failed to query server %s", server.name)
                    errors.append((server.name, "Failed to query server"))

        # Enrich in the main thread so Django ORM queries see the test transaction.
        server_map = {s.pk: s for s in servers}
        writable_pks = set(
            Server.objects.restrict(request.user, "change")
            .filter(pk__in=list(server_map.keys()))
            .values_list("pk", flat=True)
        )
        for server_pk, server in server_map.items():
            server_records = [r for r in all_records if r.get("server_pk") == server_pk]
            if server_records:
                _enrich_reservations_with_badges(server_records, server, self.dhcp_version)
                can_change = server_pk in writable_pks
                for r in server_records:
                    r["can_change"] = can_change

        search_form = forms.ReservationSearchForm(request.GET or None)
        if search_form.is_valid():
            all_records = _filter_reservations(
                all_records,
                q=search_form.cleaned_data.get("q", ""),
                subnet_id=search_form.cleaned_data.get("subnet_id"),
                version=self.dhcp_version,
            )

        table_cls = tables.GlobalReservationTable4 if self.dhcp_version == 4 else tables.GlobalReservationTable6
        table = table_cls(all_records, user=request.user)
        table.configure(request)

        if "export" in request.GET:
            return export_table(table, filename=f"kea-dhcpv{self.dhcp_version}-reservations.csv")

        ctx.update(
            {
                "table": table,
                "search_form": search_form,
                "errors": errors,
                "dhcp_version": self.dhcp_version,
                "page_title": f"DHCPv{self.dhcp_version} Reservations",
            }
        )
        return render(request, self.template_name, ctx)


class CombinedReservations4View(_CombinedReservationsView):
    """Combined DHCPv4 reservations across all selected servers."""

    dhcp_version = 4
    active_tab = "reservations4"


class CombinedReservations6View(_CombinedReservationsView):
    """Combined DHCPv6 reservations across all selected servers."""

    dhcp_version = 6
    active_tab = "reservations6"


class _CombinedLeasesView(_CombinedViewMixin):
    """Base view: broadcast a lease search query across multiple Kea servers."""

    template_name = "netbox_kea/combined_leases.html"
    dhcp_version: int = 4

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the search form or, when a query is supplied, merge results."""
        search_form_cls = forms.Leases4SearchForm if self.dhcp_version == 4 else forms.Leases6SearchForm
        table_cls = tables.GlobalLeaseTable4 if self.dhcp_version == 4 else tables.GlobalLeaseTable6

        ctx = self._combined_context(request)
        has_query = "q" in request.GET and bool(request.GET.get("q"))
        has_state = "state" in request.GET and request.GET.get("state", "") != ""
        search_form = search_form_cls(request.GET) if (has_query or has_state) else search_form_cls()

        ctx.update(
            {
                "search_form": search_form,
                "dhcp_version": self.dhcp_version,
                "page_title": f"DHCPv{self.dhcp_version} Leases",
            }
        )

        if not has_query and not has_state:
            t = table_cls([], user=request.user)
            t.configure(request)
            if "export" in request.GET:
                return export_table(t, filename=f"kea-dhcpv{self.dhcp_version}-leases.csv")
            ctx["table"] = t
            ctx["errors"] = []
            ctx["truncated_servers"] = []
            return render(request, self.template_name, ctx)

        if not search_form.is_valid():
            t = table_cls([], user=request.user)
            t.configure(request)
            ctx["table"] = t
            ctx["errors"] = []
            ctx["truncated_servers"] = []
            return render(request, self.template_name, ctx)

        q = search_form.cleaned_data.get("q")
        by = search_form.cleaned_data.get("by")
        state_filter = search_form.cleaned_data.get("state")
        servers = self._get_servers(request, self.dhcp_version)

        all_leases: list[dict[str, Any]] = []
        errors: list[tuple[str, str]] = []
        truncated_servers: list[str] = []

        if q and by:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_server = {
                    executor.submit(_fetch_leases_from_server, s, q, by, self.dhcp_version): s for s in servers
                }
                for future in concurrent.futures.as_completed(future_to_server):
                    server = future_to_server[future]
                    try:
                        all_leases.extend(future.result())
                    except Exception:  # noqa: BLE001, PERF203
                        logger.exception("Failed to query server %s", server.name)
                        errors.append((server.name, "Failed to query server"))
        else:
            # State-only filter: enumerate all leases via get-page (capped per server).
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_server = {
                    executor.submit(_fetch_all_leases_from_server, s, self.dhcp_version): s for s in servers
                }
                for future in concurrent.futures.as_completed(future_to_server):
                    server = future_to_server[future]
                    try:
                        leases, was_truncated = future.result()
                        all_leases.extend(leases)
                        if was_truncated:
                            truncated_servers.append(server.name)
                    except Exception:  # noqa: BLE001, PERF203
                        logger.exception("Failed to query server %s", server.name)
                        errors.append((server.name, "Failed to query server"))

        if state_filter is not None:
            all_leases = [ls for ls in all_leases if ls.get("state") == state_filter]

        # Enrich in the main thread so Django ORM queries see the test transaction.
        server_map = {s.pk: s for s in servers}
        for server_pk, server in server_map.items():
            server_leases = [entry for entry in all_leases if entry.get("server_pk") == server_pk]
            if server_leases:
                can_delete = request.user.has_perm("netbox_kea.bulk_delete_lease_from_server", server)
                can_change = request.user.has_perm("netbox_kea.change_server", server)
                _enrich_leases_with_badges(
                    server_leases, server, self.dhcp_version, can_delete=can_delete, can_change=can_change
                )

        table = table_cls(all_leases, user=request.user)
        table.configure(request)

        if "export" in request.GET:
            return export_table(
                table,
                filename=f"kea-dhcpv{self.dhcp_version}-leases.csv",
                use_selected_columns=request.GET["export"] == "table",
            )

        ctx["table"] = table
        ctx["errors"] = errors
        ctx["truncated_servers"] = truncated_servers
        return render(request, self.template_name, ctx)


class CombinedLeases4View(_CombinedLeasesView):
    """Combined DHCPv4 lease search across all selected servers."""

    dhcp_version = 4
    active_tab = "leases4"


class CombinedLeases6View(_CombinedLeasesView):
    """Combined DHCPv6 lease search across all selected servers."""

    dhcp_version = 6
    active_tab = "leases6"
