import concurrent.futures
import logging
from typing import Any, Literal
from urllib.parse import urlencode as _urlencode

import requests
from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from .. import constants, forms, tables
from ..dhcp_options import DHCPOption
from ..kea import KeaException, LeaseQueryGuardError, lease_query_guard_message
from ..models import Server
from ..reservation_transfer import export_reservation_document
from ..subnet_catalogue import ConfiguredSubnet, Diagnostic, VerifiedSubnet, display
from ..utilities import (
    export_table,
    format_leases,
)
from ._base import ConditionalLoginRequiredMixin
from .leases import _enrich_leases_with_badges
from .reservations import (
    _attach_reservation_action_urls,
    _configured_capabilities,
    _enrich_reservations_with_badges,
    _fetch_reservation_page,
    _fetch_reservation_snapshot,
    _filter_reservations,
    _reservation_table_record,
)

logger = logging.getLogger(__name__)


def _require_first_entry(resp: Any, what: str) -> dict[str, Any]:
    """Validate a Kea command-response shape and return its first entry.

    ``KeaClient.command`` only guarantees a list, and ``check_response`` iterating
    an empty list raises nothing — so indexing ``resp[0]`` on a malformed (empty or
    non-dict) payload blows up with ``IndexError``/``TypeError``. Surface it as a
    ``RuntimeError`` (the contract callers already catch) instead, matching the
    guard the subnet/option/server views use.
    """
    if not isinstance(resp, list) or not resp or not isinstance(resp[0], dict):
        raise RuntimeError(f"Malformed Kea response for {what}")
    return resp[0]


def _fetch_leases_from_server(
    server: Server,
    q: Any,
    by: str,
    version: int,
    *,
    state: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch leases matching *q*/*by* from one server and tag them with server details."""
    client = server.get_client(version=version)
    value = str(q.cidr) if by == constants.BY_SUBNET else q
    leases = format_leases(client.lease_search(version, by, value, state=state))
    for lease in leases:
        lease["server_name"] = server.name
        lease["server_pk"] = server.pk
    return leases


def _fetch_all_leases_from_server(
    server: "Server", version: int, max_leases: int = 1000
) -> tuple[list[dict[str, Any]], bool]:
    """Enumerate all leases on *server* through the Kea client.

    Fetches leases until the collection is complete or *max_leases* is reached.
    Leases are tagged with ``server_name`` and ``server_pk``.

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
    collection = client.lease_get_all(version, max_leases=max_leases)
    all_leases = format_leases(collection.leases)

    for lease in all_leases:
        lease["server_name"] = server.name
        lease["server_pk"] = server.pk
    return all_leases, collection.truncated


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
    version: int,
) -> dict[str, Any]:
    """Build one combined-table row from a typed catalogue subnet."""
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


def _fetch_subnets_from_server(
    server: "Server",
    version: int,
) -> tuple[list[dict[str, Any]], tuple[Diagnostic, ...]]:
    """Fetch safe Subnet Catalogue facts for one server and tag them for the combined table."""
    snapshot = display(server, version)
    if snapshot.unavailable:
        raise RuntimeError(f"Subnet Catalogue is unavailable for DHCPv{version}")
    result = [
        _catalogue_subnet_row(subnet, server, version) for subnet in (*snapshot.subnets, *snapshot.configured_subnets)
    ]

    # Enrich with utilisation stats when stat_cmds hook is available.
    try:
        client = server.get_client(version=version)
        stat_resp = client.command(
            f"stat-lease{version}-get",
            service=[f"dhcp{version}"],
        )
        from ..utilities import parse_subnet_stats

        stats = parse_subnet_stats(stat_resp, version)
        for s in result:
            if s["id"] in stats:
                s.update(stats[s["id"]])
    except (KeaException, requests.RequestException, KeyError, ValueError, TypeError, RuntimeError):
        logger.debug("stat_cmds hook unavailable or failed", exc_info=True)
    return result, snapshot.diagnostics


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
                    subnets, diagnostics = future.result()
                    all_subnets.extend(subnets)
                    errors.extend(
                        (server.name, message)
                        for message in dict.fromkeys(diagnostic.message for diagnostic in diagnostics)
                    )
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
            subnet.setdefault(
                "can_change",
                bool(subnet.get("identity_verified")) and subnet.get("server_pk") in writable_pks,
            )

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
    entry = _require_first_entry(config, f"config-get for dhcp{version}")
    if entry["arguments"] is None:
        raise RuntimeError(f"Unexpected None arguments from config-get for dhcp{version}")
    dhcp_conf = entry["arguments"].get(f"Dhcp{version}", {})
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


def _fetch_reservations_from_server(
    server: "Server",
    version: int,
    cursor: str | None = None,
    *,
    full_snapshot: bool = False,
):
    """Fetch one bounded page, or a complete typed Snapshot for transfer."""
    if full_snapshot:
        return _fetch_reservation_snapshot(server, version)
    return _fetch_reservation_page(server, version, cursor)


class _CombinedReservationsView(_CombinedViewMixin):
    """Base view: fetch reservations from all selected servers concurrently."""

    template_name = "netbox_kea/combined_reservations.html"
    dhcp_version: Literal[4, 6] = 4

    def get(self, request: HttpRequest) -> HttpResponse:
        """Merge reservation lists from all queried servers into one table."""
        ctx = self._combined_context(request)
        servers = self._get_servers(request, self.dhcp_version)

        all_records: list[dict[str, Any]] = []
        errors: list[tuple[str, str]] = []
        diagnostics = []
        snapshots = {}
        is_export = "export" in request.GET
        export_format = request.GET.get("export")
        if is_export and export_format not in ("yaml", "json"):
            return HttpResponse("Reservation export format must be YAML or JSON.", status=400)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_server = {
                executor.submit(
                    _fetch_reservations_from_server,
                    server,
                    self.dhcp_version,
                    None if is_export else request.GET.get(f"reservation_cursor_{server.pk}"),
                    full_snapshot=is_export,
                ): server
                for server in servers
                if is_export or request.GET.get(f"reservation_cursor_{server.pk}") != "done"
            }
            for future in concurrent.futures.as_completed(future_to_server):
                server = future_to_server[future]
                try:
                    snapshot = future.result()
                    snapshots[server.pk] = snapshot
                    all_records.extend(_reservation_table_record(record, server) for record in snapshot.records)
                    diagnostics.extend((server.name, diagnostic) for diagnostic in snapshot.diagnostics)
                except Exception:  # noqa: BLE001, PERF203
                    logger.exception("Failed to query server %s", server.name)
                    errors.append((server.name, "Failed to query server"))

        if is_export:
            if errors or diagnostics:
                return HttpResponse(
                    "The combined Reservation Snapshot is incomplete and cannot be exported.", status=409
                )
            records = tuple(
                record
                for server in servers
                if (snapshot := snapshots.get(server.pk)) is not None
                for record in snapshot.records
            )
            content = export_reservation_document(records, export_format)
            response = HttpResponse(
                content,
                content_type="application/json" if export_format == "json" else "application/yaml",
            )
            response["Content-Disposition"] = (
                f'attachment; filename="kea-dhcpv{self.dhcp_version}-reservations.{export_format}"'
            )
            return response

        # Enrich in the main thread so Django ORM queries see the test transaction.
        server_map = {s.pk: s for s in servers}
        writable_pks = set(
            Server.objects.restrict(request.user, "change")
            .filter(pk__in=list(server_map.keys()))
            .values_list("pk", flat=True)
        )
        mutation_unavailable_servers: list[tuple[str, str]] = []
        for server_pk, server in server_map.items():
            server_records = [r for r in all_records if r.get("server_pk") == server_pk]
            if server_records:
                _enrich_reservations_with_badges(server_records, server, self.dhcp_version)
                can_change = server_pk in writable_pks
                capabilities = _configured_capabilities(server, self.dhcp_version) if can_change else None
                can_mutate = bool(can_change and capabilities and capabilities.mutation_available)
                if can_change and not can_mutate:
                    reason = (
                        capabilities.explanation
                        if capabilities is not None and capabilities.explanation
                        else "Live Reservation mutation capabilities could not be confirmed."
                    )
                    mutation_unavailable_servers.append((server.name, reason))
                for r in server_records:
                    r["can_change"] = can_mutate
                # Per-server so a row can never be given another server's URL.
                _attach_reservation_action_urls(
                    server_records,
                    server_pk,
                    self.dhcp_version,
                    can_change=can_mutate,
                )

        search_form = forms.ReservationSearchForm(request.GET or None)
        if search_form.is_valid():
            all_records = _filter_reservations(
                all_records,
                q=search_form.cleaned_data.get("q", ""),
                subnet_id=search_form.cleaned_data.get("subnet_id"),
                version=self.dhcp_version,
                scope=search_form.cleaned_data.get("scope", ""),
            )

        table_cls = tables.GlobalReservationTable4 if self.dhcp_version == 4 else tables.GlobalReservationTable6
        table = table_cls(all_records, user=request.user)
        table.configure(request)

        next_query = request.GET.copy()
        next_query.pop("page", None)
        has_next = False
        for server in servers:
            snapshot = snapshots.get(server.pk)
            cursor_key = f"reservation_cursor_{server.pk}"
            if snapshot is None:
                continue
            if snapshot.next_cursor is None:
                next_query[cursor_key] = "done"
            else:
                next_query[cursor_key] = snapshot.next_cursor
                has_next = True

        ctx.update(
            {
                "table": table,
                "search_form": search_form,
                "errors": errors,
                "mutation_unavailable_servers": mutation_unavailable_servers,
                "reservation_diagnostics": diagnostics,
                "snapshot_complete": not errors and all(snapshot.complete for snapshot in snapshots.values()),
                "next_page_url": f"{request.path}?{next_query.urlencode()}" if has_next else None,
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
            state_in_kea = state_filter if by in (constants.BY_SUBNET, constants.BY_SUBNET_ID) else None
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_server = {
                    executor.submit(
                        _fetch_leases_from_server,
                        s,
                        q,
                        by,
                        self.dhcp_version,
                        state=state_in_kea,
                    ): s
                    for s in servers
                }
                for future in concurrent.futures.as_completed(future_to_server):
                    server = future_to_server[future]
                    try:
                        all_leases.extend(future.result())
                    except LeaseQueryGuardError as exc:  # noqa: PERF203
                        errors.append((server.name, lease_query_guard_message(exc, state_filter)))
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

        if state_filter is not None and by not in (constants.BY_SUBNET, constants.BY_SUBNET_ID):
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
