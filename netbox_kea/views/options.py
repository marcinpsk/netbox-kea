import logging
from urllib.parse import urlencode as _urlencode

import requests
from django.contrib import messages
from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from utilities.views import register_model_view

from .. import forms
from ..kea import KeaException, PartialPersistError
from ..models import Server
from ..utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    kea_error_hint,
)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin

logger = logging.getLogger(__name__)


class _BaseSubnetOptionsEditView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """GET/POST view for editing option-data of a single subnet.

    Loads the current options from Kea via ``config-get``, renders a formset,
    and on POST validates + saves via ``subnet_update_options`` (config-get →
    config-test → config-write).
    """

    dhcp_version: int = 4

    def _get_subnet_from_config(self, client, subnet_id: int) -> dict | None:
        """Fetch config and return the subnet dict, or None if not found or on error."""
        service = f"dhcp{self.dhcp_version}"
        dhcp_key = f"Dhcp{self.dhcp_version}"
        subnet_key = f"subnet{self.dhcp_version}"
        try:
            resp = client.command("config-get", service=[service])
        except (KeaException, requests.RequestException):
            logger.exception("Failed to fetch config-get for subnet options (subnet %s)", subnet_id)
            return None
        config = resp[0].get("arguments") or {}
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
            Server.objects.restrict(request.user, "change"),
            pk=pk,
        )
        return_url = reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])
        client = server.get_client(version=self.dhcp_version)
        subnet = self._get_subnet_from_config(client, subnet_id)
        if subnet is None:
            messages.error(request, "Could not load subnet configuration from Kea. The form cannot be displayed.")
            return redirect(return_url)
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
                "subnet_cidr": subnet.get("subnet", ""),
                "dhcp_version": self.dhcp_version,
                "formset": formset,
                "return_url": return_url,
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
        except PartialPersistError as exc:
            logger.warning("Options applied but config-write failed for subnet %s: %s", subnet_id, exc)
            messages.warning(request, kea_error_hint(exc))
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


class _BaseServerOptionsEditView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """GET/POST view for editing server-level (global) option-data.

    Loads current server options from ``config-get``, renders a formset, and on
    POST validates + saves via ``server_update_options`` (config-get → config-test
    → config-write).
    """

    dhcp_version: int = 4

    def _get_options_from_config(self, client) -> list[dict] | None:
        """Fetch config and return the server-level option-data list, or None on error."""
        service = f"dhcp{self.dhcp_version}"
        dhcp_key = f"Dhcp{self.dhcp_version}"
        try:
            resp = client.command("config-get", service=[service])
        except (KeaException, requests.RequestException):
            logger.exception("Failed to fetch config-get for server options (version %s)", self.dhcp_version)
            return None
        config = resp[0].get("arguments") or {}
        return config.get(dhcp_key, {}).get("option-data", [])

    def get(self, request, pk: int):
        server = get_object_or_404(
            Server.objects.restrict(request.user, "change"),
            pk=pk,
        )
        client = server.get_client(version=self.dhcp_version)
        existing = self._get_options_from_config(client)
        if existing is None:
            messages.error(request, "Could not load server options from Kea. The form cannot be displayed.")
            return redirect(reverse("plugins:netbox_kea:server", args=[pk]))
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
        except PartialPersistError as exc:
            logger.warning("Server options applied but config-write failed for dhcp%s: %s", self.dhcp_version, exc)
            messages.warning(request, kea_error_hint(exc))
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
            servers = Server.objects.restrict(request.user, "change").filter(dhcp4=True)
            add_url_name = "plugins:netbox_kea:server_reservation4_add"
        else:
            servers = Server.objects.restrict(request.user, "change").filter(dhcp6=True)
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
            except (KeaException, requests.RequestException, ValueError):
                online = False
            statuses.append({"version": version, "online": online})

        return render(
            request,
            "netbox_kea/server_status_badge.html",
            {"server": server, "statuses": statuses},
        )


# ---------------------------------------------------------------------------
# option-def views
# ---------------------------------------------------------------------------


class BaseServerOptionDefView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """List option-def entries for a Kea server.

    Subclasses set ``dhcp_version`` to 4 or 6.
    """

    dhcp_version: int

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the option-def list."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        if resp := check_dhcp_enabled(server, self.dhcp_version):
            return resp
        client = server.get_client(version=self.dhcp_version)
        try:
            option_defs = client.option_def_list(version=self.dhcp_version)
            options_load_error = False
        except (KeaException, requests.RequestException, ValueError):
            logger.exception("Failed to fetch option-def list for server %s", pk)
            option_defs = []
            options_load_error = True
        # Annotate each entry with a pre-built delete URL so templates don't
        # need to construct dynamic URL names.
        enriched_defs = []
        for opt in option_defs:
            entry = dict(opt)
            entry["delete_url"] = reverse(
                f"plugins:netbox_kea:server_option_def{self.dhcp_version}_delete",
                args=[server.pk, opt["code"], opt["space"]],
            )
            enriched_defs.append(entry)
        return render(
            request,
            "netbox_kea/server_option_def_list.html",
            {
                "object": server,
                "server": server,
                "option_defs": enriched_defs,
                "options_load_error": options_load_error,
                "dhcp_version": self.dhcp_version,
                "add_url": reverse(
                    f"plugins:netbox_kea:server_option_def{self.dhcp_version}_add",
                    args=[server.pk],
                ),
                "tab": self.tab,
            },
        )


@register_model_view(Server, "option_def6")
class ServerOptionDef6View(BaseServerOptionDefView):
    """DHCPv6 option-def tab."""

    tab = OptionalViewTab(label="DHCPv6 Option Definitions", weight=1055, is_enabled=lambda s: s.dhcp6)
    dhcp_version = 6


@register_model_view(Server, "option_def4")
class ServerOptionDef4View(BaseServerOptionDefView):
    """DHCPv4 option-def tab."""

    tab = OptionalViewTab(label="DHCPv4 Option Definitions", weight=1050, is_enabled=lambda s: s.dhcp4)
    dhcp_version = 4


class BaseServerOptionDefAddView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Add a custom option definition to a Kea server.

    Subclasses set ``dhcp_version`` to 4 or 6.
    """

    dhcp_version: int

    def _success_url(self, server: Server) -> str:
        return reverse(f"plugins:netbox_kea:server_option_def{self.dhcp_version}", args=[server.pk])

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the add option-def form."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = forms.OptionDefForm(initial={"space": f"dhcp{self.dhcp_version}"})
        return render(
            request,
            "netbox_kea/server_option_def_add.html",
            {
                "object": server,
                "server": server,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._success_url(server),
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate and create the option definition."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = forms.OptionDefForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "netbox_kea/server_option_def_add.html",
                {
                    "object": server,
                    "server": server,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "cancel_url": self._success_url(server),
                },
            )
        option_def = {
            "name": form.cleaned_data["name"],
            "code": form.cleaned_data["code"],
            "type": form.cleaned_data["type"],
            "space": form.cleaned_data["space"],
        }
        if form.cleaned_data.get("array"):
            option_def["array"] = True
        try:
            client = server.get_client(version=self.dhcp_version)
            client.option_def_add(version=self.dhcp_version, option_def=option_def)
            messages.success(request, f"Option definition '{option_def['name']}' (code {option_def['code']}) added.")
        except PartialPersistError as exc:
            logger.warning("Option def applied but config-write failed for %s: %s", server, exc)
            messages.warning(request, kea_error_hint(exc))
        except KeaException as exc:
            logger.warning("option-def add failed for %s: %s", server, exc)
            messages.error(request, f"Kea error: {kea_error_hint(exc)}")
        return redirect(self._success_url(server))


class ServerOptionDef6AddView(BaseServerOptionDefAddView):
    """Add a DHCPv6 option definition."""

    dhcp_version = 6


class ServerOptionDef4AddView(BaseServerOptionDefAddView):
    """Add a DHCPv4 option definition."""

    dhcp_version = 4


class BaseServerOptionDefDeleteView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Delete a custom option definition from a Kea server.

    The option code and space are passed as URL kwargs.
    """

    dhcp_version: int

    def _success_url(self, server: Server) -> str:
        return reverse(f"plugins:netbox_kea:server_option_def{self.dhcp_version}", args=[server.pk])

    def get(self, request: HttpRequest, pk: int, code: int, space: str) -> HttpResponse:
        """Render the delete-confirmation page."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        return render(
            request,
            "netbox_kea/server_option_def_delete.html",
            {
                "object": server,
                "server": server,
                "code": code,
                "space": space,
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._success_url(server),
            },
        )

    def post(self, request: HttpRequest, pk: int, code: int, space: str) -> HttpResponse:
        """Delete the option definition."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        try:
            client = server.get_client(version=self.dhcp_version)
            client.option_def_del(version=self.dhcp_version, code=code, space=space)
            messages.success(request, f"Option definition code={code} space={space} deleted.")
        except PartialPersistError as exc:
            logger.warning("Option def del applied but config-write failed for %s: %s", server, exc)
            messages.warning(request, kea_error_hint(exc))
        except KeaException as exc:
            logger.warning("option-def del failed for %s: %s", server, exc)
            messages.error(request, f"Kea error: {kea_error_hint(exc)}")
        return redirect(self._success_url(server))


class ServerOptionDef6DeleteView(BaseServerOptionDefDeleteView):
    """Delete a DHCPv6 option definition."""

    dhcp_version = 6


class ServerOptionDef4DeleteView(BaseServerOptionDefDeleteView):
    """Delete a DHCPv4 option definition."""

    dhcp_version = 4
