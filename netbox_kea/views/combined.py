# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
import concurrent.futures
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import render
from django.views import View

from .. import forms, server_configuration, subnet_catalogue, tables
from ..constants import Family, LeaseState
from ..decimal_text import parse_decimal
from ..kea import LeaseQueryGuardError, lease_query_guard_message
from ..leases import LeaseSnapshot
from ..models import Server
from ..reservation_transfer import export_reservation_document
from ..utilities import (
    export_table,
    snapshot_rows,
)
from ._base import ConditionalLoginRequiredMixin, _catalogue_subnet_row, _enrich_subnet_statistics, _shared_network_row
from .fan_out import fan_out
from .leases import LeaseSearch, _enrich_leases_with_badges, lease_sync_gates
from .leases import fetch as fetch_leases
from .notices import HEADLINES, Notice, ServerNotices, notice
from .reservations import ReservationQuery
from .reservations import empty_text as reservation_empty_text
from .reservations import fetch as fetch_reservations
from .reservations import present as present_reservations

logger = logging.getLogger(__name__)


#: The raw Lease records that a read of every Lease takes from each Server; reaching it gives ``page`` coverage.
_COMBINED_MAX_LEASES = 1000


def _server_lease_rows(server: Server, snapshot: LeaseSnapshot) -> list[dict[str, Any]]:
    """Return the presentation rows of one server's Snapshot, tagged with the server."""
    rows = snapshot_rows(snapshot)
    for row in rows:
        row["server_name"] = server.name
        row["server_pk"] = server.pk
    return rows


@dataclass
class _CombinedLeaseRead:
    """The lease rows of several servers and why some servers are missing or partial."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    notices: ServerNotices = field(default_factory=ServerNotices)
    truncated_servers: list[str] = field(default_factory=list)

    def add(self, server: Server, snapshot: LeaseSnapshot) -> None:
        """Add the valid Leases of one server and its Notice."""
        self.rows.extend(_server_lease_rows(server, snapshot))
        self.notices.add(server, notice(snapshot))
        if snapshot.coverage != "exhaustive":
            self.truncated_servers.append(server.name)


def _read_combined_leases(
    servers: list[Server], version: Family, q: Any, by: str | None, state_filter: LeaseState | None
) -> _CombinedLeaseRead:
    """Search each server, or read each one up to its cap for a state-only filter."""
    read = _CombinedLeaseRead()
    if q and by:
        search = LeaseSearch(by, q, state_filter)
    else:
        search = LeaseSearch(state=state_filter, max_leases=_COMBINED_MAX_LEASES)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_leases, s, version, search): s for s in servers}
        for future in concurrent.futures.as_completed(futures):
            server = futures[future]
            # A refused query and a ValueError are outside the notice rule; each keeps its own message.
            try:
                loaded = future.result()
            except LeaseQueryGuardError as exc:
                read.notices.warnings.append((server.name, lease_query_guard_message(exc, state_filter)))
            except ValueError:
                logger.exception("Failed to query server %s", server.name)
                read.notices.errors.append((server.name, HEADLINES["lease"]))
            else:
                if isinstance(loaded, Notice):
                    read.notices.add(server, loaded)
                else:
                    read.add(server, loaded)
    return read


def _selected_server_pks(request: HttpRequest) -> set[int]:
    """Return the Server primary keys that the ``server`` query parameters select."""
    selected = set()
    for text in request.GET.getlist("server"):
        with contextlib.suppress(ValueError):
            selected.add(parse_decimal(text))
    return selected


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
        selected_server_pks = _selected_server_pks(request)
        server_qs = "&".join(f"server={pk}" for pk in sorted(selected_server_pks))
        return {
            "all_servers": all_servers,
            "selected_server_pks": selected_server_pks,
            "server_qs": server_qs,
            "active_tab": self.active_tab,
        }

    def _get_servers(self, request: HttpRequest, dhcp_version: Family) -> list["Server"]:
        """Return servers to query: selected ones if ?server= provided, else all dhcp-flagged."""
        dhcp_kwarg = f"dhcp{dhcp_version}"
        selected_pks = _selected_server_pks(request)
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


def _fetch_subnets_from_server(
    server: "Server",
    version: Family,
) -> tuple[list[dict[str, Any]], Notice | None]:
    """Fetch safe Subnet Catalogue facts for one server, tagged for the combined table, and its Notice."""
    snapshot = subnet_catalogue.display(server, version)
    if snapshot.unavailable:
        return [], notice(snapshot)
    result = [
        _catalogue_subnet_row(subnet, server, version, can_change=False)
        for subnet in (*snapshot.subnets, *snapshot.configured_subnets)
    ]

    _enrich_subnet_statistics(result, server, version)
    return result, notice(snapshot)


class _CombinedSubnetsView(_CombinedViewMixin):
    """Base view: fetch subnets from all selected servers concurrently."""

    template_name = "netbox_kea/combined_subnets.html"
    dhcp_version: Family = 4

    def get(self, request: HttpRequest) -> HttpResponse:
        """Merge subnet lists from all queried servers into one table."""
        ctx = self._combined_context(request)
        servers = self._get_servers(request, self.dhcp_version)

        all_subnets: list[dict[str, Any]] = []
        read = fan_out(servers, "catalogue", lambda server: _fetch_subnets_from_server(server, self.dhcp_version))
        notices = read.notices
        for server, (subnets, found) in read.results:
            all_subnets.extend(subnets)
            notices.add(server, found)

        # Annotate can_change per server so subnet pool/action controls render correctly.
        writable_pks = set(
            Server.objects.restrict(request.user, "change")
            .filter(pk__in=[s.pk for s in servers])
            .values_list("pk", flat=True)
        )
        for subnet in all_subnets:
            can_change = subnet.get("server_pk") in writable_pks
            subnet["can_change"] = bool(subnet.get("identity_verified")) and can_change
            subnet["can_edit_options"] = subnet["can_change"] and bool(subnet.get("configuration_available"))

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
                "errors": notices.errors,
                "warnings": notices.warnings,
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


class _CombinedSharedNetworksView(_CombinedViewMixin):
    """Base view: fetch shared networks from all selected servers concurrently."""

    template_name = "netbox_kea/combined_shared_networks.html"
    dhcp_version: Family = 4

    def get(self, request: HttpRequest) -> HttpResponse:
        """Merge shared network lists from all queried servers into one table."""
        ctx = self._combined_context(request)
        servers = self._get_servers(request, self.dhcp_version)

        all_networks: list[dict[str, Any]] = []
        writable_pks = set(
            Server.objects.restrict(request.user, "change")
            .filter(pk__in=[s.pk for s in servers])
            .values_list("pk", flat=True)
        )

        # An unavailable Server Configuration is a Snapshot with no facts and an error Notice.
        read = fan_out(servers, "configuration", lambda server: server_configuration.display(server, self.dhcp_version))
        notices = read.notices
        for server, snapshot in read.results:
            notices.add(server, notice(snapshot))
            all_networks.extend(
                _shared_network_row(
                    network,
                    server,
                    self.dhcp_version,
                    can_change=server.pk in writable_pks,
                    include_server_name=True,
                )
                for network in snapshot.shared_networks
            )

        table = tables.GlobalSharedNetworkTable(all_networks, user=request.user)
        table.configure(request)

        if "export" in request.GET:
            return export_table(table, filename=f"kea-dhcpv{self.dhcp_version}-shared-networks.csv")

        ctx.update(
            {
                "table": table,
                "errors": notices.errors,
                "warnings": notices.warnings,
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


class _CombinedReservationsView(_CombinedViewMixin):
    """Base view: fetch reservations from all selected servers concurrently."""

    template_name = "netbox_kea/combined_reservations.html"
    dhcp_version: Family = 4

    def get(self, request: HttpRequest) -> HttpResponse:
        """Merge reservation lists from all queried servers into one table."""
        ctx = self._combined_context(request)
        servers = self._get_servers(request, self.dhcp_version)

        is_export = "export" in request.GET
        search_form = forms.ReservationSearchForm(request.GET or None)
        filters = search_form.cleaned_data if search_form.is_valid() else {}
        export_format = request.GET.get("export", "")
        if is_export and export_format not in ("yaml", "json"):
            return HttpResponse("Reservation export format must be YAML or JSON.", status=400)

        if is_export:
            queries = {server.pk: ReservationQuery(full=True) for server in servers}
        else:
            writable_pks = set(
                Server.objects.restrict(request.user, "change")
                .filter(pk__in=[server.pk for server in servers])
                .values_list("pk", flat=True)
            )
            queries = {
                server.pk: ReservationQuery(
                    cursor=request.GET.get(f"reservation_cursor_{server.pk}"),
                    mutations=server.pk in writable_pks,
                    **filters,
                )
                for server in servers
                if request.GET.get(f"reservation_cursor_{server.pk}") != "done"
            }
        read = fan_out(
            [server for server in servers if server.pk in queries],
            "reservation",
            lambda server: fetch_reservations(server, self.dhcp_version, queries[server.pk]),
        )
        notices = read.notices

        if is_export:
            loaded = [server_read.loaded for _server, server_read in read.results]
            snapshots = [item for item in loaded if not isinstance(item, Notice)]
            if notices.errors or len(snapshots) < len(loaded) or any(not snapshot.complete for snapshot in snapshots):
                return HttpResponse(
                    "The combined Reservation Snapshot is incomplete and cannot be exported.", status=409
                )
            records = tuple(record for snapshot in snapshots for record in snapshot.records)
            content = export_reservation_document(records, export_format)
            response = HttpResponse(
                content,
                content_type="application/json" if export_format == "json" else "application/yaml",
            )
            response["Content-Disposition"] = (
                f'attachment; filename="kea-dhcpv{self.dhcp_version}-reservations.{export_format}"'
            )
            return response

        # Present in the main thread so Django ORM queries see the test transaction.
        all_records: list[dict[str, Any]] = []
        mutation_unavailable_servers: list[tuple[str, str]] = []
        snapshots = []
        partial = bool(notices.errors)
        next_query = request.GET.copy()
        next_query.pop("page", None)
        has_next = False
        for server, server_read in read.results:
            presented = present_reservations(request.user, server, server_read, return_url=request.get_full_path())
            notices.add(server, presented.notice)
            all_records.extend(presented.rows)
            partial = partial or presented.partial
            if presented.rows and presented.mutation_unavailable_reason:
                mutation_unavailable_servers.append((server.name, presented.mutation_unavailable_reason))
            if presented.snapshot is None:
                continue
            snapshots.append(presented.snapshot)
            if presented.next_cursor is None:
                next_query[f"reservation_cursor_{server.pk}"] = "done"
            else:
                next_query[f"reservation_cursor_{server.pk}"] = presented.next_cursor
                has_next = True

        table_cls = tables.GlobalReservationTable4 if self.dhcp_version == 4 else tables.GlobalReservationTable6
        table = table_cls(
            all_records,
            user=request.user,
            empty_text=reservation_empty_text(ReservationQuery(**filters).filtered, partial),
        )
        table.configure(request)

        ctx.update(
            {
                "table": table,
                "search_form": search_form,
                "errors": notices.errors,
                "mutation_unavailable_servers": mutation_unavailable_servers,
                "reservation_diagnostics": notices.record_diagnostics,
                "snapshot_complete": not notices.errors and all(snapshot.complete for snapshot in snapshots),
                # A failed Server read is already in the error list, so only a read Snapshot can be incomplete.
                "snapshot_incomplete": any(not snapshot.complete for snapshot in snapshots),
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
    dhcp_version: Family = 4

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

        read = _read_combined_leases(servers, self.dhcp_version, q, by, state_filter)
        all_leases = read.rows

        # Enrich in the main thread so Django ORM queries see the test transaction.
        server_map = {s.pk: s for s in servers}
        sync = lease_sync_gates(request.user)
        for server_pk, server in server_map.items():
            server_leases = [entry for entry in all_leases if entry.get("server_pk") == server_pk]
            if server_leases:
                can_delete = request.user.has_perm("netbox_kea.bulk_delete_lease_from_server", server)
                can_change = request.user.has_perm("netbox_kea.change_server", server)
                _enrich_leases_with_badges(
                    server_leases,
                    server,
                    self.dhcp_version,
                    can_delete=can_delete,
                    can_change=can_change,
                    sync=sync,
                    return_url=request.get_full_path(),
                )

        table = table_cls(all_leases, user=request.user)
        table.configure(request)

        if "export" in request.GET:
            # Each server is a capped or scoped observation, so the export has limited coverage.
            return export_table(
                table,
                filename=f"kea-dhcpv{self.dhcp_version}-leases-limited-coverage.csv",
                use_selected_columns=request.GET["export"] == "table",
            )

        ctx["table"] = table
        ctx["errors"] = read.notices.errors
        ctx["warnings"] = read.notices.warnings
        ctx["truncated_servers"] = read.truncated_servers
        ctx["incomplete_servers"] = read.notices.records
        return render(request, self.template_name, ctx)


class CombinedLeases4View(_CombinedLeasesView):
    """Combined DHCPv4 lease search across all selected servers."""

    dhcp_version = 4
    active_tab = "leases4"


class CombinedLeases6View(_CombinedLeasesView):
    """Combined DHCPv6 lease search across all selected servers."""

    dhcp_version = 6
    active_tab = "leases6"
