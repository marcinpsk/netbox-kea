import concurrent.futures
import ipaddress
import logging
import uuid
from abc import ABCMeta
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
from netaddr import IPAddress, IPNetwork
from netbox.tables import BaseTable
from netbox.views import generic
from utilities.exceptions import AbortRequest
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.views import GetReturnURLMixin, register_model_view

from .. import constants, forms, tables
from ..kea import KeaClient, KeaException
from ..models import Server
from ..signals import lease_added, leases_deleted
from ..sync import sync_lease_to_netbox
from ..utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    export_table,
    format_leases,
    kea_error_hint,
)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin, _strip_empty_params

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseTable)


def _is_valid_lease_entry(entry: dict) -> bool:
    """Return True if *entry* is a dict with a valid IP in ``ip-address``."""
    if not isinstance(entry, dict):
        return False
    addr = entry.get("ip-address")
    if not isinstance(addr, str):
        return False
    try:
        ipaddress.ip_address(addr)
    except ValueError:
        logger.warning("Skipping lease entry with invalid ip-address: %r", addr)
        return False
    return True


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

    def get_leases_page(
        self, client: KeaClient, subnet: IPNetwork, page: str | None, per_page: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch one page of leases for *subnet* and return ``(leases, next_cursor)``."""
        if page:
            frm = page
        elif int(subnet.network) == 0:
            frm = str(subnet.network)
        else:
            frm = str(subnet.network - 1)

        resp = client.command(
            f"lease{self.dhcp_version}-get-page",
            service=[f"dhcp{self.dhcp_version}"],
            arguments={"from": frm, "limit": per_page},
            check=(0, 3),
        )

        if not resp or not isinstance(resp[0], dict):
            raise RuntimeError("Unexpected response shape from lease-get-page")

        if resp[0]["result"] == 3:
            return [], None

        args = resp[0].get("arguments")
        if not isinstance(args, dict):
            raise RuntimeError("Unexpected None arguments from lease-get-page")

        original_leases = args.get("leases")
        if not isinstance(original_leases, list):
            raise RuntimeError("Unexpected leases payload from lease-get-page")
        raw_leases = [entry for entry in original_leases if _is_valid_lease_entry(entry)]

        count = args.get("count")
        if not isinstance(count, int):
            raise RuntimeError("Missing or non-int count in lease-get-page response")

        if count == per_page and not raw_leases:
            logger.warning(
                "lease-get-page returned %d items but none had a valid ip-address; aborting pagination",
                len(original_leases),
            )
            raise RuntimeError("Unexpected empty leases after filtering on full page")

        # Derive cursor from original Kea response to avoid rewinding on filtered entries
        if count == per_page and original_leases:
            last = original_leases[-1]
            next_cursor = str(last["ip-address"]) if _is_valid_lease_entry(last) else None
        else:
            next_cursor = None
        for i, lease in enumerate(raw_leases):
            lease_ip = IPAddress(lease["ip-address"])
            if lease_ip not in subnet:
                raw_leases = raw_leases[:i]
                next_cursor = None
                break

        subnet_leases = format_leases(raw_leases)

        return subnet_leases, next_cursor

    def get_leases(self, client: KeaClient, q: Any, by: str) -> list[dict[str, Any]]:
        """Query Kea for leases matching *q* by search attribute *by*."""
        arguments: dict[str, Any]
        command = ""
        multiple = True

        if by == constants.BY_IP:
            arguments = {"ip-address": q}
            multiple = False
        elif by == constants.BY_HW_ADDRESS:
            arguments = {"hw-address": q}
            command = "-by-hw-address"
        elif by == constants.BY_HOSTNAME:
            arguments = {"hostname": q}
            command = "-by-hostname"
        elif by == constants.BY_CLIENT_ID:
            arguments = {"client-id": q}
            command = "-by-client-id"
        elif by == constants.BY_SUBNET_ID:
            command = "-all"
            arguments = {"subnets": [int(q)]}
        elif by == constants.BY_DUID:
            command = "-by-duid"
            arguments = {"duid": q}
        else:
            # We should never get here because the
            # form should of been validated.
            raise AbortRequest(f"Invalid search by (this shouldn't happen): {by}")
        resp = client.command(
            f"lease{self.dhcp_version}-get{command}",
            service=[f"dhcp{self.dhcp_version}"],
            arguments=arguments,
            check=(0, 3),
        )

        if not resp or not isinstance(resp[0], dict):
            raise RuntimeError(f"Unexpected response shape from lease{self.dhcp_version}-get{command}")

        if resp[0]["result"] == 3:
            return []

        args = resp[0].get("arguments")
        if not isinstance(args, dict):
            raise RuntimeError(f"Unexpected None arguments from lease{self.dhcp_version}-get{command}")
        if multiple is True:
            raw_leases = args.get("leases")
            if not isinstance(raw_leases, list):
                raise RuntimeError(f"Unexpected leases payload from lease{self.dhcp_version}-get{command}")
            raw_leases = [entry for entry in raw_leases if isinstance(entry, dict)]
            raw_leases = [entry for entry in raw_leases if _is_valid_lease_entry(entry)]
            if not raw_leases:
                raise RuntimeError(f"No valid lease dicts in lease{self.dhcp_version}-get{command} response")
            return format_leases(raw_leases)
        if "ip-address" not in args:
            raise RuntimeError(f"Single-result lease{self.dhcp_version}-get{command} response missing 'ip-address'")
        return format_leases([args])

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Return an empty table, the search form, and the add-lease URL for the initial (non-HTMX) page load."""
        # For non-htmx requests.

        table = self.get_table([], request)
        form = self.form(request.GET) if "q" in request.GET else self.form()
        can_change = Server.objects.restrict(request.user, "change").filter(pk=instance.pk).exists()
        ctx: dict[str, Any] = {
            "form": form,
            "table": table,
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
        form = self.form(request.GET)
        if not form.is_valid():
            messages.warning(request, "Invalid form for export.")
            return redirect(request.path)

        instance = self.get_object(**kwargs)

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
            if by == constants.BY_SUBNET:
                leases = []
                page: str | None = ""  # start from the beginning
                while page is not None:
                    page_leases, page = self.get_leases_page(
                        client,
                        q,
                        page,
                        per_page=get_paginate_count(request),
                    )
                    leases += page_leases
            else:
                leases = self.get_leases(client, q, by)
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

        if state_filter is not None:
            leases = [ls for ls in leases if ls.get("state") == state_filter]

        table = self.get_table(leases, request)
        return export_table(table, "leases.csv", use_selected_columns=request.GET["export"] == "table")

    def get_export_all(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Export every lease on the server (no search filter) as a CSV download.

        Paginates through ``lease{v}-get-page`` from the beginning until all
        leases have been fetched, then streams them as a CSV file.
        Requires the ``lease_cmds`` hook to be loaded on the Kea server.
        """
        instance = self.get_object(**kwargs)

        start_ip = "0.0.0.0" if self.dhcp_version == 4 else "::"
        per_page = 1000

        all_leases: list[dict[str, Any]] = []
        cursor = start_ip
        try:
            client = instance.get_client(version=self.dhcp_version)
            while True:
                resp = client.command(
                    f"lease{self.dhcp_version}-get-page",
                    service=[f"dhcp{self.dhcp_version}"],
                    arguments={"from": cursor, "limit": per_page},
                    check=(0, 3),
                )
                if not resp or not isinstance(resp[0], dict):
                    raise RuntimeError("Unexpected response shape from lease-get-page (export)")
                if resp[0]["result"] == 3:
                    break

                args = resp[0].get("arguments")
                if not isinstance(args, dict):
                    logger.error("lease-get-page returned non-dict arguments on server %s", instance.pk)
                    messages.error(request, "Failed to fetch leases for export: unexpected Kea response.")
                    return redirect(request.path)

                original_leases = args.get("leases")
                if not isinstance(original_leases, list):
                    logger.error("lease-get-page returned non-list leases on server %s", instance.pk)
                    messages.error(request, "Failed to fetch leases for export: unexpected Kea response.")
                    return redirect(request.path)
                raw_leases = [entry for entry in original_leases if _is_valid_lease_entry(entry)]

                count = args.get("count")
                if not isinstance(count, int):
                    logger.error("lease-get-page returned non-int count on server %s; aborting export", instance.pk)
                    messages.error(request, "Failed to fetch leases for export: unexpected Kea response.")
                    return redirect(request.path)

                all_leases += format_leases(raw_leases)

                if not raw_leases:
                    if count >= per_page:
                        logger.error(
                            "lease-get-page returned %d items but none had a valid ip-address on server %s; aborting export",
                            len(original_leases),
                            instance.pk,
                        )
                        messages.error(request, "Failed to fetch leases for export: unexpected Kea response.")
                        return redirect(request.path)
                    break

                if count < per_page:
                    break
                # Derive cursor from original Kea response to avoid rewinding on filtered entries
                last = original_leases[-1]
                if not _is_valid_lease_entry(last):
                    raise RuntimeError(
                        f"Export aborted: last lease entry on full page is malformed for server {instance.pk}"
                    )
                cursor = str(last["ip-address"])
        except KeaException as exc:
            logger.exception("Failed to fetch all leases for export on server %s", instance.pk)
            messages.error(request, kea_error_hint(exc))
            return redirect(request.path)
        except (requests.RequestException, ValueError, RuntimeError):
            logger.exception("Transport/parse error fetching all leases for export on server %s", instance.pk)
            messages.error(request, "Failed to fetch leases for export; see server logs.")
            return redirect(request.path)

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
            form = self.form(request.GET)
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
            if by == "subnet":
                leases, next_page = self.get_leases_page(
                    client,
                    q,
                    form.cleaned_data["page"],
                    per_page=get_paginate_count(request),
                )
                paginate = True
            else:
                paginate = False
                next_page = None
                leases = self.get_leases(client, q, by)

            # Apply optional state filter (client-side, after fetch).
            if state_filter is not None:
                leases = [ls for ls in leases if ls.get("state") == state_filter]

            can_delete = request.user.has_perm(
                "netbox_kea.bulk_delete_lease_from_server",
                obj=instance,
            )
            can_change = request.user.has_perm(
                "netbox_kea.change_server",
                obj=instance,
            )

            # Enrich leases with reservation badges + NetBox IPAM status.
            # Extracted helper so combined views get the same treatment.
            _enrich_leases_with_badges(
                leases, instance, self.dhcp_version, can_delete=can_delete, can_change=can_change
            )

            table = self.get_table(leases, request)

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
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            error_id = str(uuid.uuid4())
            logger.exception("HTMX leases handler error [%s]", error_id)
            return render(
                request,
                "netbox_kea/exception_htmx.html",
                {"error_id": error_id},
            )


@register_model_view(Server, "leases6")
class ServerLeases6View(BaseServerLeasesView[tables.LeaseTable6]):
    """DHCPv6 leases tab for a Kea Server."""

    tab = OptionalViewTab(label="DHCPv6 Leases", weight=1015, is_enabled=lambda s: s.dhcp6)
    form = forms.Leases6SearchForm
    table = tables.LeaseTable6
    dhcp_version = 6


@register_model_view(Server, "leases4")
class ServerLeases4View(BaseServerLeasesView[tables.LeaseTable4]):
    """DHCPv4 leases tab for a Kea Server."""

    tab = OptionalViewTab(label="DHCPv4 Leases", weight=1010, is_enabled=lambda s: s.dhcp4)
    form = forms.Leases4SearchForm
    table = tables.LeaseTable4
    dhcp_version = 4


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


class ServerLeases4DeleteView(BaseServerLeasesDeleteView):
    """Bulk-delete view for DHCPv4 leases."""

    form = forms.Lease4DeleteForm
    dhcp_version = 4


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
            resp = client.command(
                f"lease{self.dhcp_version}-get",
                service=[f"dhcp{self.dhcp_version}"],
                arguments={"ip-address": ip_address},
                check=(0, 3),
            )
        except KeaException as exc:
            logger.exception("Failed to fetch lease %s on server %s", ip_address, pk)
            messages.error(request, kea_error_hint(exc))
            return redirect(self._leases_url(server))
        except (requests.RequestException, ValueError):
            logger.exception("Failed to fetch lease %s on server %s", ip_address, pk)
            messages.error(request, "Failed to fetch lease: see server logs for details.")
            return redirect(self._leases_url(server))

        if not resp or not isinstance(resp[0], dict):
            logger.warning("Unexpected response shape fetching lease %s on server %s: %r", ip_address, pk, resp)
            messages.error(request, "Unexpected response from Kea: see server logs for details.")
            return redirect(self._leases_url(server))

        if resp[0]["result"] == 3:
            messages.warning(request, f"Lease {ip_address} not found.")
            return redirect(self._leases_url(server))

        lease = resp[0].get("arguments")
        if not isinstance(lease, dict):
            logger.warning("Unexpected arguments in lease response for %s on server %s: %r", ip_address, pk, resp[0])
            messages.error(request, "Unexpected response from Kea: see server logs for details.")
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


@register_model_view(Server, "lease6_edit", path="leases6/<path:ip_address>/edit")
class ServerLease6EditView(_BaseLeaseEditView):
    """Edit a single DHCPv6 lease."""

    dhcp_version = 6
    form_class = forms.Lease6EditForm


class _BaseLeaseAddView(_KeaChangeMixin, generic.ObjectView):
    """Base view for creating a new lease via ``lease{v}-add``."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_lease_add.html"
    dhcp_version: int
    form_class: type

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
                try:
                    sync_lease_to_netbox(lease)
                    messages.success(request, f"IPAddress {cd['ip_address']} synced to NetBox.")
                except (ValueError, DatabaseError, ValidationError, requests.RequestException):
                    logger.exception("Failed to sync lease %s to NetBox", cd.get("ip_address"))
                    messages.warning(request, "Lease created but NetBox IPAM sync failed; see server logs.")
            return redirect(cancel_url)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "cancel_url": cancel_url,
            },
        )


@register_model_view(Server, "lease4_add", path="leases4/add")
class ServerLease4AddView(_BaseLeaseAddView):
    """Create a new DHCPv4 lease."""

    dhcp_version = 4
    form_class = forms.Lease4AddForm


@register_model_view(Server, "lease6_add", path="leases6/add")
class ServerLease6AddView(_BaseLeaseAddView):
    """Create a new DHCPv6 lease."""

    dhcp_version = 6
    form_class = forms.Lease6AddForm


def _fetch_reservation_by_ip(client: KeaClient, version: int) -> tuple[dict[str, dict], bool]:
    """Drain all reservation pages and return a mapping of IP → reservation dict.

    Returns ``(reservation_by_ip, host_cmds_available)``.
    """
    reservation_by_ip: dict[str, dict] = {}
    from_index = 0
    source_index = 0
    while True:
        page, next_from, next_source = client.reservation_get_page(
            f"dhcp{version}", limit=1000, source_index=source_index, from_index=from_index
        )
        for r in page:
            if "ip-address" in r:
                reservation_by_ip[r["ip-address"]] = r
            elif "ip-addresses" in r:
                for addr in r["ip-addresses"]:
                    reservation_by_ip[addr] = r
        if next_from == 0 and next_source == 0:
            break
        from_index = next_from
        source_index = next_source
    return reservation_by_ip, True


def _fetch_reservation_by_ip_for_leases(
    client: "KeaClient", version: int, leases: list[dict[str, Any]]
) -> tuple[dict[str, dict], bool, set[str]]:
    """Fetch reservations only for the IPs present in *leases* (targeted lookup).

    Uses individual ``reservation-get`` calls (one per lease) in parallel so
    only the IPs we actually care about are queried — avoiding a full
    reservation-page scan on servers with large reservation databases.

    Returns ``(reservation_by_ip, host_cmds_available, failed_ips)`` where
    *failed_ips* is the set of IPs where the lookup failed with a non-result-2
    error (indeterminate state — neither confirmed absent nor confirmed present).
    """
    service = f"dhcp{version}"
    reservation_by_ip: dict[str, dict] = {}
    host_cmds_available = True

    def _fetch_one(lease: dict) -> tuple[str, dict | None, bool | None]:
        ip = lease.get("ip_address", "")
        subnet_id = lease.get("subnet_id")
        if not ip or not subnet_id:
            return ip, None, None
        try:
            subnet_id = int(subnet_id)
        except (TypeError, ValueError):
            return ip, None, None
        with client.clone() as worker_client:  # requests.Session is not thread-safe
            try:
                r = worker_client.reservation_get(service, subnet_id=subnet_id, ip_address=ip)
                return ip, r, True
            except KeaException as exc:
                if exc.response.get("result") == 2:
                    return ip, None, False  # hook not available
                logger.debug("reservation-get KeaException for %s (result != 2): %s", ip, exc)
                return ip, None, None  # indeterminate — don't show create-reservation link
            except Exception as exc:  # noqa: BLE001
                logger.debug("reservation-get failed for %s: %s", ip, exc)
                return ip, None, None  # indeterminate — don't show create-reservation link

    if not leases:
        return reservation_by_ip, host_cmds_available, set()

    failed_ips: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(leases), 10)) as executor:
        futures = [executor.submit(_fetch_one, lease) for lease in leases]
        for future in concurrent.futures.as_completed(futures):
            ip, rsv, hook_ok = future.result()
            if hook_ok is False:
                host_cmds_available = False
            elif hook_ok is None:
                failed_ips.add(ip)
            if rsv is not None:
                reservation_by_ip[ip] = rsv

    return reservation_by_ip, host_cmds_available, failed_ips


def _build_mac_lookup_candidates(
    leases: list[dict[str, Any]],
    already_matched_ips: set[str],
    failed_ips: set[str],
) -> tuple[list[dict], set[tuple[str, int]]]:
    """Collect unique (mac, subnet_id) pairs for leases needing MAC-based lookup."""
    candidates: list[dict] = []
    seen_keys: set[tuple[str, int]] = set()
    for lease in leases:
        ip = lease.get("ip_address", "")
        if ip in already_matched_ips or ip in failed_ips:
            continue
        mac = (lease.get("hw_address") or "").lower()
        subnet_id = lease.get("subnet_id")
        if not mac or subnet_id is None:
            continue
        if not isinstance(subnet_id, int):
            continue
        key = (mac, subnet_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(lease)
    return candidates, seen_keys


def _fetch_reservation_by_mac_for_leases(
    client: "KeaClient",
    version: int,
    leases: list[dict[str, Any]],
    already_matched_ips: set[str],
    failed_ips: set[str],
) -> tuple[dict[tuple[str, int], dict], set[tuple[str, int]]]:
    """Fetch reservations by MAC for leases that had no IP-based reservation match.

    For each lease whose IP is not in *already_matched_ips* or *failed_ips*,
    queries Kea with ``identifier-type=hw-address`` to detect when a device's
    reservation exists at a **different** IP (pending IP change).

    Returns ``(reservation_by_mac, failed_keys)`` where ``reservation_by_mac``
    maps ``(hw_address, subnet_id) → reservation dict`` only for reservations
    whose IP **differs** from the lease IP (i.e. a pending change), and
    ``failed_keys`` is a set of ``(hw_address, subnet_id)`` tuples where the
    lookup failed.
    """
    service = f"dhcp{version}"
    reservation_by_mac: dict[tuple[str, int], dict] = {}
    failed_keys: set[tuple[str, int]] = set()

    candidates, _ = _build_mac_lookup_candidates(leases, already_matched_ips, failed_ips)
    if not candidates:
        return reservation_by_mac, failed_keys

    _FETCH_ERROR = object()  # sentinel to distinguish lookup errors from not-found

    def _fetch_one_mac(lease: dict) -> tuple[str, int, str, dict | None | object]:
        mac = (lease.get("hw_address") or "").lower()
        ip = lease.get("ip_address", "")
        try:
            subnet_id = int(lease.get("subnet_id"))
        except (TypeError, ValueError):
            return mac, 0, ip, _FETCH_ERROR
        with client.clone() as worker_client:
            try:
                r = worker_client.reservation_get(
                    service,
                    subnet_id=subnet_id,
                    identifier_type="hw-address",
                    identifier=mac,
                )
                if r is None:
                    return mac, subnet_id, ip, None
                # Check both ip-address (v4) and ip-addresses (v6) fields.
                rsv_ip = r.get("ip-address", "")
                rsv_ips = r.get("ip-addresses") or []
                if rsv_ip and rsv_ip != ip:
                    return mac, subnet_id, ip, r
                if not rsv_ip and rsv_ips and ip not in rsv_ips:
                    return mac, subnet_id, ip, r
                return mac, subnet_id, ip, None
            except Exception:  # noqa: BLE001
                logger.debug("reservation-get by MAC failed for %s: %s", mac, ip, exc_info=True)
                return mac, subnet_id, ip, _FETCH_ERROR

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(candidates), 10)) as executor:
        future_to_lease = {executor.submit(_fetch_one_mac, lease): lease for lease in candidates}
        for future in concurrent.futures.as_completed(future_to_lease):
            try:
                mac, subnet_id, ip, rsv = future.result()
            except Exception:  # noqa: BLE001
                lease = future_to_lease[future]
                l_mac = (lease.get("hw_address") or "").lower()
                l_sid = lease.get("subnet_id")
                if l_mac and isinstance(l_sid, int):
                    failed_keys.add((l_mac, l_sid))
                continue
            key = (mac, subnet_id)
            if rsv is _FETCH_ERROR:
                failed_keys.add(key)
            elif rsv is not None:
                reservation_by_mac[key] = rsv

    return reservation_by_mac, failed_keys


def _set_lease_reservation_fields(  # noqa: PLR0913
    lease: dict[str, Any],
    *,
    server_pk: int,
    version: int,
    rsv: dict | None,
    reservation_by_mac: dict[tuple[str, int], dict],
    host_cmds_available: bool,
    failed_ips: set[str],
    failed_mac_keys: set[tuple[str, int]],
    can_change: bool,
    reservation_url_name: str,
    add_url_name: str,
) -> None:
    """Set reservation-related badge fields on a single lease dict (in-place)."""
    ip = lease.get("ip_address", "")
    rsv_subnet_id = rsv.get("subnet-id") if isinstance(rsv, dict) else None
    lease["is_reserved"] = rsv is not None

    # Default pending-change and stale-MAC fields.
    lease["pending_ip_change"] = False
    lease["pending_reservation_ip"] = ""
    lease["stale_mac"] = False
    lease["stale_lease_mac"] = ""
    lease["reservation_mac"] = ""
    lease["delete_lease_url"] = ""
    lease["can_change_reservation"] = False

    if rsv is not None:
        if not isinstance(rsv, dict) or not isinstance(rsv_subnet_id, int):
            # Malformed reservation — treat as indeterminate, don't offer actions
            failed_ips.add(ip)
            lease["reservation_url"] = None
            lease["create_reservation_url"] = None
            lease["sync_url"] = None
            return
        _set_ip_matched_reservation(lease, rsv, server_pk, version, rsv_subnet_id, can_change, reservation_url_name)
    elif host_cmds_available and ip not in failed_ips:
        _set_unmatched_reservation(
            lease,
            server_pk,
            version,
            reservation_by_mac,
            failed_mac_keys,
            can_change,
            reservation_url_name,
            add_url_name,
        )


def _set_ip_matched_reservation(
    lease: dict[str, Any],
    rsv: dict,
    server_pk: int,
    version: int,
    rsv_subnet_id: int,
    can_change: bool,
    reservation_url_name: str,
) -> None:
    """Populate fields when the lease IP matches a reservation IP."""
    ip = lease.get("ip_address", "")
    lease["reservation_url"] = reverse(reservation_url_name, args=[server_pk, rsv_subnet_id, ip])
    lease["can_change_reservation"] = can_change
    lease["create_reservation_url"] = None

    # Stale MAC detection: lease MAC ≠ reservation MAC → device mismatch.
    lease_hw = (lease.get("hw_address") or "").lower()
    rsv_hw = (rsv.get("hw-address") or "").lower()
    if lease_hw and rsv_hw and lease_hw != rsv_hw:
        lease["stale_mac"] = True
        lease["stale_lease_mac"] = lease_hw
        lease["reservation_mac"] = rsv_hw
        lease["delete_lease_url"] = reverse(
            f"plugins:netbox_kea:server_leases{version}_delete",
            args=[server_pk],
        )


def _set_unmatched_reservation(
    lease: dict[str, Any],
    server_pk: int,
    version: int,
    reservation_by_mac: dict[tuple[str, int], dict],
    failed_mac_keys: set[tuple[str, int]],
    can_change: bool,
    reservation_url_name: str,
    add_url_name: str,
) -> None:
    """Populate fields when no reservation matched by IP — check MAC-based pending change."""
    ip = lease.get("ip_address", "")
    lease_hw = (lease.get("hw_address") or "").lower()
    subnet_id = lease.get("subnet_id")
    key = (lease_hw, subnet_id) if lease_hw and isinstance(subnet_id, int) else None

    # If the MAC lookup failed for this key, don't offer actions.
    if key and key in failed_mac_keys:
        lease["reservation_url"] = None
        lease["create_reservation_url"] = None
        lease["pending_ip_change"] = False
        lease["pending_reservation_ip"] = ""
        return

    mac_rsv = reservation_by_mac.get(key) if key else None

    if mac_rsv:
        # Pending IP change: device has a reservation at a different IP.
        pending_ip = mac_rsv.get("ip-address", "")
        if not pending_ip:
            # DHCPv6 reservations use ip-addresses (list)
            rsv_ips = mac_rsv.get("ip-addresses") or []
            pending_ip = rsv_ips[0] if rsv_ips else ""
        mac_rsv_subnet_id = mac_rsv.get("subnet-id")
        lease["pending_ip_change"] = True
        lease["pending_reservation_ip"] = pending_ip
        if isinstance(mac_rsv_subnet_id, int):
            lease["reservation_url"] = reverse(reservation_url_name, args=[server_pk, mac_rsv_subnet_id, pending_ip])
        else:
            lease["reservation_url"] = None
        lease["can_change_reservation"] = can_change
        lease["create_reservation_url"] = None
        return

    # No reservation at all — offer "+ Reserve" link.
    lease["reservation_url"] = None
    if can_change and isinstance(subnet_id, int):
        base_add = reverse(add_url_name, args=[server_pk])
        if version == 6:
            params = {
                k: v
                for k, v in {
                    "subnet_id": lease.get("subnet_id", ""),
                    "ip_addresses": ip,
                    "hostname": lease.get("hostname", ""),
                }.items()
                if v
            }
        else:
            params = {
                k: v
                for k, v in {
                    "subnet_id": lease.get("subnet_id", ""),
                    "ip_address": ip,
                    "identifier_type": "hw-address",
                    "identifier": lease.get("hw_address", ""),
                    "hostname": lease.get("hostname", ""),
                }.items()
                if v
            }
        lease["create_reservation_url"] = f"{base_add}?{_urlencode(params)}" if params else base_add
    else:
        lease["create_reservation_url"] = None


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

    reservation_url_name = f"plugins:netbox_kea:server_reservation{version}_edit"
    add_url_name = f"plugins:netbox_kea:server_reservation{version}_add"

    reservation_by_ip: dict[str, dict] = {}
    host_cmds_available = True
    failed_ips: set[str] = set()
    client: KeaClient | None = None
    try:
        client = server.get_client(version=version)
        reservation_by_ip, host_cmds_available, failed_ips = _fetch_reservation_by_ip_for_leases(
            client, version, leases
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

    # Phase 1b: MAC-based lookup for pending IP changes.
    # For leases without an IP-matched reservation, check if their MAC has a
    # reservation at a *different* IP — indicating the device should move.
    reservation_by_mac: dict[tuple[str, int], dict] = {}
    failed_mac_keys: set[tuple[str, int]] = set()
    if host_cmds_available and client is not None:
        try:
            reservation_by_mac, failed_mac_keys = _fetch_reservation_by_mac_for_leases(
                client, version, leases, set(reservation_by_ip.keys()), failed_ips
            )
        except Exception:  # noqa: BLE001
            logger.debug("MAC-based reservation lookup failed; skipping pending-change detection", exc_info=True)
            _, failed_mac_keys = _build_mac_lookup_candidates(leases, set(reservation_by_ip.keys()), failed_ips)

    for lease in leases:
        ip = lease.get("ip_address", "")
        rsv = reservation_by_ip.get(ip)
        _set_lease_reservation_fields(
            lease,
            server_pk=server.pk,
            version=version,
            rsv=rsv,
            reservation_by_mac=reservation_by_mac,
            host_cmds_available=host_cmds_available,
            failed_ips=failed_ips,
            failed_mac_keys=failed_mac_keys,
            can_change=can_change,
            reservation_url_name=reservation_url_name,
            add_url_name=add_url_name,
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
            mac = (lease.get("hw_address") or "").lower()
            subnet_id = lease.get("subnet_id")
            mac_key = (mac, subnet_id) if mac and isinstance(subnet_id, int) else None
            if ip not in failed_ips and (mac_key is None or mac_key not in failed_mac_keys):
                lease["sync_url"] = sync_url
        if ip and can_change:
            lease["edit_url"] = reverse(edit_url_name, args=[server.pk, ip])
        lease["can_delete"] = can_delete
        lease["can_change"] = can_change
