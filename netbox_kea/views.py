import concurrent.futures
import logging
import re
from abc import ABCMeta
from typing import Any, Generic, TypeVar
from urllib.parse import urlencode as _urlencode

from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.http.request import HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from netaddr import IPAddress, IPNetwork
from netbox.tables import BaseTable
from netbox.views import generic
from utilities.exceptions import AbortRequest
from utilities.htmx import htmx_partial
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.views import GetReturnURLMixin, ViewTab, register_model_view

try:
    from utilities.views import ConditionalLoginRequiredMixin
except ImportError:
    from django.contrib.auth.mixins import (
        LoginRequiredMixin as ConditionalLoginRequiredMixin,  # type: ignore[assignment]
    )

from . import constants, forms, tables
from .filtersets import ServerFilterSet
from .kea import KeaClient, KeaException, PartialPersistError
from .models import Server
from .sync import sync_lease_to_netbox, sync_reservation_to_netbox
from .utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    export_table,
    format_duration,
    format_leases,
    kea_error_hint,
    parse_reservation_csv,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseTable)

# Allowed characters in a pool range/CIDR string (digits, dots, colons, letters a-f, slash, hyphen).
# Protects the <path:pool> URL parameter from injection before it reaches the Kea API.
_POOL_RE = re.compile(r"^[0-9a-fA-F.:/-]{3,100}$")


def _get_global_options(server: "Server") -> dict[str, dict[str, str]]:
    """Return parsed global DHCP option-data for each enabled DHCP version.

    Calls ``config-get`` on each enabled DHCP service and extracts the
    top-level ``option-data`` block.  Any per-service failure is silently
    skipped so the status page always renders.

    Args:
        server: The Kea :class:`Server` to query.

    Returns:
        A ``{"DHCPv4": {field_name: value}, "DHCPv6": {...}}`` dict containing
        only the versions that returned valid options.

    """
    from .utilities import format_option_data

    svc_map: dict[str, tuple[str, int]] = {}
    if server.dhcp4:
        svc_map["DHCPv4"] = ("dhcp4", 4)
    if server.dhcp6:
        svc_map["DHCPv6"] = ("dhcp6", 6)

    result: dict[str, dict[str, str]] = {}
    for label, (svc, version) in svc_map.items():
        try:
            client = server.get_client(version=version)
            resp = client.command("config-get", service=[svc])
            dhcp_key = f"Dhcp{version}"
            dhcp_block = resp[0].get("arguments", {}).get(dhcp_key, {})
            option_data = dhcp_block.get("option-data", [])
            opts = format_option_data(option_data, version=version)
            if opts:
                # Convert snake_case keys to "Title Case" for display
                result[label] = {k.replace("_", " ").title(): v for k, v in opts.items()}
        except KeaException:  # noqa: PERF203
            logger.debug("config-get failed for %s (%s) — skipping global options", label, svc)
        except Exception:
            logger.warning("Unexpected error fetching global options for %s (%s)", label, svc, exc_info=True)
    return result


class _KeaChangeMixin:
    """Mixin that gates a view behind ``netbox_kea.change_server``.

    Applied to all views that mutate live Kea state (reservation/pool/subnet
    add, edit, delete).  Both GET (form display) and POST (form submit) are
    protected so users without write access never see the form.
    """

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        pk = kwargs.get("pk")
        if pk is not None:
            if request.user.is_authenticated:
                if not Server.objects.restrict(request.user, "view").filter(pk=pk).exists():
                    raise Http404
                if not Server.objects.restrict(request.user, "change").filter(pk=pk).exists():
                    return HttpResponseForbidden("You do not have permission to modify Kea server data.")
        elif request.user.is_authenticated and not request.user.has_perm("netbox_kea.change_server"):
            return HttpResponseForbidden("You do not have permission to modify Kea server data.")
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]


@register_model_view(Server)
class ServerView(generic.ObjectView):
    """Detail view for a single Kea Server."""

    queryset = Server.objects.all()


@register_model_view(Server, "edit")
class ServerEditView(generic.ObjectEditView):
    """Create/edit view for a Kea Server."""

    queryset = Server.objects.all()
    form = forms.ServerForm


@register_model_view(Server, "delete")
class ServerDeleteView(generic.ObjectDeleteView):
    """Delete confirmation view for a Kea Server."""

    queryset = Server.objects.all()


class ServerListView(generic.ObjectListView):
    """Paginated list view for all Kea Servers."""

    queryset = Server.objects.all()
    table = tables.ServerTable
    filterset = ServerFilterSet
    filterset_form = forms.ServerFilterForm


class ServerBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete view for Kea Server objects."""

    queryset = Server.objects.all()
    table = tables.ServerTable


class ServerBulkEditView(generic.BulkEditView):
    """Bulk-edit view for Kea Server objects."""

    queryset = Server.objects.all()
    filterset = ServerFilterSet
    table = tables.ServerTable
    form = forms.ServerBulkEditForm


class ServerBulkImportView(generic.BulkImportView):
    """Bulk-import CSV/YAML data to create Server objects."""

    queryset = Server.objects.all()
    model_form = forms.ServerImportForm
    table = tables.ServerTable


@register_model_view(Server, "status")
class ServerStatusView(generic.ObjectView):
    """Server status tab: shows daemon uptime, versions and HA state."""

    queryset = Server.objects.all()
    tab = ViewTab(label="Status", weight=1000)
    template_name = "netbox_kea/server_status.html"

    def _get_ca_status(self, client: KeaClient) -> dict[str, Any]:
        """Get the control agent status."""
        status = client.command("status-get")
        args = status[0]["arguments"]
        if not args:
            raise RuntimeError("Kea status-get returned empty arguments")

        version = client.command("version-get")
        version_args = version[0]["arguments"]
        if not version_args:
            raise RuntimeError("Kea version-get returned empty arguments")

        return {
            "PID": args["pid"],
            "Uptime": format_duration(int(args["uptime"])),
            "Time since reload": format_duration(int(args["reload"])),
            "Version": version_args["extended"],
        }

    def _get_dhcp_status(self, server: Server) -> dict[str, dict[str, Any]]:
        """Return status dicts for each enabled DHCP service, keyed by human-readable name.

        Each service is queried via its own protocol-aware client, so separate DHCPv4/v6
        daemon URLs are handled correctly without requiring a Control Agent.
        """
        resp: dict[str, dict[str, Any]] = {}
        service_names = {"dhcp6": "DHCPv6", "dhcp4": "DHCPv4"}
        services = []
        if server.dhcp6:
            services.append("dhcp6")
        if server.dhcp4:
            services.append("dhcp4")

        for svc in services:
            version = int(svc[-1])
            svc_client = server.get_client(version=version)
            status = svc_client.command("status-get", service=[svc])
            version_resp = svc_client.command("version-get", service=[svc])

            args = status[0]["arguments"]
            if args is None:
                raise RuntimeError(f"Unexpected None arguments from status-get for service {svc}")
            version_args = version_resp[0]["arguments"]
            if version_args is None:
                raise RuntimeError(f"Unexpected None arguments from version-get for service {svc}")

            entry: dict[str, Any] = {
                "PID": args["pid"],
                "Uptime": format_duration(args["uptime"]),
                "Time since reload": format_duration(int(args["reload"])),
                "Version": version_args["extended"],
            }

            if (ha := args.get("high-availability")) is not None:
                # https://kea.readthedocs.io/en/latest/arm/hooks.html#load-balancing-configuration
                # Note that while the top-level parameter high-availability is a list,
                # only a single entry is currently supported.
                ha_servers = ha[0].get("ha-servers")
                ha_local = ha_servers.get("local", {})
                ha_remote = ha_servers.get("remote", {})
                entry.update(
                    {
                        "HA mode": ha[0].get("ha-mode"),
                        "HA local role": ha_local.get("role"),
                        "HA local state": ha_local.get("state"),
                        "HA remote connection interrupted": str(ha_remote.get("connection-interrupted")),
                        "HA remote age (seconds)": ha_remote.get("age"),
                        "HA remote role": ha_remote.get("role"),
                        "HA remote last state": ha_remote.get("last-state"),
                        "HA remote in touch": ha_remote.get("in-touch"),
                        "HA remote unacked clients": ha_remote.get("unacked-clients"),
                        "HA remote unacked clients left": ha_remote.get("unacked-clients-left"),
                        "HA remote connecting clients": ha_remote.get("connecting-clients"),
                    }
                )
            resp[service_names[svc]] = entry
        return resp

    def _get_statuses(self, server: Server) -> dict[str, dict[str, Any]]:
        """Return combined status dicts for CA (when present) and all enabled DHCP services."""
        result: dict[str, dict[str, Any]] = {}
        if server.has_control_agent:
            result["Control Agent"] = self._get_ca_status(server.get_client())
        result.update(self._get_dhcp_status(server))
        return result

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Fetch live status and global options from Kea and expose them to the template."""
        try:
            statuses = self._get_statuses(instance)
        except Exception:
            logger.exception("Failed to fetch statuses for server %s", instance.pk)
            statuses = {}

        service_urls: dict[str, dict[str, str]] = {}
        if instance.dhcp4:
            service_urls["DHCPv4"] = {
                "enable_url": reverse("plugins:netbox_kea:server_dhcp4_enable", args=[instance.pk]),
                "disable_url": reverse("plugins:netbox_kea:server_dhcp4_disable", args=[instance.pk]),
                "options_url": reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[instance.pk]),
            }
        if instance.dhcp6:
            service_urls["DHCPv6"] = {
                "enable_url": reverse("plugins:netbox_kea:server_dhcp6_enable", args=[instance.pk]),
                "disable_url": reverse("plugins:netbox_kea:server_dhcp6_disable", args=[instance.pk]),
                "options_url": reverse("plugins:netbox_kea:server_dhcp6_options_edit", args=[instance.pk]),
            }

        # Merge status + URL context into a list so the template can iterate without
        # needing dynamic dict key lookups (which Django templates don't support).
        services = [
            {
                "name": name,
                "status_data": status_data,
                **service_urls.get(name, {}),
            }
            for name, status_data in statuses.items()
        ]

        return {
            "services": services,
            "global_options": _get_global_options(instance),
        }


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

        if resp[0]["result"] == 3:
            return [], None

        args = resp[0]["arguments"]
        if args is None:
            raise RuntimeError("Unexpected None arguments from lease-get-page")

        raw_leases = args["leases"]
        next = f"{raw_leases[-1]['ip-address']}" if args["count"] == per_page else None
        for i, lease in enumerate(raw_leases):
            lease_ip = IPAddress(lease["ip-address"])
            if lease_ip not in subnet:
                raw_leases = raw_leases[:i]
                next = None
                break

        subnet_leases = format_leases(raw_leases)

        return subnet_leases, next

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

        if resp[0]["result"] == 3:
            return []

        args = resp[0]["arguments"]
        if args is None:
            raise RuntimeError(f"Unexpected None arguments from lease{self.dhcp_version}-get{command}")
        if multiple is True:
            return format_leases(args["leases"])
        return format_leases([args])

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Return an empty table, the search form, and the add-lease URL for the initial (non-HTMX) page load."""
        # For non-htmx requests.

        table = self.get_table([], request)
        form = self.form(request.GET) if "q" in request.GET else self.form()
        return {
            "form": form,
            "table": table,
            "add_url": reverse(
                f"plugins:netbox_kea:server_lease{self.dhcp_version}_add",
                args=[instance.pk],
            ),
        }

    def get_export(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Stream all matching leases as a CSV download."""
        form = self.form(request.GET)
        if not form.is_valid():
            messages.warning(request, "Invalid form for export.")
            return redirect(request.path)

        instance = self.get_object(**kwargs)

        by = form.cleaned_data["by"]
        q = form.cleaned_data["q"]
        client = instance.get_client(version=self.dhcp_version)
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

        table = self.get_table(leases, request)
        return export_table(table, "leases.csv", use_selected_columns=request.GET["export"] == "table")

    def get_export_all(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Export every lease on the server (no search filter) as a CSV download.

        Paginates through ``lease{v}-get-page`` from the beginning until all
        leases have been fetched, then streams them as a CSV file.
        Requires the ``lease_cmds`` hook to be loaded on the Kea server.
        """
        instance = self.get_object(**kwargs)
        client = instance.get_client(version=self.dhcp_version)

        start_ip = "0.0.0.0" if self.dhcp_version == 4 else "::"
        per_page = 1000

        all_leases: list[dict[str, Any]] = []
        cursor = start_ip
        while True:
            resp = client.command(
                f"lease{self.dhcp_version}-get-page",
                service=[f"dhcp{self.dhcp_version}"],
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
            if args["count"] < per_page:
                break
            cursor = raw_leases[-1]["ip-address"]

        table = self.get_table(all_leases, request)
        return export_table(table, "leases_all.csv", use_selected_columns=False)

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Dispatch to export, HTMX partial, or full page render as appropriate."""
        logger = logging.getLogger("netbox_kea.views.BaseServerDHCPLeasesView")

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

            # Enrich leases with reservation badges + NetBox IPAM status.
            # Extracted helper so combined views get the same treatment.
            _enrich_leases_with_badges(leases, instance, self.dhcp_version)

            table = self.get_table(leases, request)

            can_delete = request.user.has_perm(
                "netbox_kea.bulk_delete_lease_from_server",
                obj=instance,
            )
            if not can_delete:
                table.columns.hide("pk")

            return render(
                request,
                "netbox_kea/server_dhcp_leases_htmx.html",
                {
                    "can_delete": can_delete,
                    "is_embedded": False,
                    "delete_action": reverse(
                        f"plugins:netbox_kea:server_leases{self.dhcp_version}_delete",
                        args=[instance.pk],
                    ),
                    "form": form,
                    "table": table,
                    "next_page": next_page,
                    "paginate": paginate,
                    "page_lengths": EnhancedPaginator.default_page_lengths,
                },
            )
        except Exception:
            import uuid

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

    tab = OptionalViewTab(label="DHCPv6 Leases", weight=1010, is_enabled=lambda s: s.dhcp6)
    form = forms.Leases6SearchForm
    table = tables.LeaseTable6
    dhcp_version = 6


@register_model_view(Server, "leases4")
class ServerLeases4View(BaseServerLeasesView[tables.LeaseTable4]):
    """DHCPv4 leases tab for a Kea Server."""

    tab = OptionalViewTab(label="DHCPv4 Leases", weight=1020, is_enabled=lambda s: s.dhcp4)
    form = forms.Leases4SearchForm
    table = tables.LeaseTable4
    dhcp_version = 4


class FakeLeaseModelMeta:
    """Minimal ``_meta`` shim so bulk_delete.html can introspect the lease pseudo-model."""

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

        if not request.user.has_perm("netbox_kea.bulk_delete_lease_from_server", obj=instance):
            return HttpResponseForbidden("This user does not have permission to delete DHCP leases.")

        form = self.form(request.POST)

        if not form.is_valid():
            messages.warning(request, str(form.errors))
            return redirect(self.get_return_url(request, obj=instance))

        lease_ips = form.cleaned_data["pk"]
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
                    "return_url": self.get_return_url(request, obj=instance),
                },
            )

        client = instance.get_client(version=self.dhcp_version)

        for ip in lease_ips:
            try:
                self.delete_lease(client, ip)
            except Exception:  # noqa: PERF203
                logger.exception("Error deleting lease %s", ip)
                messages.error(request, f"Error deleting lease {ip}: see server logs for details.")
                return redirect(self.get_return_url(request, obj=instance))

        messages.success(request, f"Deleted {len(lease_ips)} DHCPv{self.dhcp_version} lease(s).")
        return redirect(self.get_return_url(request, obj=instance))


class ServerLeases6DeleteView(BaseServerLeasesDeleteView):
    """Bulk-delete view for DHCPv6 leases."""

    form = forms.Lease6DeleteForm
    dhcp_version = 6


class ServerLeases4DeleteView(BaseServerLeasesDeleteView):
    """Bulk-delete view for DHCPv4 leases."""

    form = forms.Lease4DeleteForm
    dhcp_version = 4


class _BaseLeaseEditView(ConditionalLoginRequiredMixin, View):
    """Base view for editing a single lease via ``lease{v}-update``.

    Subclasses must set ``dhcp_version`` and ``form_class``.
    """

    dhcp_version: int
    form_class: type

    def _get_server(self, pk: int) -> Server:
        return get_object_or_404(Server, pk=pk)

    def _leases_url(self, server: Server) -> str:
        return reverse(
            f"plugins:netbox_kea:server_leases{self.dhcp_version}",
            kwargs={"pk": server.pk},
        )

    def get(self, request: HttpRequest, pk: int, ip_address: str) -> HttpResponse:
        """Render the edit form pre-filled with the current lease values."""
        server = self._get_server(pk)
        client = server.get_client(version=self.dhcp_version)
        try:
            resp = client.command(
                f"lease{self.dhcp_version}-get",
                service=[f"dhcp{self.dhcp_version}"],
                arguments={"ip-address": ip_address},
            )
        except KeaException:
            messages.error(request, "Failed to fetch lease: see server logs for details.")
            return redirect(self._leases_url(server))

        if resp[0]["result"] == 3:
            messages.warning(request, f"Lease {ip_address} not found.")
            return redirect(self._leases_url(server))

        lease = resp[0]["arguments"]
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
        client = server.get_client(version=self.dhcp_version)
        kwargs: dict[str, object] = {}
        if cd.get("hostname") is not None:
            kwargs["hostname"] = cd["hostname"] or None
        if cd.get("valid_lft") is not None:
            kwargs["valid_lft"] = cd["valid_lft"]
        if self.dhcp_version == 4 and cd.get("hw_address"):
            kwargs["hw_address"] = cd["hw_address"]
        elif self.dhcp_version == 6 and cd.get("duid"):
            kwargs["duid"] = cd["duid"]
        try:
            client.lease_update(self.dhcp_version, ip_address, **kwargs)
            messages.success(request, f"Lease {ip_address} updated.")
        except KeaException as exc:
            logger.exception("Error updating lease %s", ip_address)
            messages.error(request, kea_error_hint(exc))
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
            client = server.get_client(version=self.dhcp_version)
            try:
                client.lease_add(self.dhcp_version, lease)
                messages.success(request, f"Lease for {cd['ip_address']} created.")
                if cd.get("sync_to_netbox"):
                    try:
                        sync_lease_to_netbox(lease)
                        messages.success(request, f"IPAddress {cd['ip_address']} synced to NetBox.")
                    except Exception:
                        logger.exception("Failed to sync lease %s to NetBox", cd.get("ip_address"))
                        messages.warning(request, "Lease created but NetBox IPAM sync failed; see server logs.")
                return redirect(cancel_url)
            except KeaException as exc:
                logger.exception("Failed to create DHCPv%s lease for %s", self.dhcp_version, cd.get("ip_address"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
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


class BaseServerDHCPSubnetsView(generic.ObjectChildrenView):
    """Base view for the subnet list tab; fetches subnet data from Kea config."""

    table = tables.SubnetTable
    queryset = Server.objects.all()
    template_name = "netbox_kea/server_dhcp_subnets.html"

    def get_children(self, request: HttpRequest, parent: Server) -> list[dict[str, Any]]:
        """Return the subnet list for *parent* by delegating to :meth:`get_subnets`."""
        return self.get_subnets(parent)

    def get_subnets(self, server: Server) -> list[dict[str, Any]]:
        """Fetch all subnets (including shared-network subnets) from the Kea config.

        Also fetches per-subnet utilisation statistics from ``stat-lease{v}-get``
        when the ``stat_cmds`` hook is loaded.  Degrades gracefully when the hook
        is absent.
        """
        from .utilities import format_option_data, parse_subnet_stats

        client = server.get_client(version=self.dhcp_version)
        config = client.command("config-get", service=[f"dhcp{self.dhcp_version}"])
        if config[0]["arguments"] is None:
            raise RuntimeError(f"Unexpected None arguments from config-get for dhcp{self.dhcp_version}")
        dhcp_conf = config[0]["arguments"].get(f"Dhcp{self.dhcp_version}", {})
        subnets = dhcp_conf.get(f"subnet{self.dhcp_version}", [])
        subnet_list = [
            {
                "id": s["id"],
                "subnet": s["subnet"],
                "dhcp_version": self.dhcp_version,
                "server_pk": server.pk,
                "options": format_option_data(s.get("option-data", []), version=self.dhcp_version),
                "pools": [p.get("pool", "") for p in s.get("pools", []) if p.get("pool")],
            }
            for s in subnets
            if "id" in s and "subnet" in s
        ]

        for sn in dhcp_conf.get("shared-networks", []):
            subnet_list.extend(
                {
                    "id": s["id"],
                    "subnet": s["subnet"],
                    "shared_network": sn["name"],
                    "dhcp_version": self.dhcp_version,
                    "server_pk": server.pk,
                    "options": format_option_data(s.get("option-data", []), version=self.dhcp_version),
                    "pools": [p.get("pool", "") for p in s.get("pools", []) if p.get("pool")],
                }
                for s in sn.get(f"subnet{self.dhcp_version}", [])
            )

        # Enrich with utilisation stats when stat_cmds hook is available.
        try:
            stat_resp = client.command(
                f"stat-lease{self.dhcp_version}-get",
                service=[f"dhcp{self.dhcp_version}"],
            )
            stats = parse_subnet_stats(stat_resp, self.dhcp_version)
            for s in subnet_list:
                if s["id"] in stats:
                    s.update(stats[s["id"]])
        except Exception:
            pass  # stat_cmds not loaded — show subnets without utilisation column

        return subnet_list

    def get(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        """Handle GET: check DHCP enabled, then render table or export."""
        instance = self.get_object(**kwargs)
        if resp := check_dhcp_enabled(instance, self.dhcp_version):
            return resp

        # We can't use the original get() since it calls get_table_configs which requires a NetBox model.
        child_objects = self.get_children(request, instance)

        table_data = self.prep_table_data(request, child_objects, instance)
        table = self.get_table(table_data, request, False)

        if "export" in request.GET:
            return export_table(
                table,
                filename=f"kea-dhcpv{self.dhcp_version}-subnets.csv",
                use_selected_columns=request.GET["export"] == "table",
            )

        # If this is an HTMX request, return only the rendered table HTML
        if htmx_partial(request):
            return render(
                request,
                "htmx/table.html",
                {
                    "object": instance,
                    "table": table,
                    "model": self.child_model,
                },
            )

        return render(
            request,
            self.get_template_name(),
            {
                "object": instance,
                "base_template": f"{instance._meta.app_label}/{instance._meta.model_name}.html",
                "table": table,
                "table_config": f"{table.name}_config",
                "return_url": request.get_full_path(),
            },
        )


@register_model_view(Server, "subnets6")
class ServerDHCP6SubnetsView(BaseServerDHCPSubnetsView):
    """DHCPv6 subnets tab for a Kea Server."""

    tab = OptionalViewTab(label="DHCPv6 Subnets", weight=1030, is_enabled=lambda s: s.dhcp6)
    dhcp_version = 6


@register_model_view(Server, "subnets4")
class ServerDHCP4SubnetsView(BaseServerDHCPSubnetsView):
    """DHCPv4 subnets tab for a Kea Server."""

    tab = OptionalViewTab(label="DHCPv4 Subnets", weight=1040, is_enabled=lambda s: s.dhcp4)
    dhcp_version = 4


# ─────────────────────────────────────────────────────────────────────────────
# Shared Networks views
# ─────────────────────────────────────────────────────────────────────────────


class BaseServerSharedNetworksView(generic.ObjectChildrenView):
    """Read-only tab listing shared networks from the Kea config."""

    table = tables.SharedNetworkTable
    queryset = Server.objects.all()
    template_name = "netbox_kea/server_shared_networks.html"
    dhcp_version: int

    def get_children(self, request: HttpRequest, parent: Server) -> list[dict[str, Any]]:
        """Fetch shared-networks from config-get and return one dict per network."""
        if check_dhcp_enabled(parent, self.dhcp_version) is not None:
            return []
        client = parent.get_client(version=self.dhcp_version)
        config = client.command("config-get", service=[f"dhcp{self.dhcp_version}"])
        if config[0]["arguments"] is None:
            return []
        dhcp_conf = config[0]["arguments"].get(f"Dhcp{self.dhcp_version}", {})
        result = []
        for sn in dhcp_conf.get("shared-networks", []):
            subnets = sn.get(f"subnet{self.dhcp_version}", [])
            subnet_links = [
                {
                    "cidr": s["subnet"],
                    "url": (
                        reverse(
                            f"plugins:netbox_kea:server_leases{self.dhcp_version}",
                            args=[parent.pk],
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
                    "server_pk": parent.pk,
                    "dhcp_version": self.dhcp_version,
                }
            )
        return result

    def get(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        """Handle GET: check DHCP enabled, then render shared-network table."""
        instance = self.get_object(**kwargs)
        if resp := check_dhcp_enabled(instance, self.dhcp_version):
            return resp

        child_objects = self.get_children(request, instance)
        table_data = self.prep_table_data(request, child_objects, instance)
        table = self.get_table(table_data, request, False)

        return render(
            request,
            self.get_template_name(),
            {
                "object": instance,
                "base_template": f"{instance._meta.app_label}/{instance._meta.model_name}.html",
                "table": table,
                "table_config": f"{table.name}_config",
                "return_url": request.get_full_path(),
                "add_url": reverse(
                    f"plugins:netbox_kea:server_shared_network{self.dhcp_version}_add",
                    args=[instance.pk],
                ),
                "dhcp_version": self.dhcp_version,
            },
        )


@register_model_view(Server, "shared_networks6")
class ServerSharedNetworks6View(BaseServerSharedNetworksView):
    """DHCPv6 shared networks tab."""

    tab = OptionalViewTab(label="DHCPv6 Shared Networks", weight=1025, is_enabled=lambda s: s.dhcp6)
    dhcp_version = 6


@register_model_view(Server, "shared_networks4")
class ServerSharedNetworks4View(BaseServerSharedNetworksView):
    """DHCPv4 shared networks tab."""

    tab = OptionalViewTab(label="DHCPv4 Shared Networks", weight=1035, is_enabled=lambda s: s.dhcp4)
    dhcp_version = 4


class BaseServerSharedNetworkAddView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Add a new shared network to a Kea server.

    Subclasses set ``dhcp_version`` to 4 or 6.
    """

    dhcp_version: int

    def _success_url(self, server: Server) -> str:
        return reverse(f"plugins:netbox_kea:server_shared_networks{self.dhcp_version}", args=[server.pk])

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the add-network form."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = forms.SharedNetworkForm()
        return render(
            request,
            "netbox_kea/server_shared_network_add.html",
            {
                "object": server,
                "server": server,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._success_url(server),
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate and create the shared network."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = forms.SharedNetworkForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "netbox_kea/server_shared_network_add.html",
                {
                    "object": server,
                    "server": server,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "cancel_url": self._success_url(server),
                },
            )
        name = form.cleaned_data["name"]
        try:
            client = server.get_client(version=self.dhcp_version)
            client.network_add(version=self.dhcp_version, name=name)
            messages.success(request, f"Shared network '{name}' created.")
        except KeaException as exc:
            logger.warning("network%d-add failed for %s: %s", self.dhcp_version, server, exc)
            messages.error(request, f"Kea error: {kea_error_hint(exc)}")
        except Exception:
            logger.exception("Unexpected error adding shared network for %s", server)
            messages.error(request, "An internal error occurred.")
        return redirect(self._success_url(server))


class ServerSharedNetwork6AddView(BaseServerSharedNetworkAddView):
    """Add a new DHCPv6 shared network."""

    dhcp_version = 6


class ServerSharedNetwork4AddView(BaseServerSharedNetworkAddView):
    """Add a new DHCPv4 shared network."""

    dhcp_version = 4


class BaseServerSharedNetworkDeleteView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Delete a shared network from a Kea server.

    The network name is passed as a URL kwarg ``network_name``.  Subnets that
    belonged to the deleted network fall back to the global address pool.
    """

    dhcp_version: int

    def _success_url(self, server: Server) -> str:
        return reverse(f"plugins:netbox_kea:server_shared_networks{self.dhcp_version}", args=[server.pk])

    def get(self, request: HttpRequest, pk: int, network_name: str) -> HttpResponse:
        """Render the delete-confirmation page."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        return render(
            request,
            "netbox_kea/server_shared_network_delete.html",
            {
                "object": server,
                "server": server,
                "network_name": network_name,
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._success_url(server),
            },
        )

    def post(self, request: HttpRequest, pk: int, network_name: str) -> HttpResponse:
        """Delete the shared network."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        try:
            client = server.get_client(version=self.dhcp_version)
            client.network_del(version=self.dhcp_version, name=network_name)
            messages.success(request, f"Shared network '{network_name}' deleted.")
        except KeaException as exc:
            logger.warning("network%d-del failed for %s: %s", self.dhcp_version, server, exc)
            messages.error(request, f"Kea error: {kea_error_hint(exc)}")
        except Exception:
            logger.exception("Unexpected error deleting shared network for %s", server)
            messages.error(request, "An internal error occurred.")
        return redirect(self._success_url(server))


class ServerSharedNetwork6DeleteView(BaseServerSharedNetworkDeleteView):
    """Delete a DHCPv6 shared network."""

    dhcp_version = 6


class ServerSharedNetwork4DeleteView(BaseServerSharedNetworkDeleteView):
    """Delete a DHCPv4 shared network."""

    dhcp_version = 4


def _enrich_reservations_with_lease_status(client: "KeaClient", reservations: list[dict], version: int) -> None:
    """Enrich each reservation dict with ``has_active_lease`` (bool | None).

    Queries ``lease4-get-all`` / ``lease6-get-all`` per unique subnet to find
    active leases.  Sets ``r["has_active_lease"] = True/False`` for each
    reservation.  Leaves ``has_active_lease`` unset (None) if the ``lease_cmds``
    hook is unavailable or an unexpected error occurs, so the template can
    distinguish "unknown" from "no lease".

    Args:
        client: Connected KeaClient for the server.
        reservations: List of reservation dicts (mutated in-place).
        version: DHCP version (4 or 6).

    """
    if not reservations:
        return

    service = f"dhcp{version}"
    lease_cmd = f"lease{version}-get-all"
    unique_subnet_ids = {r.get("subnet-id") for r in reservations if r.get("subnet-id")}

    active_lease_ips: set[str] = set()
    hook_unavailable = False

    def _fetch_leases_for_subnet(sid: int) -> list[str] | None:
        """Return list of lease IPs, or None if the lease_cmds hook is not loaded."""
        try:
            resp = client.command(
                lease_cmd,
                service=[service],
                arguments={"subnets": [sid]},
                check=(0, 3),
            )
            if resp[0]["result"] != 3:
                args = resp[0].get("arguments", {})
                return [lease.get("ip-address", "") for lease in args.get("leases", [])]
            return []
        except KeaException as exc:
            if exc.response.get("result") == 2:
                return None  # hook not loaded
            return []
        except Exception:  # noqa: BLE001
            return []

    if not unique_subnet_ids:
        return

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(unique_subnet_ids), 10)) as executor:
            futures = {executor.submit(_fetch_leases_for_subnet, sid): sid for sid in unique_subnet_ids}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is None:
                    hook_unavailable = True
                else:
                    for ip in result:
                        active_lease_ips.add(ip)
    except Exception:  # noqa: BLE001
        return

    if hook_unavailable:
        return

    for r in reservations:
        ip = r.get("ip-address", r.get("ip_address", ""))
        r["has_active_lease"] = ip in active_lease_ips


def _filter_reservations(
    reservations: list[dict[str, Any]], q: str, subnet_id: int | None, version: int
) -> list[dict[str, Any]]:
    """Filter a list of reservation dicts by free-text query and/or subnet ID.

    Filtering is done in-memory (client-side) because ``reservation-get-page``
    does not support server-side search.

    Args:
        reservations: List of reservation dicts (Kea wire format + normalised keys).
        q: Free-text query; matched case-insensitively against IP, hostname, and
           ``hw-address`` (DHCPv4) or ``duid`` (DHCPv6).
        subnet_id: If non-None, only reservations in this subnet ID are returned.
        version: 4 or 6 — determines which identifier field to search.

    """
    result = reservations
    if subnet_id is not None:
        result = [r for r in result if r.get("subnet-id") == subnet_id or r.get("subnet_id") == subnet_id]
    if q:
        q_lower = q.lower()
        if version == 4:
            result = [
                r
                for r in result
                if q_lower in r.get("ip_address", r.get("ip-address", "")).lower()
                or q_lower in r.get("hostname", "").lower()
                or q_lower in r.get("hw-address", "").lower()
            ]
        else:
            result = [
                r
                for r in result
                if q_lower in r.get("ip_address", "").lower()
                or any(q_lower in ip.lower() for ip in r.get("ip-addresses", []))
                or q_lower in r.get("hostname", "").lower()
                or q_lower in r.get("duid", "").lower()
            ]
    return result


@register_model_view(Server, "reservations4")
class ServerReservations4View(generic.ObjectView):
    """DHCPv4 reservations tab — lists all reservations from host_cmds hook."""

    queryset = Server.objects.all()
    tab = OptionalViewTab(label="DHCPv4 Reservations", weight=1050, is_enabled=lambda s: s.dhcp4)
    template_name = "netbox_kea/server_reservations.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Fetch reservations from Kea, apply search filters, and build the table."""
        server: Server = instance
        client = server.get_client(version=4)
        hook_available = True
        reservations: list[dict] = []
        try:
            source_index, from_index, limit = 0, 0, 100
            while True:
                page, next_from, next_source = client.reservation_get_page(
                    "dhcp4", source_index=source_index, from_index=from_index, limit=limit
                )
                reservations.extend(page)
                if next_from == 0 and next_source == 0:
                    break
                from_index = next_from
                source_index = next_source
        except KeaException as exc:
            if exc.response.get("result") == 2:
                hook_available = False
        except Exception:
            pass  # Network/other error — keep hook_available=True, show empty table

        # Inject server_pk so the actions template column can build edit/delete URLs.
        for r in reservations:
            r["server_pk"] = server.pk
            r.setdefault("ip_address", r.get("ip-address", ""))
            r.setdefault("subnet_id", r.get("subnet-id", 0))

        # Apply search filter before enrichment to avoid unnecessary Kea API calls.
        search_form = forms.ReservationSearchForm(request.GET or None)
        if search_form.is_valid():
            reservations = _filter_reservations(
                reservations,
                q=search_form.cleaned_data.get("q", ""),
                subnet_id=search_form.cleaned_data.get("subnet_id"),
                version=4,
            )

        # Enrich reservations with lease status + NetBox IPAM badges.
        _enrich_reservations_with_badges(reservations, server, 4)

        table = tables.ReservationTable4(reservations, user=request.user)
        table.configure(request)
        return {
            "table": table,
            "dhcp_version": 4,
            "hook_available": hook_available,
            "search_form": search_form,
            "add_url": reverse("plugins:netbox_kea:server_reservation4_add", args=[server.pk]),
            "bulk_sync_url": reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[server.pk]),
            "import_url": reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[server.pk]),
        }


@register_model_view(Server, "reservations6")
class ServerReservations6View(generic.ObjectView):
    """DHCPv6 reservations tab — lists all reservations from host_cmds hook."""

    queryset = Server.objects.all()
    tab = OptionalViewTab(label="DHCPv6 Reservations", weight=1060, is_enabled=lambda s: s.dhcp6)
    template_name = "netbox_kea/server_reservations.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Fetch DHCPv6 reservations from Kea, apply search filters, and build the table."""
        server: Server = instance
        client = server.get_client(version=6)
        hook_available = True
        reservations: list[dict] = []
        try:
            source_index, from_index, limit = 0, 0, 100
            while True:
                page, next_from, next_source = client.reservation_get_page(
                    "dhcp6", source_index=source_index, from_index=from_index, limit=limit
                )
                reservations.extend(page)
                if next_from == 0 and next_source == 0:
                    break
                from_index = next_from
                source_index = next_source
        except KeaException as exc:
            if exc.response.get("result") == 2:
                hook_available = False
        except Exception:
            pass  # Network/other error — keep hook_available=True, show empty table

        for r in reservations:
            r["server_pk"] = server.pk
            r.setdefault("ip_address", (r.get("ip-addresses") or [""])[0])
            r.setdefault("subnet_id", r.get("subnet-id", 0))

        # Apply search filter before enrichment to avoid unnecessary Kea API calls.
        search_form = forms.ReservationSearchForm(request.GET or None)
        if search_form.is_valid():
            reservations = _filter_reservations(
                reservations,
                q=search_form.cleaned_data.get("q", ""),
                subnet_id=search_form.cleaned_data.get("subnet_id"),
                version=6,
            )

        # Enrich reservations with lease status + NetBox IPAM badges.
        _enrich_reservations_with_badges(reservations, server, 6)

        table = tables.ReservationTable6(reservations, user=request.user)
        table.configure(request)
        return {
            "table": table,
            "dhcp_version": 6,
            "hook_available": hook_available,
            "search_form": search_form,
            "add_url": reverse("plugins:netbox_kea:server_reservation6_add", args=[server.pk]),
            "bulk_sync_url": reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[server.pk]),
            "import_url": reverse("plugins:netbox_kea:server_reservation6_bulk_import", args=[server.pk]),
        }


class ServerReservation4AddView(_KeaChangeMixin, generic.ObjectView):
    """Add a DHCPv4 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_form.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render add form, optionally pre-filled from query parameters."""
        server = self.get_object(pk=pk)
        initial = {
            k: request.GET.get(k, "") for k in ("subnet_id", "ip_address", "identifier_type", "identifier", "hostname")
        }
        initial = {k: v for k, v in initial.items() if v}
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": forms.Reservation4Form(initial=initial),
                "dhcp_version": 4,
                "action": "Add",
                "return_url": reverse("plugins:netbox_kea:server_reservations4", args=[pk]),
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate form and create reservation in Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation4Form(data=request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        if form.is_valid():
            cd = form.cleaned_data
            reservation = {
                "subnet-id": cd["subnet_id"],
                "ip-address": cd["ip_address"],
                cd["identifier_type"]: cd["identifier"],
            }
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            client = server.get_client(version=4)
            try:
                client.reservation_add("dhcp4", reservation)
                messages.success(request, f"Reservation for {cd['ip_address']} created.")
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {cd['ip_address']} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv4 reservation %s to NetBox", cd.get("ip_address"))
                        messages.warning(request, "Reservation created, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except PartialPersistError as exc:
                messages.warning(request, str(exc))
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to create DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
                logger.exception("Failed to create DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, "Failed to create reservation: see server logs for details.")
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": 4,
                "action": "Add",
                "return_url": return_url,
            },
        )


class ServerReservation6AddView(_KeaChangeMixin, generic.ObjectView):
    """Add a DHCPv6 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_form.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render add form, optionally pre-filled from query parameters."""
        server = self.get_object(pk=pk)
        initial = {
            k: request.GET.get(k, "")
            for k in ("subnet_id", "ip_addresses", "identifier_type", "identifier", "hostname")
        }
        initial = {k: v for k, v in initial.items() if v}
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": forms.Reservation6Form(initial=initial),
                "dhcp_version": 6,
                "action": "Add",
                "return_url": reverse("plugins:netbox_kea:server_reservations6", args=[pk]),
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate form and create DHCPv6 reservation in Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation6Form(data=request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        if form.is_valid():
            cd = form.cleaned_data
            reservation: dict[str, Any] = {
                "subnet-id": cd["subnet_id"],
                "ip-addresses": [ip.strip() for ip in cd["ip_addresses"].split(",")],
                cd["identifier_type"]: cd["identifier"],
            }
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            client = server.get_client(version=6)
            try:
                client.reservation_add("dhcp6", reservation)
                messages.success(request, "DHCPv6 reservation created.")
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        primary_ip = (reservation.get("ip-addresses") or [""])[0]
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {primary_ip} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv6 reservation to NetBox")
                        messages.warning(request, "Reservation created, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except PartialPersistError as exc:
                messages.warning(request, str(exc))
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to create DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
                logger.exception("Failed to create DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, "Failed to create reservation: see server logs for details.")
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": 6,
                "action": "Add",
                "return_url": return_url,
            },
        )


class ServerReservation4EditView(_KeaChangeMixin, generic.ObjectView):
    """Edit an existing DHCPv4 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_form.html"

    def _get_reservation(self, server: Server, subnet_id: int, ip_address: str) -> dict | None:
        client = server.get_client(version=4)
        return client.reservation_get("dhcp4", subnet_id=subnet_id, ip_address=ip_address)

    def get(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Pre-populate form with existing reservation data."""
        server = self.get_object(pk=pk)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        try:
            reservation = self._get_reservation(server, subnet_id, ip_address)
        except KeaException as exc:
            logger.exception("Failed to fetch DHCPv4 reservation %s in subnet %s", ip_address, subnet_id)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to fetch DHCPv4 reservation %s in subnet %s", ip_address, subnet_id)
            messages.error(request, "Failed to retrieve reservation: see server logs for details.")
            return redirect(return_url)
        if reservation is None:
            raise Http404(f"Reservation {ip_address} not found in subnet {subnet_id}")
        identifier_type, identifier = _get_reservation_identifier(reservation, 4)
        initial = {
            "subnet_id": reservation.get("subnet-id", subnet_id),
            "ip_address": reservation.get("ip-address", ip_address),
            "identifier_type": identifier_type,
            "identifier": identifier,
            "hostname": reservation.get("hostname", ""),
        }
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": forms.Reservation4Form(initial=initial),
                "dhcp_version": 4,
                "action": "Edit",
                "return_url": return_url,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Validate and submit updated reservation to Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation4Form(data=request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        if form.is_valid():
            cd = form.cleaned_data
            reservation: dict[str, Any] = {
                "subnet-id": cd["subnet_id"],
                "ip-address": cd["ip_address"],
                cd["identifier_type"]: cd["identifier"],
            }
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            client = server.get_client(version=4)
            try:
                client.reservation_update("dhcp4", reservation)
                messages.success(request, f"Reservation for {cd['ip_address']} updated.")
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {cd['ip_address']} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv4 reservation %s to NetBox", cd.get("ip_address"))
                        messages.warning(request, "Reservation updated, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except PartialPersistError as exc:
                messages.warning(request, str(exc))
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to update DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
                logger.exception("Failed to update DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, "Failed to update reservation: see server logs for details.")
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": 4,
                "action": "Edit",
                "return_url": return_url,
            },
        )


class ServerReservation6EditView(_KeaChangeMixin, generic.ObjectView):
    """Edit an existing DHCPv6 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_form.html"

    def _get_reservation(self, server: Server, subnet_id: int, ip_address: str) -> dict | None:
        client = server.get_client(version=6)
        return client.reservation_get("dhcp6", subnet_id=subnet_id, ip_address=ip_address)

    def get(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Pre-populate form with existing DHCPv6 reservation data."""
        server = self.get_object(pk=pk)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        try:
            reservation = self._get_reservation(server, subnet_id, ip_address)
        except KeaException as exc:
            logger.exception("Failed to fetch DHCPv6 reservation %s in subnet %s", ip_address, subnet_id)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to fetch DHCPv6 reservation %s in subnet %s", ip_address, subnet_id)
            messages.error(request, "Failed to retrieve reservation: see server logs for details.")
            return redirect(return_url)
        if reservation is None:
            raise Http404(f"Reservation {ip_address} not found in subnet {subnet_id}")
        identifier_type, identifier = _get_reservation_identifier(reservation, 6)
        ip_list = reservation.get("ip-addresses", [ip_address])
        initial = {
            "subnet_id": reservation.get("subnet-id", subnet_id),
            "ip_addresses": ",".join(ip_list),
            "identifier_type": identifier_type,
            "identifier": identifier,
            "hostname": reservation.get("hostname", ""),
        }
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": forms.Reservation6Form(initial=initial),
                "dhcp_version": 6,
                "action": "Edit",
                "return_url": return_url,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Validate and submit updated DHCPv6 reservation to Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation6Form(data=request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        if form.is_valid():
            cd = form.cleaned_data
            reservation: dict[str, Any] = {
                "subnet-id": cd["subnet_id"],
                "ip-addresses": [ip.strip() for ip in cd["ip_addresses"].split(",")],
                cd["identifier_type"]: cd["identifier"],
            }
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            client = server.get_client(version=6)
            try:
                client.reservation_update("dhcp6", reservation)
                messages.success(request, "DHCPv6 reservation updated.")
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        primary_ip = (reservation.get("ip-addresses") or [""])[0]
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {primary_ip} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv6 reservation to NetBox")
                        messages.warning(request, "Reservation updated, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except PartialPersistError as exc:
                messages.warning(request, str(exc))
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to update DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
                logger.exception("Failed to update DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, "Failed to update reservation: see server logs for details.")
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": 6,
                "action": "Edit",
                "return_url": return_url,
            },
        )


class ServerReservation4DeleteView(_KeaChangeMixin, generic.ObjectView):
    """Delete confirmation for a DHCPv4 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_delete.html"

    def get(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Show deletion confirmation page."""
        server = self.get_object(pk=pk)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "ip_address": ip_address,
                "subnet_id": subnet_id,
                "dhcp_version": 4,
                "return_url": reverse("plugins:netbox_kea:server_reservations4", args=[pk]),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Issue reservation-del to Kea and redirect."""
        server = self.get_object(pk=pk)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        client = server.get_client(version=4)
        try:
            client.reservation_del("dhcp4", subnet_id=subnet_id, ip_address=ip_address)
            messages.success(request, f"Reservation for {ip_address} deleted.")
        except PartialPersistError as exc:
            messages.warning(request, str(exc))
        except KeaException as exc:
            logger.exception("Failed to delete DHCPv4 reservation for %s", ip_address)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to delete DHCPv4 reservation for %s", ip_address)
            messages.error(request, "Failed to delete reservation: see server logs for details.")
        return redirect(return_url)


class ServerReservation6DeleteView(_KeaChangeMixin, generic.ObjectView):
    """Delete confirmation for a DHCPv6 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_delete.html"

    def get(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Show deletion confirmation page."""
        server = self.get_object(pk=pk)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "ip_address": ip_address,
                "subnet_id": subnet_id,
                "dhcp_version": 6,
                "return_url": reverse("plugins:netbox_kea:server_reservations6", args=[pk]),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Issue reservation-del to Kea and redirect."""
        server = self.get_object(pk=pk)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        client = server.get_client(version=6)
        try:
            client.reservation_del("dhcp6", subnet_id=subnet_id, ip_address=ip_address)
            messages.success(request, f"DHCPv6 reservation for {ip_address} deleted.")
        except PartialPersistError as exc:
            messages.warning(request, str(exc))
        except KeaException as exc:
            logger.exception("Failed to delete DHCPv6 reservation for %s", ip_address)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to delete DHCPv6 reservation for %s", ip_address)
            messages.error(request, "Failed to delete reservation: see server logs for details.")
        return redirect(return_url)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 10: Pool management views
# ─────────────────────────────────────────────────────────────────────────────


class _BasePoolAddView(_KeaChangeMixin, generic.ObjectView):
    """Base view for adding a pool to a subnet."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_pool_add.html"
    dhcp_version: int  # set on subclasses

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": forms.PoolAddForm(),
                "subnet_id": subnet_id,
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        form = forms.PoolAddForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "subnet_id": subnet_id,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                },
            )
        pool = form.cleaned_data["pool"]
        client = server.get_client(version=self.dhcp_version)
        try:
            client.pool_add(version=self.dhcp_version, subnet_id=subnet_id, pool=pool)
            messages.success(request, f"Pool {pool} added to subnet {subnet_id}.")
        except PartialPersistError as exc:
            messages.warning(request, str(exc))
        except KeaException as exc:
            logger.exception("Failed to add pool to subnet %s", subnet_id)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to add pool to subnet %s", subnet_id)
            messages.error(request, "Failed to add pool: see server logs for details.")
        return redirect(return_url)


class ServerSubnet4PoolAddView(_BasePoolAddView):
    """Add a pool to a DHCPv4 subnet."""

    dhcp_version = 4


class ServerSubnet6PoolAddView(_BasePoolAddView):
    """Add a pool to a DHCPv6 subnet."""

    dhcp_version = 6


class _BasePoolDeleteView(_KeaChangeMixin, generic.ObjectView):
    """Base view for deleting a pool from a subnet."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_pool_delete.html"
    dhcp_version: int

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int, subnet_id: int, pool: str) -> HttpResponse:
        if not _POOL_RE.match(pool):
            return HttpResponse("Invalid pool format.", status=400)
        server = self.get_object(pk=pk)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "pool": pool,
                "subnet_id": subnet_id,
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, pool: str) -> HttpResponse:
        if not _POOL_RE.match(pool):
            return HttpResponse("Invalid pool format.", status=400)
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        client = server.get_client(version=self.dhcp_version)
        try:
            client.pool_del(version=self.dhcp_version, subnet_id=subnet_id, pool=pool)
            messages.success(request, f"Pool {pool} removed from subnet {subnet_id}.")
        except PartialPersistError as exc:
            messages.warning(request, str(exc))
        except KeaException as exc:
            logger.exception("Failed to remove pool from subnet %s", subnet_id)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to remove pool from subnet %s", subnet_id)
            messages.error(request, "Failed to remove pool: see server logs for details.")
        return redirect(return_url)


class ServerSubnet4PoolDeleteView(_BasePoolDeleteView):
    """Delete a pool from a DHCPv4 subnet."""

    dhcp_version = 4


class ServerSubnet6PoolDeleteView(_BasePoolDeleteView):
    """Delete a pool from a DHCPv6 subnet."""

    dhcp_version = 6


# ---------------------------------------------------------------------------
# Subnet add / delete views
# ---------------------------------------------------------------------------


class _BaseSubnetAddView(_KeaChangeMixin, generic.ObjectView):
    """Base view for adding a new subnet to Kea."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_subnet_add.html"
    dhcp_version: int

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": forms.SubnetAddForm(),
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        form = forms.SubnetAddForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                },
            )
        cd = form.cleaned_data
        client = server.get_client(version=self.dhcp_version)
        try:
            client.subnet_add(
                version=self.dhcp_version,
                subnet_cidr=cd["subnet"],
                subnet_id=cd.get("subnet_id") or None,
                pools=cd["pools"],
                gateway=cd["gateway"] or None,
                dns_servers=cd["dns_servers"],
                ntp_servers=cd["ntp_servers"],
            )
            messages.success(request, f"Subnet {cd['subnet']} added.")
        except PartialPersistError as exc:
            messages.warning(request, str(exc))
            return redirect(return_url)
        except KeaException as exc:
            logger.exception("Failed to add subnet %s", cd.get("subnet"))
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to add subnet %s", cd.get("subnet"))
            messages.error(request, "Failed to add subnet: see server logs for details.")
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                },
            )
        return redirect(return_url)


class ServerSubnet4AddView(_BaseSubnetAddView):
    """Add a DHCPv4 subnet."""

    dhcp_version = 4


class ServerSubnet6AddView(_BaseSubnetAddView):
    """Add a DHCPv6 subnet."""

    dhcp_version = 6


class _BaseSubnetEditView(_KeaChangeMixin, generic.ObjectView):
    """Base view for editing an existing subnet's configuration in Kea."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_subnet_edit.html"
    dhcp_version: int

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def _fetch_subnet(self, server: Server, subnet_id: int) -> dict[str, Any]:
        """Fetch current subnet config from Kea.  Returns empty dict on failure."""
        try:
            key = f"subnet{self.dhcp_version}"
            client = server.get_client(version=self.dhcp_version)
            resp = client.command(
                f"{key}-get",
                service=[f"dhcp{self.dhcp_version}"],
                arguments={"id": subnet_id},
            )
            subnets = resp[0].get("arguments", {}).get(key, [])
            return subnets[0] if subnets else {}
        except Exception:
            logger.warning("Failed to fetch subnet %s for editing", subnet_id)
            return {}

    def _get_network_data(self, client: "KeaClient", subnet_id: int) -> tuple[list[tuple[str, str]], str]:
        """Return ``(choices, current_network_name)`` for the shared-network dropdown.

        ``choices`` is suitable for a ``ChoiceField``: ``[("", "— global pool —"), ("net-a", "net-a"), ...]``.
        ``current_network_name`` is the name of the network the subnet currently belongs to, or ``""``.
        """
        try:
            resp = client.command("config-get", service=[f"dhcp{self.dhcp_version}"])
            dhcp_conf = resp[0].get("arguments", {}).get(f"Dhcp{self.dhcp_version}", {})
            networks = dhcp_conf.get("shared-networks", [])
        except Exception:
            logger.warning("Failed to fetch shared networks for subnet edit dropdown")
            return [("", "— (global pool) —")], ""

        current_network = ""
        choices: list[tuple[str, str]] = [("", "— (global pool) —")]
        for sn in networks:
            name = sn.get("name", "")
            if not name:
                continue
            choices.append((name, name))
            subnet_ids = {s.get("id") for s in sn.get(f"subnet{self.dhcp_version}", [])}
            if subnet_id in subnet_ids:
                current_network = name
        return choices, current_network

    def _form_initial(self, subnet: dict[str, Any]) -> dict[str, Any]:
        """Build SubnetEditForm initial values from a Kea subnet dict."""
        initial: dict[str, Any] = {"subnet_cidr": subnet.get("subnet", "")}

        # Pools
        pools = subnet.get("pools", [])
        if pools:
            initial["pools"] = "\n".join(p.get("pool", "") for p in pools if p.get("pool"))

        # Options
        for opt in subnet.get("option-data", []):
            name = opt.get("name", "")
            data = opt.get("data", "")
            if name == "routers":
                initial["gateway"] = data
            elif name in ("domain-name-servers", "dns-servers"):
                initial["dns_servers"] = data
            elif name in ("ntp-servers", "sntp-servers"):
                initial["ntp_servers"] = data

        # Lease lifetimes
        if subnet.get("valid-lft"):
            initial["valid_lft"] = subnet["valid-lft"]
        if subnet.get("min-valid-lft"):
            initial["min_valid_lft"] = subnet["min-valid-lft"]
        if subnet.get("max-valid-lft"):
            initial["max_valid_lft"] = subnet["max-valid-lft"]

        return initial

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        subnet = self._fetch_subnet(server, subnet_id)
        client = server.get_client(version=self.dhcp_version)
        network_choices, current_network = self._get_network_data(client, subnet_id)
        initial = self._form_initial(subnet)
        initial["shared_network"] = current_network
        initial["current_network"] = current_network
        form = forms.SubnetEditForm(initial=initial)
        form.fields["shared_network"].choices = network_choices
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "subnet_id": subnet_id,
                "subnet_cidr": subnet.get("subnet", ""),
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        client = server.get_client(version=self.dhcp_version)
        network_choices, _ = self._get_network_data(client, subnet_id)
        form = forms.SubnetEditForm(request.POST)
        form.fields["shared_network"].choices = network_choices
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "subnet_id": subnet_id,
                    "subnet_cidr": request.POST.get("subnet_cidr", ""),
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                },
            )
        cd = form.cleaned_data
        old_network = cd.get("current_network", "")
        new_network = cd.get("shared_network", "")

        # Handle shared-network membership change
        if old_network != new_network:
            try:
                if old_network:
                    client.network_subnet_del(version=self.dhcp_version, name=old_network, subnet_id=subnet_id)
                if new_network:
                    client.network_subnet_add(version=self.dhcp_version, name=new_network, subnet_id=subnet_id)
            except KeaException as exc:
                logger.warning("network_subnet change failed for subnet %s on server %s: %s", subnet_id, pk, exc)
                messages.error(request, f"Network assignment error: {kea_error_hint(exc)}")
                return redirect(return_url)
            except Exception:
                logger.exception("Unexpected error changing network for subnet %s on server %s", subnet_id, pk)
                messages.error(request, "An internal error occurred during network assignment.")
                return redirect(return_url)

        try:
            client.subnet_update(
                version=self.dhcp_version,
                subnet_id=subnet_id,
                subnet_cidr=cd["subnet_cidr"],
                pools=cd["pools"] if cd["pools"] != [] else [],
                gateway=cd["gateway"] or None,
                dns_servers=cd["dns_servers"] or None,
                ntp_servers=cd["ntp_servers"] or None,
                valid_lft=cd.get("valid_lft"),
                min_valid_lft=cd.get("min_valid_lft"),
                max_valid_lft=cd.get("max_valid_lft"),
            )
            messages.success(request, f"Subnet {cd['subnet_cidr']} updated.")
        except PartialPersistError as exc:
            messages.warning(request, str(exc))
            return redirect(return_url)
        except KeaException as exc:
            logger.exception("Failed to update subnet %s on server %s", subnet_id, pk)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to update subnet %s on server %s", subnet_id, pk)
            messages.error(request, "Failed to update subnet: see server logs for details.")
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "subnet_id": subnet_id,
                    "subnet_cidr": cd["subnet_cidr"],
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                },
            )
        return redirect(return_url)


class ServerSubnet4EditView(_BaseSubnetEditView):
    """Edit a DHCPv4 subnet's configuration."""

    dhcp_version = 4


class ServerSubnet6EditView(_BaseSubnetEditView):
    """Edit a DHCPv6 subnet's configuration."""

    dhcp_version = 6


class _BaseSubnetDeleteView(_KeaChangeMixin, generic.ObjectView):
    """Base view for deleting a subnet from Kea."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_subnet_delete.html"
    dhcp_version: int

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        client = server.get_client(version=self.dhcp_version)
        subnet_cidr = ""
        try:
            resp = client.command(
                f"subnet{self.dhcp_version}-get",
                service=[f"dhcp{self.dhcp_version}"],
                arguments={"id": subnet_id},
            )
            key = f"subnet{self.dhcp_version}"
            subnet_cidr = resp[0].get("arguments", {}).get(key, [{}])[0].get("subnet", "")
        except Exception:
            pass
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "subnet_id": subnet_id,
                "subnet_cidr": subnet_cidr,
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        client = server.get_client(version=self.dhcp_version)
        try:
            client.subnet_del(version=self.dhcp_version, subnet_id=subnet_id)
            messages.success(request, f"Subnet {subnet_id} deleted.")
        except PartialPersistError as exc:
            messages.warning(request, str(exc))
        except KeaException as exc:
            logger.exception("Failed to delete subnet %s", subnet_id)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to delete subnet %s", subnet_id)
            messages.error(request, "Failed to delete subnet: see server logs for details.")
        return redirect(return_url)


class ServerSubnet4DeleteView(_BaseSubnetDeleteView):
    """Delete a DHCPv4 subnet."""

    dhcp_version = 4


class ServerSubnet6DeleteView(_BaseSubnetDeleteView):
    """Delete a DHCPv6 subnet."""

    dhcp_version = 6


class _BaseSubnetWipeView(_KeaChangeMixin, generic.ObjectView):
    """Base view for wiping all leases in a subnet."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_subnet_wipe.html"
    dhcp_version: int

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        client = server.get_client(version=self.dhcp_version)
        subnet_cidr = ""
        try:
            resp = client.command(
                f"subnet{self.dhcp_version}-get",
                service=[f"dhcp{self.dhcp_version}"],
                arguments={"id": subnet_id},
            )
            key = f"subnet{self.dhcp_version}"
            subnet_cidr = resp[0].get("arguments", {}).get(key, [{}])[0].get("subnet", "")
        except Exception:
            pass
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "subnet_id": subnet_id,
                "subnet_cidr": subnet_cidr,
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        client = server.get_client(version=self.dhcp_version)
        try:
            client.lease_wipe(version=self.dhcp_version, subnet_id=subnet_id)
            messages.success(request, f"All leases in subnet {subnet_id} wiped.")
        except KeaException:
            logger.exception("Failed to wipe leases in subnet %s", subnet_id)
            messages.error(
                request,
                "Failed to wipe leases: see server logs for details. Ensure the lease_cmds hook is loaded.",
            )
        except Exception:
            logger.exception("Failed to wipe leases in subnet %s", subnet_id)
            messages.error(request, "Failed to wipe leases: see server logs for details.")
        return redirect(return_url)


class ServerSubnet4WipeView(_BaseSubnetWipeView):
    """Wipe all DHCPv4 leases in a subnet."""

    dhcp_version = 4


class ServerSubnet6WipeView(_BaseSubnetWipeView):
    """Wipe all DHCPv6 leases in a subnet."""

    dhcp_version = 6


class _BaseServerDHCPEnableView(_KeaChangeMixin, generic.ObjectView):
    """Confirmation view to re-enable a Kea DHCP service that was previously disabled."""

    queryset = Server.objects.all()
    dhcp_version: int
    template_name = "netbox_kea/server_dhcp_enable.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        return {"dhcp_version": self.dhcp_version}

    def post(self, request: HttpRequest, pk: int, **kwargs: Any) -> HttpResponse:
        instance = self.get_object(pk=pk)
        service = f"dhcp{self.dhcp_version}"
        try:
            client = instance.get_client(version=self.dhcp_version)
            client.dhcp_enable(service)
            messages.success(request, f"DHCPv{self.dhcp_version} service re-enabled on {instance}.")
        except KeaException as exc:
            messages.error(request, f"Failed to enable DHCPv{self.dhcp_version}: {kea_error_hint(exc)}")
        except Exception:
            logger.exception("Unexpected error enabling %s on server %s", service, pk)
            messages.error(request, "An internal error occurred.")
        return redirect(reverse("plugins:netbox_kea:server_status", args=[pk]))


class ServerDHCP4EnableView(_BaseServerDHCPEnableView):
    """Re-enable DHCPv4 processing."""

    dhcp_version = 4


class ServerDHCP6EnableView(_BaseServerDHCPEnableView):
    """Re-enable DHCPv6 processing."""

    dhcp_version = 6


class _BaseServerDHCPDisableView(_KeaChangeMixin, generic.ObjectView):
    """Confirmation form to temporarily disable a Kea DHCP service."""

    queryset = Server.objects.all()
    dhcp_version: int
    template_name = "netbox_kea/server_dhcp_disable.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        form = forms.DHCPDisableForm(request.POST or None)
        return {"dhcp_version": self.dhcp_version, "form": form}

    def post(self, request: HttpRequest, pk: int, **kwargs: Any) -> HttpResponse:
        instance = self.get_object(pk=pk)
        form = forms.DHCPDisableForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                self.get_extra_context(request, instance) | {"object": instance},
            )
        service = f"dhcp{self.dhcp_version}"
        max_period = form.cleaned_data.get("max_period")
        try:
            client = instance.get_client(version=self.dhcp_version)
            client.dhcp_disable(service, max_period=max_period)
            if max_period:
                messages.warning(
                    request,
                    f"DHCPv{self.dhcp_version} disabled on {instance} for up to {max_period}s.",
                )
            else:
                messages.warning(request, f"DHCPv{self.dhcp_version} disabled on {instance}.")
        except KeaException as exc:
            messages.error(request, f"Failed to disable DHCPv{self.dhcp_version}: {kea_error_hint(exc)}")
        except Exception:
            logger.exception("Unexpected error disabling %s on server %s", service, pk)
            messages.error(request, "An internal error occurred.")
        return redirect(reverse("plugins:netbox_kea:server_status", args=[pk]))


class ServerDHCP4DisableView(_BaseServerDHCPDisableView):
    """Disable DHCPv4 processing."""

    dhcp_version = 4


class ServerDHCP6DisableView(_BaseServerDHCPDisableView):
    """Disable DHCPv6 processing."""

    dhcp_version = 6


def _get_reservation_identifier(
    reservation: dict[str, Any],
    version: int,
) -> tuple[str, str]:
    """Extract the identifier type and value from a Kea reservation dict.

    Args:
        reservation: Kea reservation dict (from ``reservation-get``).
        version: DHCP version (4 or 6) to determine identifier priority order.

    Returns:
        ``(identifier_type, identifier_value)`` tuple.

    """
    v4_types = ["hw-address", "client-id", "circuit-id", "flex-id"]
    v6_types = ["duid", "hw-address", "client-id", "flex-id"]
    priority = v6_types if version == 6 else v4_types
    for itype in priority:
        if itype in reservation:
            return itype, reservation[itype]
    return "hw-address", ""


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: Global multi-server views
# ─────────────────────────────────────────────────────────────────────────────


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
) -> tuple[dict[str, dict], bool]:
    """Fetch reservations only for the IPs present in *leases* (targeted lookup).

    Uses individual ``reservation-get`` calls (one per lease) in parallel so
    only the IPs we actually care about are queried — avoiding a full
    reservation-page scan on servers with large reservation databases.

    Returns ``(reservation_by_ip, host_cmds_available)``.
    """
    service = f"dhcp{version}"
    reservation_by_ip: dict[str, dict] = {}
    host_cmds_available = True

    def _fetch_one(lease: dict) -> tuple[str, dict | None, bool]:
        ip = lease.get("ip_address", "")
        subnet_id = lease.get("subnet_id")
        if not ip or not subnet_id:
            return ip, None, True
        try:
            r = client.reservation_get(service, subnet_id=int(subnet_id), ip_address=ip)
            return ip, r, True
        except KeaException as exc:
            if exc.response.get("result") == 2:
                return ip, None, False  # hook not available
            return ip, None, True
        except Exception as exc:  # noqa: BLE001
            logger.debug("reservation-get failed for %s: %s", ip, exc)
            return ip, None, True

    if not leases:
        return reservation_by_ip, host_cmds_available

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(leases), 10)) as executor:
        futures = [executor.submit(_fetch_one, lease) for lease in leases]
        for future in concurrent.futures.as_completed(futures):
            ip, rsv, hook_ok = future.result()
            if not hook_ok:
                host_cmds_available = False
            if rsv is not None:
                reservation_by_ip[ip] = rsv

    return reservation_by_ip, host_cmds_available


def _enrich_leases_with_badges(leases: list[dict[str, Any]], server: "Server", version: int) -> None:
    """In-place: add reservation and NetBox IPAM badge fields to lease dicts.

    Adds:
    - ``reservation_url``: edit-reservation link if a reservation exists for this IP
    - ``create_reservation_url``: pre-filled add link if host_cmds is loaded
    - ``netbox_ip_url``: absolute URL if IP exists in NetBox IPAM
    - ``sync_url``: POST endpoint URL to create a NetBox IP when absent
    """
    from .sync import bulk_fetch_netbox_ips

    client = server.get_client(version=version)
    reservation_url_name = f"plugins:netbox_kea:server_reservation{version}_edit"
    add_url_name = f"plugins:netbox_kea:server_reservation{version}_add"

    reservation_by_ip: dict[str, dict] = {}
    host_cmds_available = True
    try:
        reservation_by_ip, host_cmds_available = _fetch_reservation_by_ip_for_leases(client, version, leases)
    except KeaException as exc:
        if exc.response.get("result") == 2:
            host_cmds_available = False
        else:
            logger.warning("reservation lookup failed during lease enrichment: %s", exc)
            host_cmds_available = False
    except Exception as exc:  # noqa: BLE001 — unexpected error (e.g. mock misconfiguration)
        logger.warning("unexpected error during lease enrichment: %s", exc)
        host_cmds_available = False

    for lease in leases:
        ip = lease.get("ip_address", "")
        rsv = reservation_by_ip.get(ip)
        rsv_subnet_id = rsv.get("subnet-id") if isinstance(rsv, dict) else None
        if rsv and rsv_subnet_id is not None:
            lease["reservation_url"] = reverse(
                reservation_url_name,
                args=[server.pk, rsv_subnet_id, ip],
            )
            lease["create_reservation_url"] = None
        elif host_cmds_available:
            lease["reservation_url"] = None
            base_add = reverse(add_url_name, args=[server.pk])
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

    sync_url = reverse(f"plugins:netbox_kea:server_lease{version}_sync", args=[server.pk])
    edit_url_name = f"plugins:netbox_kea:server_lease{version}_edit"
    nb_ips = bulk_fetch_netbox_ips([lease.get("ip_address", "") for lease in leases if lease.get("ip_address")])
    for lease in leases:
        ip = lease.get("ip_address", "")
        nb_ip = nb_ips.get(ip)
        if nb_ip:
            lease["netbox_ip_url"] = nb_ip.get_absolute_url()
        else:
            lease["sync_url"] = sync_url
        if ip:
            lease["edit_url"] = reverse(edit_url_name, args=[server.pk, ip])


def _enrich_reservations_with_badges(reservations: list[dict[str, Any]], server: "Server", version: int) -> None:
    """In-place: add active-lease status and NetBox IPAM badge fields to reservation dicts.

    Adds:
    - ``has_active_lease``: True/False (None if lease_cmds unavailable)
    - ``netbox_ip_url``: absolute URL if IP exists in NetBox IPAM
    - ``sync_url``: POST endpoint URL to create a NetBox IP when absent
    """
    from .sync import bulk_fetch_netbox_ips

    client = server.get_client(version=version)
    _enrich_reservations_with_lease_status(client, reservations, version=version)

    sync_url = reverse(f"plugins:netbox_kea:server_reservation{version}_sync", args=[server.pk])
    nb_ips = bulk_fetch_netbox_ips([r.get("ip_address", "") for r in reservations if r.get("ip_address")])
    for r in reservations:
        nb_ip = nb_ips.get(r.get("ip_address", ""))
        if nb_ip:
            r["netbox_ip_url"] = nb_ip.get_absolute_url()
        else:
            r["sync_url"] = sync_url


# ---------------------------------------------------------------------------
# Combined multi-server views  (/plugins/kea/combined/...)
# ---------------------------------------------------------------------------


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
    from .utilities import format_option_data

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
        from .utilities import parse_subnet_stats

        stats = parse_subnet_stats(stat_resp, version)
        for s in result:
            if s["id"] in stats:
                s.update(stats[s["id"]])
    except Exception:  # noqa: BLE001
        pass  # stat_cmds not loaded — show subnets without utilisation column
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
            for future, server in future_to_server.items():
                try:
                    all_subnets.extend(future.result())
                except Exception:  # noqa: BLE001, PERF203
                    logger.exception("Failed to query server %s", server.name)
                    errors.append((server.name, "Failed to query server"))

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
            for future, server in future_to_server.items():
                try:
                    all_records.extend(future.result())
                except Exception:  # noqa: BLE001, PERF203
                    logger.exception("Failed to query server %s", server.name)
                    errors.append((server.name, "Failed to query server"))

        # Enrich in the main thread so Django ORM queries see the test transaction.
        server_map = {s.pk: s for s in servers}
        for server_pk, server in server_map.items():
            server_records = [r for r in all_records if r.get("server_pk") == server_pk]
            if server_records:
                _enrich_reservations_with_badges(server_records, server, self.dhcp_version)

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
        search_form_cls = forms.CombinedLeases4SearchForm if self.dhcp_version == 4 else forms.CombinedLeases6SearchForm
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
                for future, server in future_to_server.items():
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
                for future, server in future_to_server.items():
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
                _enrich_leases_with_badges(server_leases, server, self.dhcp_version)

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


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: NetBox IPAM Sync Views
# ─────────────────────────────────────────────────────────────────────────────


class _BaseSyncView(ConditionalLoginRequiredMixin, View):
    """POST-only HTMX endpoint that syncs a Kea lease/reservation to a NetBox IPAddress.

    Returns a small HTML badge fragment.
    Subclasses set ``_status`` to ``"active"`` (leases) or ``"reserved"``
    (reservations) and call the appropriate sync helper.
    """

    _status: str = "active"

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not (request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress")):
            return HttpResponseForbidden("You do not have permission to sync to NetBox IPAM.")

        get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)

        ip_str = request.POST.get("ip_address", "").strip()
        if not ip_str:
            return HttpResponse("ip_address is required", status=400)

        try:
            IPAddress(ip_str)
        except Exception:
            return HttpResponse("Invalid IP address", status=400)

        hostname = request.POST.get("hostname", "").strip()
        try:
            nb_ip, _created = self._sync({"ip-address": ip_str, "hostname": hostname})
        except Exception:  # noqa: BLE001
            logger.exception("Sync error for ip=%s", ip_str)
            return HttpResponse("Sync error: see server logs for details.", status=500)

        return render(
            request,
            "netbox_kea/inc/sync_badge.html",
            {"nb_ip": nb_ip},
        )

    def _sync(self, data: dict):
        raise NotImplementedError


class ServerLease4SyncView(_BaseSyncView):
    """Sync a single DHCPv4 lease to a NetBox IPAddress (status=active)."""

    def _sync(self, data: dict):
        from .sync import sync_lease_to_netbox

        return sync_lease_to_netbox(data)


class ServerLease6SyncView(_BaseSyncView):
    """Sync a single DHCPv6 lease to a NetBox IPAddress (status=active)."""

    def _sync(self, data: dict):
        from .sync import sync_lease_to_netbox

        return sync_lease_to_netbox(data)


class ServerReservation4SyncView(_BaseSyncView):
    """Sync a DHCPv4 reservation to a NetBox IPAddress (status=reserved)."""

    def _sync(self, data: dict):
        from .sync import sync_reservation_to_netbox

        return sync_reservation_to_netbox(data)


class ServerReservation6SyncView(_BaseSyncView):
    """Sync a DHCPv6 reservation to a NetBox IPAddress (status=reserved)."""

    def _sync(self, data: dict):
        from .sync import sync_reservation_to_netbox

        return sync_reservation_to_netbox(data)


class _BaseBulkReservationSyncView(ConditionalLoginRequiredMixin, View):
    """Fetch all reservations for a server and sync them to NetBox IPAM."""

    dhcp_version: int = 4  # overridden in subclasses

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not (request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress")):
            return HttpResponseForbidden("You do not have permission to sync to NetBox IPAM.")

        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        from .sync import sync_reservation_to_netbox

        try:
            reservations = _fetch_reservations_from_server(server, self.dhcp_version)
        except Exception:
            logger.exception("Failed to fetch reservations from %s (DHCPv%s)", server.name, self.dhcp_version)
            messages.error(request, "Failed to fetch reservations: see server logs for details.")
            return HttpResponseRedirect(
                reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
            )

        created = updated = errors = 0
        for res in reservations:
            if not res.get("ip-address") and not res.get("ip-addresses"):
                continue
            try:
                nb_ip, was_created = sync_reservation_to_netbox(res)
                if was_created:
                    created += 1
                elif nb_ip:
                    updated += 1
            except Exception:
                ip_log = res.get("ip-address") or ", ".join(res.get("ip-addresses") or []) or "unknown"
                logger.exception("Failed to sync reservation %s", ip_log)
                errors += 1

        if errors:
            messages.warning(
                request,
                f"Bulk sync: {created} created, {updated} updated, {errors} errors.",
            )
        else:
            messages.success(
                request,
                f"Bulk sync complete: {created} created, {updated} updated.",
            )
        redirect_url = reverse(
            f"plugins:netbox_kea:server_reservations{self.dhcp_version}",
            args=[pk],
        )
        return HttpResponseRedirect(redirect_url)


class ServerReservation4BulkSyncView(_BaseBulkReservationSyncView):
    """Bulk sync all DHCPv4 reservations to NetBox IPAM."""

    dhcp_version = 4


class ServerReservation6BulkSyncView(_BaseBulkReservationSyncView):
    """Bulk sync all DHCPv6 reservations to NetBox IPAM."""

    dhcp_version = 6


# ─────────────────────────────────────────────────────────────────────────────
# Bulk Reservation Import (CSV → Kea)
# ─────────────────────────────────────────────────────────────────────────────


class _BaseBulkReservationImportView(ConditionalLoginRequiredMixin, View):
    """Upload a CSV file and batch-insert reservations into Kea.

    Subclasses set :attr:`dhcp_version` and :attr:`form_class`.

    **GET**: render the upload form.
    **POST**: parse CSV → loop :meth:`KeaClient.reservation_add` → show summary.

    Result codes:
    - ``created``: reservation successfully added.
    - ``skipped``: Kea returned result=1 with "already exists" text (idempotent).
    - ``errors``: any other :class:`~netbox_kea.kea.KeaException` or unexpected failure.
    """

    dhcp_version: int
    form_class: type

    template_name = "netbox_kea/server_reservation_bulk_import.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the CSV upload form."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = self.form_class()
        return_url = reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
        return render(
            request,
            self.template_name,
            {
                "object": instance,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "return_url": return_url,
                "result": None,
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Parse uploaded CSV and insert reservations into Kea."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        return_url = reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
        form = self.form_class(request.POST, request.FILES)
        result = None

        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )

        csv_file = request.FILES["csv_file"]
        content = csv_file.read().decode("utf-8-sig")  # utf-8-sig strips BOM automatically

        try:
            rows = parse_reservation_csv(content, self.dhcp_version)
        except ValueError as exc:
            form.add_error("csv_file", str(exc))
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )

        client = instance.get_client(version=self.dhcp_version)
        created = 0
        skipped = 0
        error_rows: list[dict[str, Any]] = []

        for row in rows:
            try:
                client.reservation_add(self.dhcp_version, row)
                created += 1
            except KeaException as exc:  # noqa: PERF203
                text = getattr(exc, "response", {}).get("text", "") or ""
                result_code = getattr(exc, "response", {}).get("result", -1)
                if result_code == 1 and ("already exist" in text.lower() or "duplicate" in text.lower()):
                    skipped += 1
                else:
                    error_rows.append({"row": row, "error": kea_error_hint(exc)})
            except Exception:
                logger.exception("Unexpected error importing reservation row %s", row)
                error_rows.append({"row": row, "error": "An unexpected error occurred."})

        result = {
            "created": created,
            "skipped": skipped,
            "errors": len(error_rows),
            "error_rows": error_rows,
            "total": created + skipped + len(error_rows),
        }
        return render(
            request,
            self.template_name,
            {
                "object": instance,
                "form": self.form_class(),
                "dhcp_version": self.dhcp_version,
                "return_url": return_url,
                "result": result,
            },
        )


class ServerReservation4BulkImportView(_BaseBulkReservationImportView):
    """Bulk import DHCPv4 reservations from a CSV file."""

    dhcp_version = 4
    form_class = forms.Reservation4BulkImportForm


class ServerReservation6BulkImportView(_BaseBulkReservationImportView):
    """Bulk import DHCPv6 reservations from a CSV file."""

    dhcp_version = 6
    form_class = forms.Reservation6BulkImportForm


# ─────────────────────────────────────────────────────────────────────────────
# Subnet Option Data Management
# ─────────────────────────────────────────────────────────────────────────────


class _BaseSubnetOptionsEditView(ConditionalLoginRequiredMixin, View):
    """GET/POST view for editing option-data of a single subnet.

    Loads the current options from Kea via ``config-get``, renders a formset,
    and on POST validates + saves via ``subnet_update_options`` (config-get →
    config-test → config-write).
    """

    dhcp_version: int = 4

    def _get_subnet_from_config(self, client, subnet_id: int) -> dict | None:
        """Fetch config and return the subnet dict, or None if not found."""
        service = f"dhcp{self.dhcp_version}"
        dhcp_key = f"Dhcp{self.dhcp_version}"
        subnet_key = f"subnet{self.dhcp_version}"
        resp = client.command("config-get", service=[service])
        config = resp[0]["arguments"]
        for s in config.get(dhcp_key, {}).get(subnet_key, []):
            if s.get("id") == subnet_id:
                return s
        for sn in config.get(dhcp_key, {}).get("shared-networks", []):
            for s in sn.get(subnet_key, []):
                if s.get("id") == subnet_id:
                    return s
        return None

    def get(self, request, pk: int, subnet_id: int):
        server = get_object_or_404(
            Server.objects.restrict(request.user, "view"),
            pk=pk,
        )
        client = server.get_client(version=self.dhcp_version)
        subnet = self._get_subnet_from_config(client, subnet_id)
        initial = []
        if subnet:
            initial = [
                {
                    "name": opt.get("name", ""),
                    "data": opt.get("data", ""),
                    "always_send": opt.get("always-send", False),
                }
                for opt in subnet.get("option-data", [])
            ]
        formset = forms.SubnetOptionsFormSet(initial=initial)
        return render(
            request,
            "netbox_kea/server_subnet_options_edit.html",
            {
                "object": server,
                "server": server,
                "subnet_id": subnet_id,
                "subnet_cidr": subnet.get("subnet", "") if subnet else "",
                "dhcp_version": self.dhcp_version,
                "formset": formset,
                "return_url": reverse(
                    f"plugins:netbox_kea:server_subnets{self.dhcp_version}",
                    args=[pk],
                ),
            },
        )

    def post(self, request, pk: int, subnet_id: int):
        server = get_object_or_404(
            Server.objects.restrict(request.user, "change"),
            pk=pk,
        )
        return_url = reverse(
            f"plugins:netbox_kea:server_subnets{self.dhcp_version}",
            args=[pk],
        )
        formset = forms.SubnetOptionsFormSet(request.POST)
        if not formset.is_valid():
            subnet_cidr = ""
            client = server.get_client(version=self.dhcp_version)
            subnet = self._get_subnet_from_config(client, subnet_id)
            if subnet:
                subnet_cidr = subnet.get("subnet", "")
            return render(
                request,
                "netbox_kea/server_subnet_options_edit.html",
                {
                    "object": server,
                    "server": server,
                    "subnet_id": subnet_id,
                    "subnet_cidr": subnet_cidr,
                    "dhcp_version": self.dhcp_version,
                    "formset": formset,
                    "return_url": return_url,
                },
            )

        options = []
        for f in formset.forms:
            if not f.cleaned_data or f.cleaned_data.get("DELETE"):
                continue
            opt: dict = {"name": f.cleaned_data["name"], "data": f.cleaned_data["data"]}
            if f.cleaned_data.get("always_send"):
                opt["always-send"] = True
            options.append(opt)

        client = server.get_client(version=self.dhcp_version)
        try:
            client.subnet_update_options(
                version=self.dhcp_version,
                subnet_id=subnet_id,
                options=options,
            )
            messages.success(request, f"Subnet {subnet_id} options updated.")
        except KeaException as exc:
            logger.exception("Failed to update options for subnet %s: %s", subnet_id, exc)
            messages.error(request, kea_error_hint(exc))
        return redirect(return_url)


class ServerSubnet4OptionsEditView(_BaseSubnetOptionsEditView):
    """Edit option-data for a DHCPv4 subnet."""

    dhcp_version = 4


class ServerSubnet6OptionsEditView(_BaseSubnetOptionsEditView):
    """Edit option-data for a DHCPv6 subnet."""

    dhcp_version = 6


# ─────────────────────────────────────────────────────────────────────────────
# Server-Level DHCP Options Management
# ─────────────────────────────────────────────────────────────────────────────


class _BaseServerOptionsEditView(ConditionalLoginRequiredMixin, View):
    """GET/POST view for editing server-level (global) option-data.

    Loads current server options from ``config-get``, renders a formset, and on
    POST validates + saves via ``server_update_options`` (config-get → config-test
    → config-write).
    """

    dhcp_version: int = 4

    def _get_options_from_config(self, client) -> list[dict]:
        """Fetch config and return the server-level option-data list."""
        service = f"dhcp{self.dhcp_version}"
        dhcp_key = f"Dhcp{self.dhcp_version}"
        resp = client.command("config-get", service=[service])
        config = resp[0]["arguments"]
        return config.get(dhcp_key, {}).get("option-data", [])

    def get(self, request, pk: int):
        server = get_object_or_404(
            Server.objects.restrict(request.user, "view"),
            pk=pk,
        )
        client = server.get_client(version=self.dhcp_version)
        existing = self._get_options_from_config(client)
        initial = [
            {
                "name": opt.get("name", ""),
                "data": opt.get("data", ""),
                "always_send": opt.get("always-send", False),
            }
            for opt in existing
        ]
        formset = forms.SubnetOptionsFormSet(initial=initial)
        return render(
            request,
            "netbox_kea/server_dhcp_options_edit.html",
            {
                "object": server,
                "server": server,
                "dhcp_version": self.dhcp_version,
                "formset": formset,
                "return_url": reverse("plugins:netbox_kea:server", args=[pk]),
            },
        )

    def post(self, request, pk: int):
        server = get_object_or_404(
            Server.objects.restrict(request.user, "change"),
            pk=pk,
        )
        return_url = reverse("plugins:netbox_kea:server", args=[pk])
        formset = forms.SubnetOptionsFormSet(request.POST)
        if not formset.is_valid():
            return render(
                request,
                "netbox_kea/server_dhcp_options_edit.html",
                {
                    "object": server,
                    "server": server,
                    "dhcp_version": self.dhcp_version,
                    "formset": formset,
                    "return_url": return_url,
                },
            )

        options = []
        for f in formset.forms:
            if not f.cleaned_data or f.cleaned_data.get("DELETE"):
                continue
            opt: dict = {"name": f.cleaned_data["name"], "data": f.cleaned_data["data"]}
            if f.cleaned_data.get("always_send"):
                opt["always-send"] = True
            options.append(opt)

        client = server.get_client(version=self.dhcp_version)
        try:
            client.server_update_options(version=self.dhcp_version, options=options)
            messages.success(request, f"DHCPv{self.dhcp_version} server options updated.")
        except KeaException as exc:
            logger.exception("Failed to update server options for %s: %s", server, exc)
            messages.error(request, kea_error_hint(exc))
        return redirect(return_url)


class ServerDHCP4OptionsEditView(_BaseServerOptionsEditView):
    """Edit server-level option-data for DHCPv4."""

    dhcp_version = 4


class ServerDHCP6OptionsEditView(_BaseServerOptionsEditView):
    """Edit server-level option-data for DHCPv6."""

    dhcp_version = 6


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3c: IPAddress → Kea Reservation panel
# ─────────────────────────────────────────────────────────────────────────────


class IPAddressKeaReservationsView(ConditionalLoginRequiredMixin, View):
    """Show Kea servers with pre-filled 'Create reservation' links for a NetBox IPAddress.

    Accessible at /plugins/kea/ip-addresses/<id>/kea-reservations/.
    Rendered as a standalone page and also embedded via the IPAddress template extension.
    """

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:  # noqa: D102
        from ipam.models import IPAddress as NbIP

        nb_ip = get_object_or_404(NbIP.objects.restrict(request.user, "view"), pk=pk)
        ip_str = str(nb_ip.address.ip)
        is_v6 = ":" in ip_str
        version = 6 if is_v6 else 4

        if version == 4:
            servers = Server.objects.restrict(request.user, "view").filter(dhcp4=True)
            add_url_name = "plugins:netbox_kea:server_reservation4_add"
        else:
            servers = Server.objects.restrict(request.user, "view").filter(dhcp6=True)
            add_url_name = "plugins:netbox_kea:server_reservation6_add"

        server_links = []
        for server in servers:
            base_url = reverse(add_url_name, args=[server.pk])
            ip_param = "ip_addresses" if version == 6 else "ip_address"
            params = _urlencode(
                {
                    ip_param: ip_str,
                    "hostname": nb_ip.dns_name or "",
                }
            )
            server_links.append(
                {
                    "server": server,
                    "url": f"{base_url}?{params}",
                }
            )

        return render(
            request,
            "netbox_kea/ip_kea_reservations.html",
            {
                "object": nb_ip,
                "nb_ip": nb_ip,
                "server_links": server_links,
                "version": version,
            },
        )


class CombinedServerStatusBadgeView(ConditionalLoginRequiredMixin, View):
    """HTMX endpoint: lightweight status fragment for one server.

    Returns a small HTML snippet containing one badge per enabled DHCP
    protocol (v4/v6).  Each badge shows "Online" or "Offline" based on
    whether ``version-get`` can reach the Kea daemon.

    Intended to be called with ``hx-get`` / ``hx-trigger="load"`` from the
    combined dashboard so the main page stays fast.
    """

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Return status badge fragment for server *pk*."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)

        statuses: list[dict] = []
        for version, enabled in ((4, server.dhcp4), (6, server.dhcp6)):
            if not enabled:
                continue
            try:
                client = server.get_client(version=version)
                client.command("version-get", service=[f"dhcp{version}"])
                online = True
            except Exception:
                online = False
            statuses.append({"version": version, "online": online})

        return render(
            request,
            "netbox_kea/server_status_badge.html",
            {"server": server, "statuses": statuses},
        )
