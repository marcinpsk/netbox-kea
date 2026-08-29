import concurrent.futures
import logging
import threading
import uuid
from abc import ABCMeta
from collections.abc import Callable
from functools import partial
from typing import Any, Generic, TypeVar
from urllib.parse import urlencode as _urlencode

import requests
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.db.utils import OperationalError, ProgrammingError
from django.http import HttpResponse, HttpResponseForbidden
from django.http.request import HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from netbox.tables import BaseTable
from netbox.views import generic
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.views import GetReturnURLMixin, register_model_view

from .. import constants, forms, tables
from ..kea import (
    KeaClient,
    KeaException,
    LeaseQueryGuardError,
    lease_query_guard_message,
)
from ..models import Server
from ..reservations import (
    GlobalReservationScope,
    InSubnetReservationScope,
    Reservation,
    ReservationIdentity,
)
from ..signals import lease_added, leases_deleted
from ..sync import sync_lease_to_netbox
from ..utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    export_table,
    fetch_subnet_choices,
    format_leases,
    kea_error_hint,
)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin, _strip_empty_params

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseTable)
_LEASE_EXPORT_MAX_LEASES = 50_000


def _run_lease_sync_to_netbox(request: HttpRequest, lease: dict, ip_address: str) -> None:
    """Sync a just-created lease to NetBox IPAM, gated on IPAM write permission.

    Requires ``ipam.add_ipaddress`` + ``ipam.change_ipaddress`` (server-edit access
    alone is not enough — mirrors the per-row/bulk sync endpoints). The sync uses
    ``force=False``, so a foreign (non-Kea-managed) NetBox IP is skipped rather than
    overwritten; that skip is reported as a warning instead of a misleading "synced"
    message. Queues a success/warning message; never raises.
    """
    if not (request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress")):
        messages.warning(request, "Lease created, but it was not synced to NetBox (requires IPAM permission).")
        return
    try:
        conflicts: list[str] = []
        _nb_ip, nb_created, nb_changed = sync_lease_to_netbox(lease, conflicts=conflicts)
        if conflicts:
            messages.warning(
                request,
                f"Lease created, but NetBox IPAM sync was skipped: {ip_address} already exists and is not Kea-managed.",
            )
        else:
            nb_action = "created" if nb_created else "updated" if nb_changed else "already up to date"
            messages.success(request, f"IPAddress {ip_address} {nb_action} in NetBox.")
    except (ValueError, DatabaseError, ValidationError, requests.RequestException):
        logger.exception("Failed to sync lease %s to NetBox", ip_address)
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

    Silently skips if JournalEntry is unavailable (older NetBox or import error).

    Args:
        server: The Server instance the journal entry is attached to.
        user: The request.user who performed the action.
        action: Human-readable action name: "added" or "deleted".
        ip_addresses: A single IP string or list of IPs affected.
        hw_address: Optional hardware address (for add events).
        hostname: Optional hostname (for add events).
        duid: Optional DUID (for DHCPv6 add events).

    """
    try:
        from extras.models import JournalEntry

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
    except ImportError:
        pass  # JournalEntry unavailable on older NetBox versions
    except (ProgrammingError, OperationalError, DatabaseError):
        logger.debug("Failed to create lease journal entry", exc_info=True)


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
        """Build the lease-search form with the subnet quick-select choices populated."""
        subnet_choices, subnet_cmds_available = fetch_subnet_choices(server, self.dhcp_version)
        kwargs = {"subnet_choices": subnet_choices, "subnet_cmds_available": subnet_cmds_available}
        if data is None:
            return self.form(**kwargs)
        return self.form(data, **kwargs)

    def get_leases_page(
        self, client: KeaClient, page: str | None, per_page: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch and format one validated lease page."""
        result = client.lease_get_page(
            self.dhcp_version,
            limit=per_page,
            cursor=page or None,
        )
        return format_leases(result.leases), result.next_cursor

    def get_leases(
        self,
        client: KeaClient,
        q: Any,
        by: str,
        *,
        state: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query and format leases matching *q* by search attribute *by*."""
        leases = client.lease_search(self.dhcp_version, by, q, state=state)
        return format_leases(leases)

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
        state_filter: int | None = form.cleaned_data.get("state")
        try:
            client = instance.get_client(version=self.dhcp_version)
        except (ValueError, requests.RequestException):
            logger.exception("Failed to create Kea client for server %s", instance.pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return redirect(request.path)
        try:
            state_in_kea = state_filter if by in (constants.BY_SUBNET, constants.BY_SUBNET_ID) else None
            leases = self.get_leases(client, str(q.cidr) if by == constants.BY_SUBNET else q, by, state=state_in_kea)
        except LeaseQueryGuardError as exc:
            messages.warning(request, lease_query_guard_message(exc, state_filter))
            return redirect(request.path)
        except KeaException as exc:
            logger.exception("Failed to fetch leases for export on server %s", instance.pk)
            messages.error(request, kea_error_hint(exc))
            return redirect(request.path)
        except (requests.RequestException, ValueError):
            logger.exception("Transport/parse error fetching leases for export on server %s", instance.pk)
            messages.error(request, "Failed to fetch leases for export; see server logs.")
            return redirect(request.path)
        except RuntimeError:
            logger.exception("Unexpected error fetching leases for export on server %s", instance.pk)
            messages.error(request, "Failed to fetch leases for export; see server logs.")
            return redirect(request.path)

        if state_filter is not None and by not in (constants.BY_SUBNET, constants.BY_SUBNET_ID):
            leases = [ls for ls in leases if ls.get("state") == state_filter]

        table = self.get_table(leases, request)
        return export_table(table, "leases.csv", use_selected_columns=request.GET["export"] == "table")

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
            collection = client.lease_get_all(
                self.dhcp_version,
                per_page=per_page,
                max_leases=_LEASE_EXPORT_MAX_LEASES,
            )
        except KeaException as exc:
            logger.exception("Failed to fetch all leases for export on server %s", instance.pk)
            messages.error(request, kea_error_hint(exc))
            return redirect(request.path)
        except (requests.RequestException, ValueError, RuntimeError):
            logger.exception("Transport/parse error fetching all leases for export on server %s", instance.pk)
            messages.error(request, "Failed to fetch leases for export; see server logs.")
            return redirect(request.path)

        if collection.truncated:
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

        all_leases = format_leases(collection.leases)
        table = self.get_table(all_leases, request)
        return export_table(table, "leases_all.csv", use_selected_columns=False)

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

        try:
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

            by = form.cleaned_data["by"]
            q = form.cleaned_data["q"]
            state_filter: int | None = form.cleaned_data.get("state")
            client = instance.get_client(version=self.dhcp_version)
            is_subnet_search = by in (constants.BY_SUBNET, constants.BY_SUBNET_ID)
            if by == "":
                leases, next_page = self.get_leases_page(
                    client,
                    form.cleaned_data["page"],
                    per_page=get_paginate_count(request),
                )
                paginate = True
            else:
                paginate = is_subnet_search
                next_page = None
                state_in_kea = state_filter if is_subnet_search else None
                leases = self.get_leases(
                    client,
                    str(q.cidr) if by == constants.BY_SUBNET else q,
                    by,
                    state=state_in_kea,
                )

            # Apply optional state filter (client-side, after fetch).
            if state_filter is not None and not is_subnet_search:
                leases = [ls for ls in leases if ls.get("state") == state_filter]

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
            if is_subnet_search:
                visible_leases = [row.record for row in table.paginated_rows]
                next_page = table.page.next_page_number() if table.page.has_next() else None

            # Enrich only the visible table page with reservation badges and NetBox IPAM status.
            _enrich_leases_with_badges(
                visible_leases,
                instance,
                self.dhcp_version,
                can_delete=can_delete,
                can_change=can_change,
            )

            if not can_delete:
                table.columns.hide("pk")

            stripped_return_url = _strip_empty_params(request.get_full_path())
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
                    "paginate": paginate,
                    "page_lengths": EnhancedPaginator.default_page_lengths,
                },
            )
            # Tell HTMX which URL to push to the browser history.  The request
            # URL may include empty params (e.g. state=) that HTMX would otherwise
            # push verbatim; sending the stripped URL as HX-Push-Url overrides
            # that so the address bar always shows the clean URL.
            response["HX-Push-Url"] = stripped_return_url
            return response
        except LeaseQueryGuardError as exc:
            logger.info("Rejected unsafe Subnet lease query on server %s: %s", instance.pk, exc)
            form.add_error("state", lease_query_guard_message(exc, form.cleaned_data.get("state")))
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

    def delete_lease(self, client: KeaClient, ip: str) -> None:
        """Issue a lease-del command to Kea for *ip*; silently accepts result 3 (not found)."""
        client.command(
            f"lease{self.dhcp_version}-del",
            arguments={"ip-address": ip},
            service=[f"dhcp{self.dhcp_version}"],
            check=(0, 3),
        )

    def get(self, request: HttpRequest, **kwargs):
        """Redirect back to the server on GET (this view is POST-only)."""
        return redirect(self.get_return_url(request, obj=self.get_object(**kwargs)))

    def post(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Show confirmation page or delete leases if confirmed."""
        instance: Server = self.get_object(**kwargs)

        if resp := check_dhcp_enabled(instance, self.dhcp_version):
            return resp

        if not request.user.has_perm("netbox_kea.bulk_delete_lease_from_server", obj=instance):
            return HttpResponseForbidden("This user does not have permission to delete DHCP leases.")

        form = self.form(request.POST)

        if not form.is_valid():
            messages.warning(request, str(form.errors))
            return redirect(_strip_empty_params(self.get_return_url(request, obj=instance)))

        lease_ips = form.cleaned_data["pk"]
        return_url = _strip_empty_params(self.get_return_url(request, obj=instance))
        if "_confirm" not in request.POST:
            return render(
                request,
                "generic/bulk_delete.html",
                {
                    "model": FakeLeaseModel,
                    "table": tables.LeaseDeleteTable(
                        ({"ip": ip} for ip in lease_ips),
                        orderable=False,
                    ),
                    "form": form,
                    "return_url": return_url,
                },
            )

        try:
            client = instance.get_client(version=self.dhcp_version)
        except (ValueError, requests.RequestException):
            logger.exception("Failed to create Kea client for server %s", instance.pk)
            messages.error(request, "Failed to connect to Kea: see server logs for details.")
            return redirect(return_url)

        successful_ips: list[str] = []
        for ip in lease_ips:
            try:
                self.delete_lease(client, ip)
                successful_ips.append(ip)
            except KeaException as exc:  # noqa: PERF203
                logger.exception("Kea error deleting lease %s on server %s", ip, instance.pk)
                messages.error(request, f"Error deleting lease {ip}: {kea_error_hint(exc)}")
            except (requests.RequestException, ValueError):  # noqa: PERF203
                logger.exception("Error deleting lease %s on server %s", ip, instance.pk)
                messages.error(request, f"Error deleting lease {ip}: see server logs for details.")

        if successful_ips:
            messages.success(request, f"Deleted {len(successful_ips)} DHCPv{self.dhcp_version} lease(s).")
            try:
                _add_lease_journal(instance, request.user, "deleted", successful_ips)
            except DatabaseError:
                logger.exception("Failed to record lease journal for server %s; continuing", instance.pk)
            leases_deleted.send_robust(
                sender=None,
                server=instance,
                ip_addresses=successful_ips,
                dhcp_version=self.dhcp_version,
                request=request,
            )

        failed_count = len(lease_ips) - len(successful_ips)
        if failed_count:
            messages.warning(request, f"Failed to delete {failed_count} lease(s). See above for details.")
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


class _BaseLeaseEditView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Base view for editing a single lease via ``lease{v}-update``.

    Subclasses must set ``dhcp_version`` and ``form_class``.
    """

    dhcp_version: int
    form_class: type

    def _get_server(self, pk: int) -> Server:
        return get_object_or_404(Server.objects.restrict(self.request.user, "view"), pk=pk)

    def _leases_url(self, server: Server) -> str:
        return reverse(
            f"plugins:netbox_kea:server_leases{self.dhcp_version}",
            kwargs={"pk": server.pk},
        )

    def get(self, request: HttpRequest, pk: int, ip_address: str) -> HttpResponse:
        """Render the edit form pre-filled with the current lease values."""
        server = self._get_server(pk)

        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp

        try:
            client = server.get_client(version=self.dhcp_version)
            lease = client.lease_get_by_ip(self.dhcp_version, ip_address)
        except KeaException as exc:
            logger.exception("Failed to fetch lease %s on server %s", ip_address, pk)
            messages.error(request, kea_error_hint(exc))
            return redirect(self._leases_url(server))
        except (requests.RequestException, RuntimeError, ValueError):
            logger.exception("Failed to fetch lease %s on server %s", ip_address, pk)
            messages.error(request, "Failed to fetch lease: see server logs for details.")
            return redirect(self._leases_url(server))

        if lease is None:
            messages.warning(request, f"Lease {ip_address} not found.")
            return redirect(self._leases_url(server))
        initial = {
            "hostname": lease.get("hostname", ""),
            "valid_lft": lease.get("valid-lft"),
        }
        if self.dhcp_version == 4:
            initial["hw_address"] = lease.get("hw-address", "")
        else:
            initial["duid"] = lease.get("duid", "")

        form = self.form_class(initial=initial)
        return render(
            request,
            "netbox_kea/server_lease_edit.html",
            {
                "object": server,
                "server": server,
                "ip_address": ip_address,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._leases_url(server),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, ip_address: str) -> HttpResponse:
        """Validate form and apply the update via ``lease{v}-update``."""
        server = self._get_server(pk)

        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp

        form = self.form_class(request.POST)
        if not form.is_valid():
            return render(
                request,
                "netbox_kea/server_lease_edit.html",
                {
                    "object": server,
                    "server": server,
                    "ip_address": ip_address,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "cancel_url": self._leases_url(server),
                    "tab": self.tab,
                },
            )
        cd = form.cleaned_data
        kwargs: dict[str, object] = {}
        if cd.get("hostname") is not None:
            kwargs["hostname"] = cd["hostname"]
        if cd.get("valid_lft") is not None:
            kwargs["valid_lft"] = cd["valid_lft"]
        if self.dhcp_version == 4 and cd.get("hw_address"):
            kwargs["hw_address"] = cd["hw_address"]
        elif self.dhcp_version == 6 and cd.get("duid"):
            kwargs["duid"] = cd["duid"]
        try:
            client = server.get_client(version=self.dhcp_version)
            client.lease_update(self.dhcp_version, ip_address, **kwargs)
            messages.success(request, f"Lease {ip_address} updated.")
        except KeaException as exc:
            logger.exception("Error updating lease %s", ip_address)
            messages.error(request, kea_error_hint(exc))
        except (requests.RequestException, ValueError):
            logger.exception("Error updating lease %s (transport/parse error)", ip_address)
            messages.error(request, "Failed to update lease: see server logs for details.")
        return redirect(self._leases_url(server))


@register_model_view(Server, "lease4_edit", path="leases4/<path:ip_address>/edit")
class ServerLease4EditView(_BaseLeaseEditView):
    """Edit a single DHCPv4 lease."""

    dhcp_version = 4
    form_class = forms.Lease4EditForm
    tab = _LEASES_TAB


@register_model_view(Server, "lease6_edit", path="leases6/<path:ip_address>/edit")
class ServerLease6EditView(_BaseLeaseEditView):
    """Edit a single DHCPv6 lease."""

    dhcp_version = 6
    form_class = forms.Lease6EditForm
    tab = _LEASES_TAB


class _BaseLeaseAddView(_KeaChangeMixin, generic.ObjectView):
    """Base view for creating a new lease via ``lease{v}-add``."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_lease_add.html"
    dhcp_version: int
    form_class: type
    # Use _active_tab (not `tab`) so model_view_tabs does not register this as a
    # duplicate navigation entry — the add view URL resolves with pk-only, which
    # would cause the parent list tab to appear twice in the tab bar.
    _active_tab: OptionalViewTab

    def _leases_url(self, server: Server) -> str:
        return reverse(f"plugins:netbox_kea:server_leases{self.dhcp_version}", args=[server.pk])

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the empty add form."""
        server = self.get_object(pk=pk)

        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp

        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": self.form_class(),
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._leases_url(server),
                "tab": self._active_tab,
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate form and create the lease via Kea."""
        server = self.get_object(pk=pk)

        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp

        form = self.form_class(request.POST)
        cancel_url = self._leases_url(server)
        if form.is_valid():
            cd = form.cleaned_data
            lease: dict[str, Any] = {"ip-address": cd["ip_address"]}
            if cd.get("subnet_id"):
                lease["subnet-id"] = cd["subnet_id"]
            if cd.get("valid_lft") is not None:
                lease["valid-lft"] = cd["valid_lft"]
            if cd.get("hostname"):
                lease["hostname"] = cd["hostname"]
            if self.dhcp_version == 4:
                if cd.get("hw_address"):
                    lease["hw-address"] = cd["hw_address"]
            else:
                lease["duid"] = cd["duid"]
                lease["iaid"] = cd["iaid"]
            try:
                client = server.get_client(version=self.dhcp_version)
                client.lease_add(self.dhcp_version, lease)
            except KeaException as exc:
                logger.exception("Failed to create DHCPv%s lease for %s", self.dhcp_version, cd.get("ip_address"))
                messages.error(request, kea_error_hint(exc))
                return render(
                    request,
                    self.template_name,
                    {
                        "object": server,
                        "form": form,
                        "dhcp_version": self.dhcp_version,
                        "cancel_url": cancel_url,
                        "tab": self._active_tab,
                    },
                )
            except requests.RequestException:
                logger.exception("Failed to create DHCPv%s lease for %s", self.dhcp_version, cd.get("ip_address"))
                messages.error(request, "Failed to create lease: see server logs for details.")
                return render(
                    request,
                    self.template_name,
                    {
                        "object": server,
                        "form": form,
                        "dhcp_version": self.dhcp_version,
                        "cancel_url": cancel_url,
                        "tab": self._active_tab,
                    },
                )
            except ValueError:
                logger.exception(
                    "Failed to create DHCPv%s lease for %s (parse error)", self.dhcp_version, cd.get("ip_address")
                )
                messages.error(request, "Failed to create lease: invalid response from Kea.")
                return render(
                    request,
                    self.template_name,
                    {
                        "object": server,
                        "form": form,
                        "dhcp_version": self.dhcp_version,
                        "cancel_url": cancel_url,
                        "tab": self._active_tab,
                    },
                )
            # Lease created in Kea — run post-create side effects.
            messages.success(request, f"Lease for {cd['ip_address']} created.")
            try:
                _add_lease_journal(
                    server,
                    request.user,
                    "added",
                    cd["ip_address"],
                    hw_address=cd.get("hw_address") or "",
                    hostname=cd.get("hostname") or "",
                    duid=cd.get("duid") or "",
                )
            except (DatabaseError, OperationalError, ProgrammingError):
                logger.exception("Failed to record journal entry for lease %s", cd.get("ip_address"))
            lease_added.send_robust(
                sender=None,
                server=server,
                ip_address=cd["ip_address"],
                hw_address=cd.get("hw_address") or "",
                hostname=cd.get("hostname") or "",
                dhcp_version=self.dhcp_version,
                request=request,
            )
            if cd.get("sync_to_netbox"):
                _run_lease_sync_to_netbox(request, lease, cd["ip_address"])
            return redirect(cancel_url)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "cancel_url": cancel_url,
                "tab": self._active_tab,
            },
        )


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


_LEASE_RESERVATION_IDENTIFIERS = {
    4: (("hw-address", "hw_address"), ("client-id", "client_id")),
    6: (("duid", "duid"), ("hw-address", "hw_address")),
}


def _lease_reservation_identities(lease: dict[str, Any], version: int) -> tuple[ReservationIdentity, ...]:
    identities = []
    for identifier_type, lease_key in _LEASE_RESERVATION_IDENTIFIERS[version]:
        value = lease.get(lease_key)
        if not value:
            continue
        try:
            identity = ReservationIdentity(identifier_type, value)
        except ValueError:
            continue
        if identity not in identities:
            identities.append(identity)
    return tuple(identities)


class _IdentityLookups:
    """Resolve each ``(Scope, Identity)`` Reservation lookup once for a whole lease page.

    Leases that share a device repeat the same identities, and Kea answers each of
    those queries identically for the life of one page.
    """

    def __init__(self) -> None:
        self._registry = threading.Lock()
        self._locks: dict[Any, threading.Lock] = {}
        self._results: dict[Any, Reservation | None] = {}

    def resolve(self, key: Any, lookup: Callable[[], Reservation | None]) -> Reservation | None:
        """Return the memoized result for *key*, calling *lookup* at most once."""
        with self._registry:
            entry = self._locks.setdefault(key, threading.Lock())
        with entry:
            if key not in self._results:
                self._results[key] = lookup()
            return self._results[key]


def _close_worker_client(client: KeaClient) -> None:
    """Close one worker client, reporting a failure instead of raising it."""
    try:
        client.close()
    except Exception:  # noqa: BLE001
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


def _reservation_for_lease_worker(worker_clients, version, catalogue, lease, lookups: _IdentityLookups):
    """Resolve one lease to a typed Reservation in a thread-local client."""
    ip = lease.get("ip_address", "")
    subnet_id = lease.get("subnet_id")
    if not ip or isinstance(subnet_id, bool) or not isinstance(subnet_id, int):
        return ip, None, None
    subnet = catalogue.find_by_id(subnet_id)
    if subnet is None:
        return ip, None, None
    scope = InSubnetReservationScope(subnet.identity)
    identities = _lease_reservation_identities(lease, version)
    worker_client = worker_clients.get()
    try:
        reservation = worker_client.reservation_by_address(version, catalogue, scope, ip)
        if reservation is not None:
            return ip, reservation, True
        for identity_scope in (scope, GlobalReservationScope()):
            for identity in identities:
                reservation = lookups.resolve(
                    (identity_scope, identity),
                    partial(worker_client.reservation_by_identity, version, catalogue, identity_scope, identity),
                )
                if reservation is not None:
                    return ip, reservation, True
        return ip, None, True
    except KeaException as exc:
        if exc.response.get("result") == 2:
            return ip, None, False
        logger.debug("Reservation lookup failed for lease %s", ip, exc_info=True)
        return ip, None, None
    except (requests.RequestException, RuntimeError, ValueError):
        logger.debug("Reservation lookup failed for lease %s", ip, exc_info=True)
        return ip, None, None


def _fetch_reservations_for_leases(
    client: KeaClient,
    version: int,
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


def _canonical_reservation_url(server_pk: int, reservation: Reservation) -> str | None:
    if isinstance(reservation.scope, GlobalReservationScope):
        return None
    query = _urlencode(
        {
            "identifier_type": reservation.identity.identifier_type,
            "identifier": reservation.identity.value,
        }
    )
    base = reverse(
        f"plugins:netbox_kea:server_reservation{reservation.family}_edit",
        args=[server_pk, reservation.scope.subnet.subnet_id],
    )
    return f"{base}?{query}"


def _set_lease_reservation_fields(
    lease: dict[str, Any],
    reservation: Reservation | None,
    server_pk: int,
    version: int,
    subnet_cidr: str | None,
    host_cmds_available: bool,
    failed_ips: set[str],
    can_change: bool,
) -> None:
    """Set one lease row from its typed canonical Reservation match."""
    ip = lease.get("ip_address", "")
    lease.update(
        {
            "is_reserved": reservation is not None,
            "reservation_url": None,
            "create_reservation_url": None,
            "pending_ip_change": False,
            "pending_reservation_ip": "",
            "stale_mac": False,
            "stale_lease_mac": "",
            "reservation_mac": "",
            "delete_lease_url": "",
            "can_change_reservation": False,
        }
    )
    if reservation is not None:
        lease["reservation_url"] = _canonical_reservation_url(server_pk, reservation)
        lease["can_change_reservation"] = can_change and lease["reservation_url"] is not None
        if (
            isinstance(reservation.scope, InSubnetReservationScope)
            and reservation.addresses
            and all(str(address) != ip for address in reservation.addresses)
        ):
            lease["pending_ip_change"] = True
            lease["pending_reservation_ip"] = str(reservation.addresses[0])
        if (
            ip in {str(address) for address in reservation.addresses}
            and reservation.identity.identifier_type == "hw-address"
        ):
            lease_hw = _lease_reservation_identities(lease, version)
            lease_hw_value = next(
                (identity.value for identity in lease_hw if identity.identifier_type == "hw-address"), ""
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
    params = {
        "subnet_cidr": subnet_cidr,
        "ip_addresses" if version == 6 else "ip_address": ip,
        "hostname": lease.get("hostname", ""),
    }
    identities = _lease_reservation_identities(lease, version)
    if identities:
        params["identifier_type"] = identities[0].identifier_type
        params["identifier"] = identities[0].value
    base = reverse(f"plugins:netbox_kea:server_reservation{version}_add", args=[server_pk])
    lease["create_reservation_url"] = f"{base}?{_urlencode({key: value for key, value in params.items() if value})}"


def _enrich_leases_with_badges(
    leases: list[dict[str, Any]], server: "Server", version: int, can_delete: bool = False, can_change: bool = False
) -> None:
    """In-place: add reservation and NetBox IPAM badge fields to lease dicts.

    Adds:
    - ``reservation_url``: reservation link if a reservation exists for this IP
    - ``can_change_reservation``: whether the user may edit the reservation (gates link vs plain badge)
    - ``create_reservation_url``: pre-filled add link if host_cmds is loaded
    - ``netbox_ip_url``: absolute URL if IP exists in NetBox IPAM
    - ``sync_url``: POST endpoint URL to create a NetBox IP when absent
    - ``can_delete``: whether the current user may delete this lease
    - ``can_change``: whether the current user may edit this lease (gates edit_url)
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
    except KeaException as exc:
        if exc.response.get("result") == 2:
            host_cmds_available = False
        else:
            failed_ips = {lease.get("ip_address", "") for lease in leases}
            logger.warning("reservation lookup failed during lease enrichment: %s", exc)
    except Exception as exc:  # noqa: BLE001
        failed_ips = {lease.get("ip_address", "") for lease in leases}
        logger.warning("unexpected error during lease enrichment: %s", exc, exc_info=True)

    for lease in leases:
        ip = lease.get("ip_address", "")
        subnet = catalogue.find_by_id(lease.get("subnet_id")) if catalogue is not None else None
        _set_lease_reservation_fields(
            lease,
            reservation_by_ip.get(ip),
            server.pk,
            version,
            subnet.cidr if subnet is not None else None,
            host_cmds_available,
            failed_ips,
            can_change,
        )

    sync_url = reverse(f"plugins:netbox_kea:server_lease{version}_sync", args=[server.pk])
    edit_url_name = f"plugins:netbox_kea:server_lease{version}_edit"
    nb_ips = bulk_fetch_netbox_ips([lease.get("ip_address", "") for lease in leases if lease.get("ip_address")])
    for lease in leases:
        ip = lease.get("ip_address", "")
        nb_ip = nb_ips.get(ip)
        if nb_ip:
            lease["netbox_ip_url"] = nb_ip.get_absolute_url()
        elif can_change and host_cmds_available and not lease.get("pending_ip_change") and not lease.get("stale_mac"):
            # Don't offer Sync for leases with indeterminate reservation state.
            if ip not in failed_ips:
                lease["sync_url"] = sync_url
        if ip and can_change:
            lease["edit_url"] = reverse(edit_url_name, args=[server.pk, ip])
        lease["can_delete"] = can_delete
        lease["can_change"] = can_change
