# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
import concurrent.futures
import ipaddress
import logging
import threading
import uuid
from abc import ABCMeta
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from functools import partial
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlencode as _urlencode

import requests
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseForbidden
from django.http.request import HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from extras.models import JournalEntry
from netbox.tables import BaseTable
from netbox.views import generic
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.views import GetReturnURLMixin, register_model_view

from .. import constants, event_scope, forms, subnet_catalogue, tables
from ..constants import Family, LeaseState
from ..ipam_reconciliation import LEASE, LEASE_PREFIX, claim, claim_permissions
from ..kea import (
    KeaClient,
    KeaException,
    LeaseQueryGuardError,
    LeaseQueryUnknownSubnet,
    lease_query_guard_message,
)
from ..leases import (
    AllocationKind,
    DHCPv4AddressLease,
    DHCPv4Binding,
    DHCPv4LeaseRequest,
    DHCPv6Binding,
    DHCPv6LeaseRequest,
    DHCPv6PrefixLease,
    Lease,
    LeaseAbsent,
    LeaseChanged,
    LeaseChangeRefused,
    LeaseChangeResult,
    LeaseConflict,
    LeaseEdit,
    LeaseFact,
    LeaseFound,
    LeaseRequest,
    LeaseSnapshot,
    ShownLease,
    address_identity,
    creation_mismatches,
    is_current,
    parse_selection,
    shown_lease,
)
from ..models import Server
from ..published_name import lease_published_name
from ..reservations import (
    GlobalReservationScope,
    InSubnetReservationScope,
    Reservation,
    lease_identities,
)
from ..signals import lease_added, leases_deleted
from ..subnet_catalogue import CatalogueUnavailable, VerifiedSubnet
from ..sync_permissions import SyncGate, sync_gate
from ..utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    diagnostic_reasons,
    export_table,
    kea_error_hint,
    lease_csv_response,
    snapshot_leases,
    snapshot_rows,
)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin, _safe_return_url, _strip_empty_params

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseTable)
_LEASE_EXPORT_MAX_LEASES = 50_000


def lease_sync_gates(user: Any) -> dict[AllocationKind, SyncGate]:
    """Apply the manual Sync rule to *user* for the claim of each Lease kind."""
    return {
        "address": sync_gate(user, claim_permissions(LEASE)),
        "delegated-prefix": sync_gate(user, claim_permissions(LEASE_PREFIX)),
    }


def _read_created_lease(request: HttpRequest, client: KeaClient, creation: LeaseRequest) -> Lease | None:
    """Read the Lease that Kea just created and check it against *creation*.

    Return ``None`` when the read fails, finds nothing, reads a malformed Lease or reads one that
    contradicts the request.
    """
    address = str(creation.address)
    try:
        result = client.lease_get(address_identity(creation.family, address))
    except (KeaException, requests.RequestException, RuntimeError, ValueError):
        logger.exception("Kea created lease %s, but the read back failed", address)
        return None
    if not isinstance(result, LeaseFound):
        logger.warning("Kea created lease %s, but the read back did not return it (%s)", address, result.outcome)
        return None
    mismatches = creation_mismatches(creation, result.lease)
    if mismatches:
        facts = _fact_words(mismatches)
        logger.warning("Kea created lease %s, but the read back has another %s", address, facts)
        messages.warning(
            request, f"Lease {address} created, but the lease that Kea returned does not match the request ({facts})."
        )
        return None
    return result.lease


def _run_lease_sync_to_netbox(
    request: HttpRequest, server: Server, address: str, created: Lease | None, gate: SyncGate
) -> None:
    """Sync a just-created lease to NetBox IPAM from its fresh read back, when *gate* allows it.

    *gate* is the manual Sync rule of a lease claim; server-edit access alone is not enough. Only a Current Lease
    is claimed, also when the form supplied a Subnet. The sync uses ``force=False``, so a
    foreign (non-Kea-managed) NetBox IP is skipped rather than overwritten; that skip is
    reported as a warning instead of a misleading "synced" message. Queues a
    success/warning message; never raises.
    """
    if not gate.allowed:
        messages.warning(request, f"Lease created, but it was not synced to NetBox. {gate.reason}")
        return
    if created is None:
        messages.warning(request, "Lease created but NetBox IPAM sync failed; see server logs.")
        return
    if not is_current(created, datetime.now(tz=timezone.utc)):
        messages.warning(
            request, "Lease created, but it was not synced to NetBox: Kea did not report it as a current lease."
        )
        return
    try:
        result = claim(server, created.family, [created], force=False)
        outcome = next(iter(result.addresses.values())).outcome
        if outcome == "error":
            messages.warning(request, "Lease created but NetBox IPAM sync failed; see server logs.")
        elif outcome == "disagreement":
            messages.warning(request, f"Lease created, but NetBox IPAM owners disagree about {address}.")
        elif outcome == "conflict":
            messages.warning(
                request,
                f"Lease created, but NetBox IPAM sync was skipped: {address} already exists and is not Kea-managed,"
                " or its note leaves no room for the new sync marker.",
            )
        else:
            nb_action = outcome if outcome in {"created", "updated"} else "already up to date"
            messages.success(request, f"IPAddress {address} {nb_action} in NetBox.")
    except event_scope.EventDispatchError:
        raise
    except (CatalogueUnavailable, RuntimeError, OSError, ValueError, DatabaseError, ValidationError):
        logger.exception("Failed to sync lease %s to NetBox", address)
        messages.warning(request, "Lease created but NetBox IPAM sync failed; see server logs.")


def _add_lease_journal(
    server: "Server",
    user: Any,
    action: str,
    ip_addresses: "list[str] | str",
    hw_address: str = "",
    hostname: str = "",
    duid: str = "",
) -> None:
    """Create a JournalEntry on *server* recording a lease CRUD event.

    Args:
        server: The Server instance the journal entry is attached to.
        user: The request.user who performed the action.
        action: Human-readable action name: "added" or "deleted".
        ip_addresses: A single IP string or list of IPs affected.
        hw_address: Optional hardware address (for add events).
        hostname: Optional hostname (for add events).
        duid: Optional DUID (for DHCPv6 add events).

    Raises:
        DatabaseError: If the entry cannot be saved. The lease change in Kea is already done,
            so callers log the error and continue.

    """
    if isinstance(ip_addresses, str):
        ip_addresses = [ip_addresses]
    ip_list = ", ".join(ip_addresses)
    if len(ip_addresses) == 1:
        parts = [f"Lease {action}: {ip_list}"]
    else:
        parts = [f"{len(ip_addresses)} lease(s) {action}: {ip_list}"]
    if hw_address:
        parts.append(f"hw-address: {hw_address}")
    if duid:
        parts.append(f"duid: {duid}")
    if hostname:
        parts.append(f"hostname: {hostname}")
    JournalEntry.objects.create(
        assigned_object=server,
        created_by=user,
        kind="info",
        comments="; ".join(parts),
    )


def _diagnostic_lines(snapshot: LeaseSnapshot) -> list[str]:
    """Return one safe line for each lease record that *snapshot* excluded."""
    return [
        f"{diagnostic.source_position} ({diagnostic.field or 'record'}): {diagnostic.message}"
        for diagnostic in snapshot.diagnostics
    ]


def _incomplete_export_message(snapshot: LeaseSnapshot) -> str:
    """Return why a complete export of *snapshot* is refused."""
    reasons = diagnostic_reasons(snapshot.diagnostics)
    return (
        f"Export refused: Kea returned {len(snapshot.diagnostics)} lease record(s) that could not be read"
        f" ({reasons}). A complete export cannot leave them out."
    )


class BaseServerLeasesView(generic.ObjectView, Generic[T]):
    """Generic base view for DHCP lease search tabs; specialised by IP version."""

    template_name = "netbox_kea/server_dhcp_leases.html"
    queryset = Server.objects.all()
    table: type[T]

    def get_table(self, data: list[dict[str, Any]], request: HttpRequest) -> T:
        """Build and configure the lease table for *request*."""
        table = self.table(data, user=request.user)
        table.configure(request)
        return table

    def _make_search_form(self, server: Server, data: Any | None = None):
        """Build the lease-search form with the subnet quick-select choices and their diagnostics."""
        snapshot = subnet_catalogue.display(server, self.dhcp_version)
        kwargs = {
            "subnet_choices": snapshot.subnet_choices,
            "subnet_cmds_available": snapshot.subnet_cmds_available,
            "subnet_diagnostics": tuple(dict.fromkeys(diagnostic.message for diagnostic in snapshot.diagnostics)),
            "subnet_catalogue_unavailable": snapshot.unavailable,
        }
        if data is None:
            return self.form(**kwargs)
        return self.form(data, **kwargs)

    def get_leases_page(self, client: KeaClient, server: Server, page: str | None, per_page: int) -> LeaseSnapshot:
        """Read one validated lease page of the Server."""
        return client.lease_get_page(self.dhcp_version, limit=per_page, cursor=page or None, server_id=server.pk)

    def get_leases(
        self,
        client: KeaClient,
        server: Server,
        q: Any,
        by: str,
        *,
        state: LeaseState | None = None,
    ) -> LeaseSnapshot:
        """Read the leases matching *q* by search attribute *by*."""
        return client.lease_search(self.dhcp_version, by, q, state=state, server_id=server.pk)

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Return an empty table, the search form, and the add-lease URL for the initial (non-HTMX) page load."""
        # For non-htmx requests.

        table = self.get_table([], request)
        form = self._make_search_form(instance, request.GET if "q" in request.GET else None)
        can_change = Server.objects.restrict(request.user, "change").filter(pk=instance.pk).exists()
        ctx: dict[str, Any] = {
            "form": form,
            "table": table,
            # Drives the in-page v4/v6 protocol toggle on the merged Leases tab.
            "dhcp_version": self.dhcp_version,
        }
        if can_change:
            ctx["add_url"] = reverse(
                f"plugins:netbox_kea:server_lease{self.dhcp_version}_add",
                args=[instance.pk],
            )
            ctx["bulk_import_url"] = reverse(
                f"plugins:netbox_kea:server_lease{self.dhcp_version}_bulk_import",
                args=[instance.pk],
            )
        return ctx

    def get_export(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Stream all matching leases as a CSV download."""
        instance = self.get_object(**kwargs)
        form = self._make_search_form(instance, request.GET)
        if not form.is_valid():
            messages.warning(request, "Invalid form for export.")
            return redirect(request.path)

        by = form.cleaned_data["by"]
        if not by:
            messages.warning(request, "A search attribute is required to export.")
            return redirect(request.path)

        q = form.cleaned_data["q"]
        state_filter: LeaseState | None = form.cleaned_data.get("state")
        try:
            client = instance.get_client(version=self.dhcp_version)
        except ValueError:
            logger.exception("Failed to create Kea client for server %s", instance.pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return redirect(request.path)
        try:
            state_in_kea = state_filter if by in (constants.BY_SUBNET, constants.BY_SUBNET_ID) else None
            snapshot = self.get_leases(
                client, instance, str(q.cidr) if by == constants.BY_SUBNET else q, by, state=state_in_kea
            )
        except LeaseQueryGuardError as exc:
            messages.warning(request, lease_query_guard_message(exc, state_filter))
            return redirect(request.path)
        except KeaException as exc:
            logger.exception("Failed to fetch leases for export on server %s", instance.pk)
            messages.error(request, kea_error_hint(exc))
            return redirect(request.path)
        except (requests.RequestException, RuntimeError, ValueError):
            logger.exception("Transport/parse error fetching leases for export on server %s", instance.pk)
            messages.error(request, "Failed to fetch leases for export; see server logs.")
            return redirect(request.path)

        limited = request.GET["export"] == "table"
        if not limited and not snapshot.complete:
            messages.warning(request, _incomplete_export_message(snapshot))
            return redirect(request.path)
        # A Subnet search filters by state in Kea; every other search filters here.
        local_state = state_filter if by not in (constants.BY_SUBNET, constants.BY_SUBNET_ID) else None
        if limited:
            table = self.get_table(snapshot_rows(snapshot, local_state), request)
            return export_table(table, "leases_limited_coverage.csv", use_selected_columns=True)
        return lease_csv_response(
            snapshot_leases(snapshot, local_state),
            family=self.dhcp_version,
            evaluated_at=snapshot.evaluated_at,
            filename="leases.csv",
        )

    def get_export_all(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Export a bounded complete lease collection as a CSV download.

        Fetches leases through the Kea client up to the export safety limit.
        Refuses a partial export when the server has more leases than the limit.
        Requires the ``lease_cmds`` hook to be loaded on the Kea server.
        """
        instance = self.get_object(**kwargs)

        per_page = 1000

        try:
            client = instance.get_client(version=self.dhcp_version)
            snapshot = client.lease_get_all(
                self.dhcp_version,
                per_page=per_page,
                max_leases=_LEASE_EXPORT_MAX_LEASES,
                server_id=instance.pk,
            )
        except KeaException as exc:
            logger.exception("Failed to fetch all leases for export on server %s", instance.pk)
            messages.error(request, kea_error_hint(exc))
            return redirect(request.path)
        except (requests.RequestException, ValueError, RuntimeError):
            logger.exception("Transport/parse error fetching all leases for export on server %s", instance.pk)
            messages.error(request, "Failed to fetch leases for export; see server logs.")
            return redirect(request.path)

        if snapshot.coverage != "exhaustive":
            logger.warning(
                "Refused a partial DHCPv%s lease export for server %s at the %s-lease limit",
                self.dhcp_version,
                instance.pk,
                _LEASE_EXPORT_MAX_LEASES,
            )
            messages.warning(
                request,
                f"Export is limited to {_LEASE_EXPORT_MAX_LEASES:,} leases. Narrow the lease set before exporting.",
            )
            return redirect(request.path)
        if not snapshot.complete:
            logger.warning(
                "Refused an incomplete DHCPv%s lease export for server %s: %d excluded record(s)",
                self.dhcp_version,
                instance.pk,
                len(snapshot.diagnostics),
            )
            messages.warning(request, _incomplete_export_message(snapshot))
            return redirect(request.path)

        return lease_csv_response(
            snapshot.records, family=self.dhcp_version, evaluated_at=snapshot.evaluated_at, filename="leases_all.csv"
        )

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Dispatch to export, HTMX partial, or full page render as appropriate."""
        instance: Server = self.get_object(**kwargs)

        if resp := check_dhcp_enabled(instance, self.dhcp_version):
            return resp

        if "export" in request.GET:
            return self.get_export(request, **kwargs)

        if "export_all" in request.GET:
            return self.get_export_all(request, **kwargs)

        if not request.htmx:
            return super().get(request, **kwargs)

        # Outside the try: the LeaseQueryGuardError handler below reads both `form` and
        # `form.cleaned_data`, so neither may depend on how far into the try we got.
        form = self._make_search_form(instance, request.GET)
        if not form.is_valid():
            table = self.get_table([], request)
            return render(
                request,
                "netbox_kea/server_dhcp_leases_htmx.html",
                {
                    "is_embedded": False,
                    "form": form,
                    "table": table,
                    "paginate": False,
                },
            )

        try:
            by = form.cleaned_data["by"]
            q = form.cleaned_data["q"]
            state_filter: LeaseState | None = form.cleaned_data.get("state")
            client = instance.get_client(version=self.dhcp_version)
            is_subnet_search = by in (constants.BY_SUBNET, constants.BY_SUBNET_ID)
            if by == "":
                snapshot = self.get_leases_page(
                    client,
                    instance,
                    form.cleaned_data["page"],
                    per_page=get_paginate_count(request),
                )
                next_page = None if snapshot.next_cursor is None else str(snapshot.next_cursor)
            else:
                next_page = None
                state_in_kea = state_filter if is_subnet_search else None
                snapshot = self.get_leases(
                    client,
                    instance,
                    str(q.cidr) if by == constants.BY_SUBNET else q,
                    by,
                    state=state_in_kea,
                )
            leases = snapshot_rows(snapshot, None if is_subnet_search else state_filter)

            can_delete = request.user.has_perm(
                "netbox_kea.bulk_delete_lease_from_server",
                obj=instance,
            )
            can_change = request.user.has_perm(
                "netbox_kea.change_server",
                obj=instance,
            )

            table = self.get_table(leases, request)
            visible_leases = leases
            if by != "":
                visible_leases = [row.record for row in table.paginated_rows]
                next_page = table.page.next_page_number() if table.page.has_next() else None

            stripped_return_url = _strip_empty_params(request.get_full_path())
            # Enrich only the visible table page with reservation badges and NetBox IPAM status.
            _enrich_leases_with_badges(
                visible_leases,
                instance,
                self.dhcp_version,
                can_delete=can_delete,
                can_change=can_change,
                sync=lease_sync_gates(request.user),
                return_url=stripped_return_url,
            )

            if not can_delete:
                table.columns.hide("pk")

            response = render(
                request,
                "netbox_kea/server_dhcp_leases_htmx.html",
                {
                    "can_delete": can_delete,
                    "is_embedded": False,
                    "delete_action": (
                        reverse(
                            f"plugins:netbox_kea:server_leases{self.dhcp_version}_delete",
                            args=[instance.pk],
                        )
                        + "?"
                        + _urlencode({"return_url": stripped_return_url})
                    ),
                    "return_url": stripped_return_url,
                    "form": form,
                    "table": table,
                    "next_page": next_page,
                    "lease_diagnostics": _diagnostic_lines(snapshot),
                    "paginate": True,
                    "page_lengths": EnhancedPaginator.default_page_lengths,
                },
            )
            # Tell HTMX which URL to push to the browser history.  The request
            # URL may include empty params (e.g. state=) that HTMX would otherwise
            # push verbatim; sending the stripped URL as HX-Push-Url overrides
            # that so the address bar always shows the clean URL.
            response["HX-Push-Url"] = stripped_return_url
        except LeaseQueryGuardError as exc:
            logger.info("Rejected unsafe Subnet lease query on server %s: %s", instance.pk, exc)
            field = "q" if isinstance(exc, LeaseQueryUnknownSubnet) else "state"
            form.add_error(field, lease_query_guard_message(exc, form.cleaned_data.get("state")))
            table = self.get_table([], request)
            return render(
                request,
                "netbox_kea/server_dhcp_leases_htmx.html",
                {
                    "is_embedded": False,
                    "form": form,
                    "table": table,
                    "paginate": False,
                },
            )
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            error_id = str(uuid.uuid4())
            logger.exception("HTMX leases handler error [%s]", error_id)
            return render(
                request,
                "netbox_kea/exception_htmx.html",
                {"error_id": error_id},
            )
        else:
            return response


# Single consolidated "Leases" tab shared by the v4 and v6 leases views. Only
# ServerLeases4View carries it as a class attribute (so exactly one tab entry is
# generated); ServerLeases6View injects it via get_extra_context so the same tab
# stays highlighted when viewing v6. An in-page v4/v6 toggle (template) switches
# between the two underlying URLs, which are unchanged.
_LEASES_TAB = OptionalViewTab(label="Leases", weight=1010, is_enabled=lambda s: s.dhcp4 or s.dhcp6)


@register_model_view(Server, "leases6")
class ServerLeases6View(BaseServerLeasesView[tables.LeaseTable6]):
    """DHCPv6 leases view (rendered under the shared Leases tab)."""

    form = forms.Leases6SearchForm
    table = tables.LeaseTable6
    dhcp_version = 6

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Highlight the shared Leases tab (this view has no class-level tab of its own)."""
        ctx = super().get_extra_context(request, instance)
        ctx["tab"] = _LEASES_TAB
        return ctx


@register_model_view(Server, "leases4")
class ServerLeases4View(BaseServerLeasesView[tables.LeaseTable4]):
    """DHCPv4 leases view; owns the shared Leases tab."""

    tab = _LEASES_TAB
    form = forms.Leases4SearchForm
    table = tables.LeaseTable4
    dhcp_version = 4

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Redirect to the v6 leases view on v6-only servers so the merged tab works."""
        instance = self.get_object(**kwargs)
        if not instance.dhcp4 and instance.dhcp6:
            return redirect(reverse("plugins:netbox_kea:server_leases6", args=[instance.pk]))
        return super().get(request, **kwargs)


class FakeLeaseModelMeta:
    """Minimal ``_meta`` shim so bulk_delete.html can introspect the lease pseudo-model."""

    app_label = "netbox_kea"
    model_name = "lease"
    verbose_name_plural = "leases"


# Fake model to allow us to use the bulk_delete.html template.
class FakeLeaseModel:
    """Pseudo-model used to satisfy the bulk_delete.html template contract without a real DB model."""

    _meta = FakeLeaseModelMeta


class BaseServerLeasesDeleteView(GetReturnURLMixin, generic.ObjectView, metaclass=ABCMeta):
    """Base view for confirming and processing bulk deletion of DHCP leases."""

    queryset = Server.objects.all()
    default_return_url = "plugins:netbox_kea:server_list"

    def get(self, request: HttpRequest, **kwargs):
        """Redirect back to the server on GET (this view is POST-only)."""
        return redirect(self.get_return_url(request, obj=self.get_object(**kwargs)))

    def post(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Show confirmation page or delete leases if confirmed."""
        instance: Server = self.get_object(**kwargs)

        if check_dhcp_enabled(instance, self.dhcp_version):
            return _after_lease_delete(request, instance.get_absolute_url())

        if not request.user.has_perm("netbox_kea.bulk_delete_lease_from_server", obj=instance):
            return HttpResponseForbidden("This user does not have permission to delete DHCP leases.")

        form = self.form(request.POST)

        return_url = _strip_empty_params(self.get_return_url(request, obj=instance))
        if not form.is_valid():
            messages.warning(request, str(form.errors))
            return _after_lease_delete(request, return_url)

        selected: tuple[ShownLease, ...] = form.cleaned_data["pk"]
        if "_confirm" not in request.POST:
            return render(
                request,
                "netbox_kea/server_lease_bulk_delete.html",
                {
                    "model": FakeLeaseModel,
                    "table": tables.LeaseDeleteTable([_shown_row(shown) for shown in selected], orderable=False),
                    "form": form,
                    "return_url": return_url,
                },
            )

        try:
            client = instance.get_client(version=self.dhcp_version)
        except ValueError:
            logger.exception("Failed to create Kea client for server %s", instance.pk)
            messages.error(request, "Failed to connect to Kea: see server logs for details.")
            return _after_lease_delete(request, return_url)

        deleted: list[Lease] = []
        failed_count = 0
        for shown in selected:
            try:
                result = client.lease_delete(shown)
            except KeaException as exc:
                logger.exception("Kea error deleting lease %s on server %s", shown.label, instance.pk)
                messages.error(request, f"Error deleting lease {shown.label}: {kea_error_hint(exc)}")
                failed_count += 1
                continue
            except (requests.RequestException, RuntimeError, ValueError):
                logger.exception("Error deleting lease %s on server %s", shown.label, instance.pk)
                messages.error(request, f"Error deleting lease {shown.label}: see server logs for details.")
                failed_count += 1
                continue
            if isinstance(result, LeaseChanged):
                deleted.append(result.lease)
            else:
                failed_count += _report_outcome(request, shown.label, result, "delete")

        if deleted:
            messages.success(request, f"Deleted {len(deleted)} DHCPv{self.dhcp_version} lease(s).")
            try:
                _add_lease_journal(instance, request.user, "deleted", [shown_lease(lease).label for lease in deleted])
            except DatabaseError:
                logger.exception("Failed to record lease journal for server %s; continuing", instance.pk)
            leases_deleted.send_robust(
                sender=None,
                server=instance,
                leases=tuple(deleted),
                dhcp_version=self.dhcp_version,
                request=request,
            )

        if failed_count:
            messages.warning(request, f"Failed to delete {failed_count} lease(s). See above for details.")
        return _after_lease_delete(request, return_url)


def _after_lease_delete(request: HttpRequest, return_url: str) -> HttpResponse:
    """Show the queued messages: reload the page for the one-click row button, else redirect to *return_url*."""
    if request.headers.get("HX-Request"):
        response = HttpResponse()
        response["HX-Refresh"] = "true"
        return response
    return redirect(return_url)


class ServerLeases6DeleteView(BaseServerLeasesDeleteView):
    """Bulk-delete view for DHCPv6 leases."""

    form = forms.Lease6DeleteForm
    dhcp_version = 6
    tab = _LEASES_TAB


class ServerLeases4DeleteView(BaseServerLeasesDeleteView):
    """Bulk-delete view for DHCPv4 leases."""

    form = forms.Lease4DeleteForm
    dhcp_version = 4
    tab = _LEASES_TAB


# The words of each Lease fact whose name alone does not read well.
_FACT_NAMES: dict[LeaseFact, str] = {"identity": "kind", "subnet_id": "Subnet", "binding": "client binding"}
_KIND_LABELS = {"address": "Address", "delegated-prefix": "Delegated prefix"}
# The past participle and the page of each Lease action.
_ACTIONS: dict[str, tuple[str, str]] = {"edit": ("changed", "the form"), "delete": ("deleted", "the list")}


def _fact_words(facts: tuple[LeaseFact, ...]) -> str:
    *rest, last = (_FACT_NAMES.get(fact, fact.replace("_", " ")) for fact in facts)
    return f"{', '.join(rest)} and {last}" if rest else last


def _report_outcome(
    request: HttpRequest, label: str, result: LeaseChangeResult, action: Literal["edit", "delete"]
) -> bool:
    """Queue the one message of a Lease edit or delete outcome; return whether the action failed."""
    verb, page = _ACTIONS[action]
    if isinstance(result, LeaseChanged):
        messages.success(request, f"Lease {label} updated.")
    elif isinstance(result, LeaseAbsent):
        not_recreated = " The edit did not recreate it." if action == "edit" else ""
        messages.warning(request, f"Lease {label} was not found in Kea; nothing was {verb}.{not_recreated}")
    elif isinstance(result, LeaseConflict):
        messages.warning(
            request,
            f"Lease {label} was not {verb}: its {_fact_words(result.fields)} changed in Kea after {page} was shown."
            f" Reload {page} and try again.",
        )
    elif isinstance(result, LeaseChangeRefused):
        messages.warning(
            request,
            f"Kea did not change lease {label}: it reports that the lease was deleted or changed after the check."
            f" Reload {page} and try again.",
        )
    else:
        logger.warning("Kea returned a malformed lease %s", label)
        messages.error(request, f"Lease {label} was not {verb}: Kea returned a lease that could not be read.")
        return True
    return False


def _binding_text(binding: DHCPv4Binding | DHCPv6Binding) -> str:
    if isinstance(binding, DHCPv6Binding):
        return f"DUID {binding.duid or '(empty)'}, IAID {binding.iaid}"
    parts = [
        f"{name} {value}" for name, value in (("MAC", binding.hw_address), ("client ID", binding.client_id)) if value
    ]
    return ", ".join(parts) or "(none)"


def _shown_row(shown: ShownLease) -> dict[str, Any]:
    """Return the confirmation row of one selected Lease: the facts that its delete compares."""
    return {
        "lease": shown.label,
        "kind": _KIND_LABELS[shown.identity.kind],
        "binding": _binding_text(shown.binding),
        "subnet_id": shown.subnet_id,
    }


class _BaseLeaseEditView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Base view for editing a single Lease via ``lease{v}-update``.

    The route target is the address, or ``address/length`` for a delegated prefix. The page carries the
    shown Lease facts, and the save writes only the fields that differ from them.
    Subclasses must set ``dhcp_version`` and ``form_class``.
    """

    dhcp_version: Family
    form_class: type[forms._LeaseEditForm]

    def _get_server(self, pk: int) -> Server:
        return get_object_or_404(Server.objects.restrict(self.request.user, "view"), pk=pk)

    def _leases_url(self, server: Server) -> str:
        """Return the lease search that linked here, else the lease list."""
        return _safe_return_url(
            self.request, reverse(f"plugins:netbox_kea:server_leases{self.dhcp_version}", kwargs={"pk": server.pk})
        )

    def _render(self, request: HttpRequest, server: Server, form: forms._LeaseEditForm, shown: ShownLease):
        return render(
            request,
            "netbox_kea/server_lease_edit.html",
            {
                "object": server,
                "server": server,
                "lease_label": shown.label,
                "lease_kind": _KIND_LABELS[shown.identity.kind],
                "form": form,
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._leases_url(server),
                "tab": self.tab,
            },
        )

    def get(self, request: HttpRequest, pk: int, ip_address: str) -> HttpResponse:
        """Render the edit form pre-filled with the current lease values and the shown facts."""
        server = self._get_server(pk)

        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp

        try:
            label, identity = parse_selection(self.dhcp_version, ip_address)
            client = server.get_client(version=self.dhcp_version)
            result = client.lease_get(identity)
        except KeaException as exc:
            logger.exception("Failed to fetch lease %s on server %s", ip_address, pk)
            messages.error(request, kea_error_hint(exc))
            return redirect(self._leases_url(server))
        except (requests.RequestException, RuntimeError, ValueError):
            logger.exception("Failed to fetch lease %s on server %s", ip_address, pk)
            messages.error(request, "Failed to fetch lease: see server logs for details.")
            return redirect(self._leases_url(server))

        if isinstance(result, LeaseAbsent):
            messages.warning(request, f"Lease {label} not found.")
            return redirect(self._leases_url(server))
        if not isinstance(result, LeaseFound):
            logger.warning("Kea returned a malformed lease %s on server %s", label, pk)
            messages.error(request, "Kea returned a lease that could not be read; it cannot be edited.")
            return redirect(self._leases_url(server))
        lease = result.lease
        shown = shown_lease(lease)
        if shown.label != label:
            messages.warning(request, f"Kea now delegates {shown.label}, not {label}. Reload the lease list.")
            return redirect(self._leases_url(server))
        identifier = lease.hw_address if isinstance(lease, DHCPv4AddressLease) else lease.duid
        initial: dict[str, Any] = {
            "shown": shown.model_dump_json(),
            "hostname": lease.hostname,
            "valid_lft": lease.valid_lifetime,
            self.form_class.identifier_field: identifier or "",
        }
        return self._render(request, server, self.form_class(initial=initial), shown)

    def post(self, request: HttpRequest, pk: int, ip_address: str) -> HttpResponse:
        """Write the changed fields via ``lease{v}-update`` after a fresh read agrees with the shown facts."""
        server = self._get_server(pk)

        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp

        form = self.form_class(request.POST)
        valid = form.is_valid()
        shown: ShownLease | None = form.cleaned_data.get("shown")
        try:
            label, identity = parse_selection(self.dhcp_version, ip_address)
        except ValueError:
            label, identity = ip_address, None
        if shown is None or shown.identity != identity or shown.label != label:
            messages.error(request, "The form does not hold the shown facts of this lease. Reload the lease list.")
            return redirect(self._leases_url(server))
        if not valid:
            return self._render(request, server, form, shown)
        edit: LeaseEdit = form.cleaned_data["edit"]
        if not edit.written:
            messages.info(request, f"Lease {label} was not changed: the form holds the shown values.")
            return redirect(self._leases_url(server))
        try:
            client = server.get_client(version=self.dhcp_version)
            result = client.lease_update(shown, edit)
        except KeaException as exc:
            logger.exception("Error updating lease %s", label)
            messages.error(request, kea_error_hint(exc))
        except (requests.RequestException, RuntimeError, ValueError):
            logger.exception("Error updating lease %s (transport/parse error)", label)
            messages.error(request, "Failed to update lease: see server logs for details.")
        else:
            _report_outcome(request, label, result, "edit")
        return redirect(self._leases_url(server))


@register_model_view(Server, "lease4_edit", path="leases4/<path:ip_address>/edit")
class ServerLease4EditView(_BaseLeaseEditView):
    """Edit a single DHCPv4 lease."""

    dhcp_version = 4
    form_class = forms.Lease4EditForm
    tab = _LEASES_TAB


@register_model_view(Server, "lease6_edit", path="leases6/<path:ip_address>/edit")
class ServerLease6EditView(_BaseLeaseEditView):
    """Edit a single DHCPv6 lease or delegated prefix."""

    dhcp_version = 6
    form_class = forms.Lease6EditForm
    tab = _LEASES_TAB


class _BaseLeaseAddView(_KeaChangeMixin, generic.ObjectView):
    """Base view for creating a new lease via ``lease{v}-add``."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_lease_add.html"
    dhcp_version: Family
    form_class: type[forms._LeaseAddForm]
    # Use _active_tab (not `tab`) so model_view_tabs does not register this as a
    # duplicate navigation entry — the add view URL resolves with pk-only, which
    # would cause the parent list tab to appear twice in the tab bar.
    _active_tab: OptionalViewTab

    def _leases_url(self, server: Server) -> str:
        return reverse(f"plugins:netbox_kea:server_leases{self.dhcp_version}", args=[server.pk])

    def _render(self, request: HttpRequest, server: Server, form: forms._LeaseAddForm) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._leases_url(server),
                "tab": self._active_tab,
            },
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the empty add form."""
        server = self.get_object(pk=pk)

        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp

        return self._render(
            request, server, self.form_class(sync_refusal=sync_gate(request.user, claim_permissions(LEASE)).reason)
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Create the Lease of the typed request, read it back, and report both outcomes."""
        server = self.get_object(pk=pk)

        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp

        gate = sync_gate(request.user, claim_permissions(LEASE))
        form = self.form_class(request.POST, sync_refusal=gate.reason)
        if not form.is_valid():
            return self._render(request, server, form)
        creation: LeaseRequest = form.cleaned_data["request"]
        address = str(creation.address)
        try:
            client = server.get_client(version=self.dhcp_version)
            client.lease_add(creation)
        except KeaException as exc:
            logger.exception("Failed to create DHCPv%s lease for %s", self.dhcp_version, address)
            messages.error(request, kea_error_hint(exc))
            return self._render(request, server, form)
        except requests.RequestException:
            logger.exception("Failed to create DHCPv%s lease for %s", self.dhcp_version, address)
            messages.error(request, "Failed to create lease: see server logs for details.")
            return self._render(request, server, form)
        except (RuntimeError, ValueError):
            logger.exception("Failed to create DHCPv%s lease for %s (parse error)", self.dhcp_version, address)
            messages.error(request, "Failed to create lease: invalid response from Kea.")
            return self._render(request, server, form)
        messages.success(request, f"Lease for {address} created.")
        created = _read_created_lease(request, client, creation)
        try:
            _add_lease_journal(
                server,
                request.user,
                "added",
                address,
                hw_address=creation.hw_address if isinstance(creation, DHCPv4LeaseRequest) else "",
                hostname=creation.hostname or "",
                duid=creation.duid if isinstance(creation, DHCPv6LeaseRequest) else "",
            )
        except DatabaseError:
            logger.exception("Failed to record journal entry for lease %s", address)
        lease_added.send_robust(
            sender=None,
            server=server,
            creation=creation,
            lease=created,
            dhcp_version=self.dhcp_version,
            request=request,
        )
        if form.cleaned_data["sync_to_netbox"]:
            _run_lease_sync_to_netbox(request, server, address, created, gate)
        return redirect(self._leases_url(server))


@register_model_view(Server, "lease4_add", path="leases4/add")
class ServerLease4AddView(_BaseLeaseAddView):
    """Create a new DHCPv4 lease."""

    dhcp_version = 4
    form_class = forms.Lease4AddForm
    _active_tab = _LEASES_TAB


@register_model_view(Server, "lease6_add", path="leases6/add")
class ServerLease6AddView(_BaseLeaseAddView):
    """Create a new DHCPv6 lease."""

    dhcp_version = 6
    form_class = forms.Lease6AddForm
    _active_tab = _LEASES_TAB


class _IdentityLookups:
    """Resolve each ``(Scope, Identity)`` Reservation lookup once for a whole lease page.

    Leases that share a device repeat the same identities, and Kea answers each of
    those queries identically for the life of one page.
    """

    def __init__(self) -> None:
        self._registry = threading.Lock()
        self._locks: dict[Any, threading.Lock] = {}
        self._results: dict[Any, Reservation | None] = {}
        self._failures: dict[Any, Exception] = {}

    def resolve(self, key: Any, lookup: Callable[[], Reservation | None]) -> Reservation | None:
        """Return the memoized result for *key*, calling *lookup* at most once.

        A failure is memoized and replayed: leases repeat identities, so re-raising the
        stored exception keeps every caller's "indeterminate" answer without reissuing a
        query that already failed for this page.
        """
        with self._registry:
            entry = self._locks.setdefault(key, threading.Lock())
        with entry:
            if key in self._failures:
                raise self._failures[key]
            if key not in self._results:
                try:
                    self._results[key] = lookup()
                except Exception as exc:
                    self._failures[key] = exc
                    raise
            return self._results[key]


def _close_worker_client(client: KeaClient) -> None:
    """Close one worker client, reporting a failure instead of raising it."""
    try:
        client.close()
    except Exception:
        logger.warning("Could not close a Reservation worker Kea client", exc_info=True)


class _LeaseReservationWorkerClients:
    """Keep one private Kea client for each reservation worker thread."""

    def __init__(self, source: KeaClient) -> None:
        self._source = source
        self._local = threading.local()
        self._registry = threading.Lock()
        self._clients: list[KeaClient] = []

    def get(self) -> KeaClient:
        """Return the current worker thread's Kea client."""
        worker_client = getattr(self._local, "client", None)
        if worker_client is None:
            worker_client = self._source.clone()
            with self._registry:
                self._clients.append(worker_client)
            self._local.client = worker_client
        return worker_client

    def close(self) -> None:
        """Close all worker clients after the executor stops.

        A close failure must not replace the enrichment result the callers already
        computed, so each one is logged and the rest still close.
        """
        with self._registry:
            clients = tuple(self._clients)
        for client in clients:
            _close_worker_client(client)


def _row_address(row: dict[str, Any]) -> str:
    """Return the canonical address of the typed Lease of one presentation row."""
    return str(row["lease"].identity.address)


def _reservation_for_lease_worker(worker_clients, version, catalogue, row, lookups: _IdentityLookups):
    """Resolve one lease row to a typed Reservation in a thread-local client."""
    lease = row["lease"]
    ip = _row_address(row)
    subnet = catalogue.find_by_id(lease.subnet_id)
    if not isinstance(subnet, VerifiedSubnet):
        return ip, None, None
    scope = InSubnetReservationScope(subnet.identity)
    carried = lease_identities(lease)
    worker_client = worker_clients.get()
    try:
        if isinstance(lease, DHCPv6PrefixLease):
            reservation = worker_client.reservation_by_prefix(catalogue, scope, lease.prefix)
        else:
            reservation = worker_client.reservation_by_address(version, catalogue, scope, ip)
        if reservation is not None:
            return ip, reservation, True
        for identity_scope in (scope, GlobalReservationScope()):
            for identity in carried.identities:
                reservation = lookups.resolve(
                    (identity_scope, identity),
                    partial(worker_client.reservation_by_identity, version, catalogue, identity_scope, identity),
                )
                if reservation is not None:
                    return ip, reservation, True
    except KeaException as exc:
        if exc.unsupported_command:
            return ip, None, False
        logger.debug("Reservation lookup failed for lease %s", ip, exc_info=True)
        return ip, None, None
    except (requests.RequestException, RuntimeError, ValueError):
        logger.debug("Reservation lookup failed for lease %s", ip, exc_info=True)
        return ip, None, None
    # An identifier that no Reservation can hold leaves "no Reservation" unproven.
    return ip, None, None if carried.foreign else True


def _fetch_reservations_for_leases(
    client: KeaClient,
    version: Family,
    catalogue,
    leases: list[dict[str, Any]],
) -> tuple[dict[str, Reservation], bool, set[str]]:
    """Resolve each visible lease through scoped address and normalized Identity queries."""
    if not leases:
        return {}, True, set()
    matches: dict[str, Reservation] = {}
    failed_ips: set[str] = set()
    host_cmds_available = True
    worker_clients = _LeaseReservationWorkerClients(client)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(leases), 10)) as executor:
            lookups = _IdentityLookups()
            futures = [
                executor.submit(_reservation_for_lease_worker, worker_clients, version, catalogue, lease, lookups)
                for lease in leases
            ]
            for future in concurrent.futures.as_completed(futures):
                ip, reservation, lookup_state = future.result()
                if lookup_state is False:
                    host_cmds_available = False
                elif lookup_state is None:
                    failed_ips.add(ip)
                if reservation is not None:
                    matches[ip] = reservation
    finally:
        worker_clients.close()
    return matches, host_cmds_available, failed_ips


def _canonical_reservation_url(server_pk: int, reservation: Reservation, return_url: str) -> str | None:
    if isinstance(reservation.scope, GlobalReservationScope):
        return None
    params = {"identifier_type": reservation.identity.identifier_type, "identifier": reservation.identity.value}
    query = _urlencode({**params, "return_url": return_url} if return_url else params)
    base = reverse(
        f"plugins:netbox_kea:server_reservation{reservation.family}_edit",
        args=[server_pk, reservation.scope.subnet.subnet_id],
    )
    return f"{base}?{query}"


def _set_lease_reservation_fields(
    lease: dict[str, Any],
    reservation: Reservation | None,
    server_pk: int,
    version: Family,
    subnet_cidr: str | None,
    host_cmds_available: bool,
    failed_ips: set[str],
    can_change: bool,
    return_url: str,
) -> None:
    """Set one lease row from its typed canonical Reservation match.

    A delegated prefix compares with the Reservation prefixes, an address with the Reservation addresses.
    """
    observed = lease["lease"]
    ip = _row_address(lease)
    is_prefix = isinstance(observed, DHCPv6PrefixLease)
    target = lease["label"]
    lease.update(
        {
            "is_reserved": reservation is not None,
            "host_reservation": False,
            "reservation_url": None,
            "create_reservation_url": None,
            "pending_ip_change": False,
            "pending_reservation_ip": "",
            "other_kind_reservation": False,
            "stale_mac": False,
            "stale_lease_mac": "",
            "reservation_mac": "",
            "delete_lease_url": "",
            "can_change_reservation": False,
        }
    )
    if reservation is not None:
        reserved = [str(item) for item in (reservation.delegated_prefixes if is_prefix else reservation.addresses)]
        lease["reservation_url"] = _canonical_reservation_url(server_pk, reservation, return_url)
        lease["can_change_reservation"] = can_change and lease["reservation_url"] is not None
        if (
            isinstance(reservation.scope, InSubnetReservationScope)
            and not reservation.addresses
            and not reservation.delegated_prefixes
        ):
            # Kea assigns this lease from the pool; the Reservation holds only a hostname or options.
            lease["is_reserved"] = False
            lease["host_reservation"] = True
        if not reserved and (reservation.addresses if is_prefix else reservation.delegated_prefixes):
            # The client's Reservation holds only the other kind, so it does not reserve this allocation.
            lease["is_reserved"] = False
            lease["other_kind_reservation"] = True
            return
        if isinstance(reservation.scope, InSubnetReservationScope) and reserved and target not in reserved:
            lease["pending_ip_change"] = True
            # The lease keeps its allocation until renewal, so the reservation is pending, not current.
            lease["is_reserved"] = False
            # Every reserved allocation of the kind, because the domain names no primary one.
            lease["pending_reservation_ip"] = ", ".join(reserved)
        if target in reserved and reservation.identity.identifier_type == "hw-address":
            lease_hw_value = next(
                (
                    identity.value
                    for identity in lease_identities(observed).identities
                    if identity.identifier_type == "hw-address"
                ),
                "",
            )
            if lease_hw_value and lease_hw_value != reservation.identity.value:
                lease["stale_mac"] = True
                lease["stale_lease_mac"] = lease_hw_value
                lease["reservation_mac"] = reservation.identity.value
                lease["delete_lease_url"] = reverse(
                    f"plugins:netbox_kea:server_leases{version}_delete",
                    args=[server_pk],
                )
        return
    if not (can_change and host_cmds_available and ip not in failed_ips and subnet_cidr):
        return
    allocation_field = "prefixes" if is_prefix else "ip_addresses" if version == 6 else "ip_address"
    params = {
        "subnet_cidr": subnet_cidr,
        allocation_field: target,
        "hostname": lease_published_name(observed.hostname),
        "return_url": return_url,
    }
    identities = lease_identities(observed).identities
    if identities:
        params["identifier_type"] = identities[0].identifier_type
        params["identifier"] = identities[0].value
    base = reverse(f"plugins:netbox_kea:server_reservation{version}_add", args=[server_pk])
    lease["create_reservation_url"] = f"{base}?{_urlencode({key: value for key, value in params.items() if value})}"


def _sync_vrf_prefixes(server: "Server", leases: list[Lease]) -> dict[ipaddress.IPv6Network, Any]:
    """Return the NetBox Prefix in the Server's sync VRF for each delegated prefix of *leases*, in one query."""
    from ipam.models import Prefix

    delegated = [str(lease.prefix) for lease in leases if isinstance(lease, DHCPv6PrefixLease)]
    if not delegated:
        return {}
    found: dict[ipaddress.IPv6Network, Any] = {}
    for prefix in Prefix.objects.filter(vrf_id=server.sync_vrf_id, prefix__in=delegated):
        found.setdefault(ipaddress.IPv6Network(str(prefix.prefix)), prefix)
    return found


#: The NetBox model that a Sync of each allocation kind writes.
def _enrich_leases_with_badges(
    leases: list[dict[str, Any]],
    server: "Server",
    version: Family,
    can_delete: bool = False,
    can_change: bool = False,
    *,
    sync: Mapping[AllocationKind, SyncGate],
    return_url: str,
) -> None:
    """In-place: add reservation and NetBox IPAM badge fields to lease dicts.

    Adds:
    - ``reservation_url``: reservation link if a reservation exists for this IP
    - ``can_change_reservation``: whether the user may edit the reservation (gates link vs plain badge)
    - ``host_reservation``: an In-Subnet Reservation that holds no address and no delegated prefix matched
    - ``create_reservation_url``: pre-filled add link if host_cmds is loaded
    - ``netbox_ip_url``: absolute URL if the address of an address Lease exists in NetBox IPAM
    - ``netbox_prefix_url``: absolute URL if the delegated prefix exists as a Prefix in the Server's sync VRF
    - ``sync_url``: POST endpoint URL to claim the Lease when neither link is set and the gate of its kind allows it
    - ``sync_refusal``: the reason when the gate of its kind refuses the user
    - ``can_delete``: whether the current user may delete this lease
    - ``can_change``: whether the current user may edit this lease (gates edit_url)

    *return_url* is the lease search page; the lease and Reservation edit forms return to it.
    """
    from ..sync import bulk_fetch_netbox_ips

    reservation_by_ip: dict[str, Reservation] = {}
    host_cmds_available = True
    failed_ips: set[str] = set()
    client: KeaClient | None = None
    catalogue = None
    try:
        from ..subnet_catalogue import display

        client = server.get_client(version=version)
        catalogue = display(server, version)
        reservation_by_ip, host_cmds_available, failed_ips = _fetch_reservations_for_leases(
            client, version, catalogue, leases
        )
    except Exception as exc:
        failed_ips = {_row_address(lease) for lease in leases}
        logger.warning("unexpected error during lease enrichment: %s", exc, exc_info=True)
    finally:
        if client is not None:
            _close_worker_client(client)

    for lease in leases:
        ip = _row_address(lease)
        subnet = catalogue.find_by_id(lease["lease"].subnet_id) if catalogue is not None else None
        _set_lease_reservation_fields(
            lease,
            reservation_by_ip.get(ip),
            server.pk,
            version,
            subnet.cidr if isinstance(subnet, VerifiedSubnet) else None,
            host_cmds_available,
            failed_ips,
            can_change,
            return_url,
        )

    sync_url = reverse(f"plugins:netbox_kea:server_lease{version}_sync", args=[server.pk])
    edit_url_name = f"plugins:netbox_kea:server_lease{version}_edit"
    edit_query = f"?{_urlencode({'return_url': return_url})}" if return_url else ""
    # A delegated prefix is not an IP Address, so only an address Lease links one.
    addresses = [_row_address(lease) for lease in leases if lease["lease"].kind == "address"]
    nb_ips = bulk_fetch_netbox_ips(addresses, vrf_id=server.sync_vrf_id)
    nb_prefixes = _sync_vrf_prefixes(server, [lease["lease"] for lease in leases])
    for lease in leases:
        ip = _row_address(lease)
        observed = lease["lease"]
        if isinstance(observed, DHCPv6PrefixLease):
            synced, url_key = nb_prefixes.get(observed.prefix), "netbox_prefix_url"
        else:
            synced, url_key = nb_ips.get(ip), "netbox_ip_url"
        if synced is not None:
            lease[url_key] = synced.get_absolute_url()
        # Sync needs a Current Lease; don't offer it for leases with indeterminate reservation state.
        elif (
            lease["current"]
            and host_cmds_available
            and not lease.get("pending_ip_change")
            and not lease.get("stale_mac")
            and ip not in failed_ips
        ):
            gate = sync[observed.kind]
            if gate.allowed:
                lease["sync_url"] = sync_url
            else:
                lease["sync_refusal"] = gate.reason
        if can_change:
            lease["edit_url"] = reverse(edit_url_name, args=[server.pk, lease["label"]]) + edit_query
        lease["can_delete"] = can_delete
        lease["can_change"] = can_change
