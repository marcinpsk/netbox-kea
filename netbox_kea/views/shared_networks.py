import logging
from typing import Any
from urllib.parse import urlencode as _urlencode

import requests
from django.contrib import messages
from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from netbox.views import generic
from utilities.views import register_model_view

from .. import forms, tables
from ..kea import KeaClient, KeaException, PartialPersistError
from ..models import Server
from ..utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    kea_error_hint,
)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin

logger = logging.getLogger(__name__)


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
        try:
            client = parent.get_client(version=self.dhcp_version)
            config = client.command("config-get", service=[f"dhcp{self.dhcp_version}"])
        except KeaException:
            logger.debug("Failed to fetch config-get for shared networks on server %s", parent.pk)
            return []
        except (requests.RequestException, ValueError):
            logger.debug("Transport error fetching config-get for shared networks on server %s", parent.pk)
            return []
        if not config or not isinstance(config[0], dict):
            return []
        args = config[0].get("arguments")
        if not isinstance(args, dict):
            return []
        dhcp_conf = args.get(f"Dhcp{self.dhcp_version}", {})
        can_change = Server.objects.restrict(request.user, "change").filter(pk=parent.pk).exists()
        result = []
        for sn in dhcp_conf.get("shared-networks", []):
            if not isinstance(sn, dict):
                logger.warning("Skipping non-dict shared-network entry on server %s", parent.pk)
                continue
            subnets = sn.get(f"subnet{self.dhcp_version}", [])
            if not isinstance(subnets, list):
                subnets = []
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
                if isinstance(s, dict) and s.get("subnet")
            ]
            result.append(
                {
                    "name": sn.get("name", ""),
                    "description": sn.get("description", ""),
                    "subnet_count": len(subnets),
                    "subnet_links": subnet_links,
                    "server_pk": parent.pk,
                    "dhcp_version": self.dhcp_version,
                    "can_change": can_change,
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
                "tab": self.tab,
            },
        )


@register_model_view(Server, "shared_networks6")
class ServerSharedNetworks6View(BaseServerSharedNetworksView):
    """DHCPv6 shared networks tab."""

    tab = OptionalViewTab(label="DHCPv6 Shared Networks", weight=1035, is_enabled=lambda s: s.dhcp6)
    dhcp_version = 6


@register_model_view(Server, "shared_networks4")
class ServerSharedNetworks4View(BaseServerSharedNetworksView):
    """DHCPv4 shared networks tab."""

    tab = OptionalViewTab(label="DHCPv4 Shared Networks", weight=1025, is_enabled=lambda s: s.dhcp4)
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


class ServerSharedNetwork4DeleteView(BaseServerSharedNetworkDeleteView):
    """Delete a DHCPv4 shared network."""

    dhcp_version = 4


class BaseServerSharedNetworkEditView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Edit a shared network's description, interface, relay, and option-data.

    Shared network updates require a config-get → modify → config-test → config-write
    cycle because there is no free ``network{v}-update`` Kea hook command.
    """

    dhcp_version: int

    def _success_url(self, server: Server) -> str:
        return reverse(f"plugins:netbox_kea:server_shared_networks{self.dhcp_version}", args=[server.pk])

    def _fetch_network(self, client: "KeaClient", network_name: str) -> dict:
        """Return the shared-network dict from config-get, or {} if not found."""
        try:
            resp = client.command("config-get", service=[f"dhcp{self.dhcp_version}"])
            if isinstance(resp, list) and resp and isinstance(resp[0], dict):
                args = resp[0].get("arguments")
            elif isinstance(resp, dict):
                args = resp.get("arguments")
            else:
                logger.warning("config-get returned unexpected response shape for dhcp%s", self.dhcp_version)
                return {}
            if not isinstance(args, dict):
                logger.warning("config-get returned unexpected arguments for dhcp%s", self.dhcp_version)
                return {}
            dhcp_key = f"Dhcp{self.dhcp_version}"
            for sn in args.get(dhcp_key, {}).get("shared-networks", []):
                if sn.get("name") == network_name:
                    return sn
        except (KeaException, requests.RequestException):
            logger.exception("Failed to fetch config-get for shared network edit on server")
        return {}

    def get(self, request: HttpRequest, pk: int, network_name: str) -> HttpResponse:
        """Render the edit form pre-populated with current values."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        try:
            client = server.get_client(version=self.dhcp_version)
        except ValueError:
            logger.exception("Failed to create Kea client for shared network edit on server %s", server.pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return redirect(self._success_url(server))
        network = self._fetch_network(client, network_name)

        if not network:
            messages.error(request, f"Shared network '{network_name}' not found or could not be retrieved.")
            return redirect(self._success_url(server))

        initial: dict[str, Any] = {"name": network_name}
        initial["description"] = network.get("description", "")
        initial["interface"] = network.get("interface", "")
        relay = network.get("relay", {})
        initial["relay_addresses"] = ", ".join(relay.get("ip-addresses", []))
        for opt in network.get("option-data", []):
            opt_name = opt.get("name", "")
            if opt_name in ("domain-name-servers", "dns-servers"):
                initial["dns_servers"] = opt.get("data", "")
            elif opt_name in ("ntp-servers", "sntp-servers"):
                initial["ntp_servers"] = opt.get("data", "")

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
                },
            )

        cd = form.cleaned_data
        relay_addresses = (
            [s.strip() for s in cd["relay_addresses"].split(",") if s.strip()] if cd["relay_addresses"] else []
        )

        # Preserve option-data entries that are not DNS/NTP — we only manage those
        # two via the form and must not silently drop unrelated options on save.
        try:
            client = server.get_client(version=self.dhcp_version)
        except ValueError:
            logger.exception("Failed to create Kea client for shared network update on server %s", server.pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return render(
                request,
                "netbox_kea/server_shared_network_edit.html",
                {
                    "object": server,
                    "form": form,
                    "network_name": network_name,
                    "dhcp_version": self.dhcp_version,
                    "cancel_url": self._success_url(server),
                },
            )
        existing_network = self._fetch_network(client, network_name)
        if not existing_network:
            logger.warning(
                "Failed to reload current shared-network %r on server %s — aborting update to preserve option-data",
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
                },
            )
        preserved_options: list[dict] = [
            opt
            for opt in existing_network.get("option-data", [])
            if opt.get("name") not in ("domain-name-servers", "dns-servers", "ntp-servers", "sntp-servers")
        ]

        options: list[dict] = list(preserved_options)
        if cd.get("dns_servers"):
            dns_name = "domain-name-servers" if self.dhcp_version == 4 else "dns-servers"
            dns_aliases = ("domain-name-servers", "dns-servers")
            existing_dns = next(
                (o for o in existing_network.get("option-data", []) if o.get("name") in dns_aliases), None
            )
            new_dns = dict(existing_dns) if existing_dns else {"name": dns_name}
            new_dns["data"] = cd["dns_servers"]
            options.append(new_dns)
        if cd.get("ntp_servers"):
            ntp_name = "ntp-servers" if self.dhcp_version == 4 else "sntp-servers"
            ntp_aliases = ("ntp-servers", "sntp-servers")
            existing_ntp = next(
                (o for o in existing_network.get("option-data", []) if o.get("name") in ntp_aliases), None
            )
            new_ntp = dict(existing_ntp) if existing_ntp else {"name": ntp_name}
            new_ntp["data"] = cd["ntp_servers"]
            options.append(new_ntp)

        try:
            client.network_update(
                version=self.dhcp_version,
                name=network_name,
                description=cd.get("description") or "",
                interface=cd.get("interface") or "",
                relay_addresses=relay_addresses,
                options=options,
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


class ServerSharedNetwork4EditView(BaseServerSharedNetworkEditView):
    """Edit a DHCPv4 shared network."""

    dhcp_version = 4
