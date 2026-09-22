import logging
from typing import Any

import requests
from django.contrib import messages
from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from netbox.views import generic
from utilities.views import register_model_view

from .. import forms, server_configuration, tables
from ..constants import Family
from ..kea import KeaException, PartialPersistError
from ..models import Server
from ..utilities import (
    check_dhcp_enabled,
    kea_error_hint,
)
from ._base import (
    ConditionalLoginRequiredMixin,
    _diagnostic_messages,
    _KeaChangeMixin,
    _shared_network_row,
    _subnet_option_fields,
)
from .subnets import _SUBNETS_TAB, subnets_nav_context

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared Networks views
# ─────────────────────────────────────────────────────────────────────────────


class BaseServerSharedNetworksView(generic.ObjectChildrenView):
    """Read-only tab listing shared networks from the Kea config."""

    table = tables.SharedNetworkTable
    queryset = Server.objects.all()
    template_name = "netbox_kea/server_shared_networks.html"
    dhcp_version: Family

    def get_children(self, request: HttpRequest, parent: Server) -> list[dict[str, Any]]:
        """Return Shared Network rows from the Server Configuration snapshot."""
        if check_dhcp_enabled(parent, self.dhcp_version) is not None:
            return []
        snapshot = server_configuration.display(parent, self.dhcp_version)
        _diagnostic_messages(
            request, snapshot.diagnostics, messages.ERROR if not snapshot.available else messages.WARNING
        )
        if not snapshot.available:
            messages.error(request, "Failed to load Shared Network configuration from Kea.")
            return []
        can_change = Server.objects.restrict(request.user, "change").filter(pk=parent.pk).exists()
        return [
            _shared_network_row(network, parent, self.dhcp_version, can_change) for network in snapshot.shared_networks
        ]

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
                **subnets_nav_context(instance.pk, "shared_networks", self.dhcp_version),
            },
        )


@register_model_view(Server, "shared_networks6")
class ServerSharedNetworks6View(BaseServerSharedNetworksView):
    """DHCPv6 shared networks view (rendered under the shared Subnets tab)."""

    dhcp_version = 6


@register_model_view(Server, "shared_networks4")
class ServerSharedNetworks4View(BaseServerSharedNetworksView):
    """DHCPv4 shared networks view (rendered under the shared Subnets tab)."""

    dhcp_version = 4

    def get(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        """Redirect to the v6 view on v6-only servers so the merged tab works."""
        instance = self.get_object(**kwargs)
        if not instance.dhcp4 and instance.dhcp6:
            return redirect(reverse("plugins:netbox_kea:server_shared_networks6", args=[instance.pk]))
        return super().get(request, **kwargs)


class BaseServerSharedNetworkAddView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Add a new shared network to a Kea server.

    Subclasses set ``dhcp_version`` to 4 or 6.
    """

    dhcp_version: Family

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
                "tab": self.tab,
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
                    "tab": self.tab,
                },
            )
        name = form.cleaned_data["name"]
        try:
            client = server.get_client(version=self.dhcp_version)
            client.network_add(version=self.dhcp_version, name=name)
            messages.success(request, f"Shared network '{name}' created.")
        except PartialPersistError as exc:
            logger.warning("network%d-add partial persist for %s: %s", self.dhcp_version, server, exc)
            messages.warning(
                request,
                f"Shared network '{name}' created on the live server but config persistence failed. "
                "Manual reconciliation may be required.",
            )
        except KeaException as exc:
            logger.warning("network%d-add failed for %s: %s", self.dhcp_version, server, exc)
            messages.error(request, f"Kea error: {kea_error_hint(exc)}")
        except (requests.RequestException, ValueError):
            logger.exception("Transport error adding shared network for %s", server)
            messages.error(request, "An internal error occurred.")
        return redirect(self._success_url(server))


class ServerSharedNetwork6AddView(BaseServerSharedNetworkAddView):
    """Add a new DHCPv6 shared network."""

    dhcp_version = 6
    tab = _SUBNETS_TAB


class ServerSharedNetwork4AddView(BaseServerSharedNetworkAddView):
    """Add a new DHCPv4 shared network."""

    dhcp_version = 4
    tab = _SUBNETS_TAB


class BaseServerSharedNetworkDeleteView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Delete a shared network from a Kea server.

    The network name is passed as a URL kwarg ``network_name``.  Subnets that
    belonged to the deleted network fall back to the global address pool.
    """

    dhcp_version: Family

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
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, network_name: str) -> HttpResponse:
        """Delete the shared network."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        try:
            client = server.get_client(version=self.dhcp_version)
            client.network_del(version=self.dhcp_version, name=network_name)
            messages.success(request, f"Shared network '{network_name}' deleted.")
        except PartialPersistError as exc:
            logger.warning("network%d-del partial persist for %s: %s", self.dhcp_version, server, exc)
            messages.warning(
                request,
                f"Shared network '{network_name}' deleted on the live server but config persistence failed. "
                "Manual reconciliation may be required.",
            )
        except KeaException as exc:
            logger.warning("network%d-del failed for %s: %s", self.dhcp_version, server, exc)
            messages.error(request, f"Kea error: {kea_error_hint(exc)}")
        except (requests.RequestException, ValueError):
            logger.exception("Transport error deleting shared network for %s", server)
            messages.error(request, "An internal error occurred.")
        return redirect(self._success_url(server))


class ServerSharedNetwork6DeleteView(BaseServerSharedNetworkDeleteView):
    """Delete a DHCPv6 shared network."""

    dhcp_version = 6
    tab = _SUBNETS_TAB


class ServerSharedNetwork4DeleteView(BaseServerSharedNetworkDeleteView):
    """Delete a DHCPv4 shared network."""

    dhcp_version = 4
    tab = _SUBNETS_TAB


class BaseServerSharedNetworkEditView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Edit a Shared Network's managed configuration fields."""

    dhcp_version: Family

    def _success_url(self, server: Server) -> str:
        return reverse(f"plugins:netbox_kea:server_shared_networks{self.dhcp_version}", args=[server.pk])

    def get(self, request: HttpRequest, pk: int, network_name: str) -> HttpResponse:
        """Render the edit form pre-populated with current values."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        configuration = server_configuration.for_verification(server, self.dhcp_version)
        _diagnostic_messages(
            request,
            configuration.diagnostics,
            messages.ERROR if not configuration.available else messages.WARNING,
        )
        network = next((network for network in configuration.shared_networks if network.name == network_name), None)

        if not configuration.shared_networks_complete or network is None or not network.complete:
            messages.error(request, f"Shared network '{network_name}' not found or could not be retrieved.")
            return redirect(self._success_url(server))

        option_fields = _subnet_option_fields(network.options, self.dhcp_version)
        initial: dict[str, Any] = {
            "name": network_name,
            "description": network.description or "",
            "interface": network.interface or "",
            "relay_addresses": ", ".join(str(address) for address in network.relay_addresses),
            "dns_servers": option_fields.get("dns_servers", ""),
            "ntp_servers": option_fields.get("ntp_servers", ""),
        }

        form = forms.SharedNetworkEditForm(initial=initial)
        return render(
            request,
            "netbox_kea/server_shared_network_edit.html",
            {
                "object": server,
                "form": form,
                "network_name": network_name,
                "dhcp_version": self.dhcp_version,
                "cancel_url": self._success_url(server),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, network_name: str) -> HttpResponse:
        """Validate form and apply the shared network update."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = forms.SharedNetworkEditForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "netbox_kea/server_shared_network_edit.html",
                {
                    "object": server,
                    "form": form,
                    "network_name": network_name,
                    "dhcp_version": self.dhcp_version,
                    "cancel_url": self._success_url(server),
                    "tab": self.tab,
                },
            )

        cd = form.cleaned_data
        relay_addresses = (
            [s.strip() for s in cd["relay_addresses"].split(",") if s.strip()] if cd["relay_addresses"] else []
        )
        dns_servers = [address for address in cd["dns_servers"].split(",") if address]
        ntp_servers = [address for address in cd["ntp_servers"].split(",") if address]

        configuration = server_configuration.for_verification(server, self.dhcp_version)
        _diagnostic_messages(
            request,
            configuration.diagnostics,
            messages.ERROR if not configuration.available else messages.WARNING,
        )
        network = next((network for network in configuration.shared_networks if network.name == network_name), None)
        if (
            not configuration.available
            or not configuration.shared_networks_complete
            or network is None
            or not network.complete
        ):
            logger.warning(
                "Failed to reload current Shared Network %r on server %s. The update was aborted.",
                network_name,
                server.pk,
            )
            messages.error(request, "Could not reload current network state; update aborted to prevent data loss.")
            return render(
                request,
                "netbox_kea/server_shared_network_edit.html",
                {
                    "object": server,
                    "form": form,
                    "network_name": network_name,
                    "dhcp_version": self.dhcp_version,
                    "cancel_url": self._success_url(server),
                    "tab": self.tab,
                },
            )

        try:
            client = server.get_client(version=self.dhcp_version)
            client.network_update(
                version=self.dhcp_version,
                name=network_name,
                description=cd.get("description") or "",
                interface=cd.get("interface") or "",
                relay_addresses=relay_addresses,
                dns_servers=dns_servers,
                ntp_servers=ntp_servers,
            )
            messages.success(request, f"Shared network '{network_name}' updated.")
        except PartialPersistError:
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.warning("network_update failed for %s on server %s: %s", network_name, pk, exc)
            messages.error(request, f"Kea error: {kea_error_hint(exc)}")
        except (requests.RequestException, ValueError):
            logger.exception("Transport error updating shared network '%s' on server %s", network_name, pk)
            messages.error(request, "An internal error occurred.")
        return redirect(self._success_url(server))


class ServerSharedNetwork6EditView(BaseServerSharedNetworkEditView):
    """Edit a DHCPv6 shared network."""

    dhcp_version = 6
    tab = _SUBNETS_TAB


class ServerSharedNetwork4EditView(BaseServerSharedNetworkEditView):
    """Edit a DHCPv4 shared network."""

    dhcp_version = 4
    tab = _SUBNETS_TAB
