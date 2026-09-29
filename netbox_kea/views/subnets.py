import ipaddress
import logging
import re
from typing import Any

import requests
from django.contrib import messages
from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from netbox.views import generic
from utilities.htmx import htmx_partial
from utilities.views import register_model_view

from .. import config_write, forms, server_configuration, tables
from ..constants import Family
from ..dhcp_options import form_option_fields
from ..kea import KeaException, subnet_network
from ..models import Server
from ..pools import Pool, addresses_in_pools, parse_pool
from ..reservations import InSubnetReservationScope
from ..subnet_catalogue import (
    CatalogueSnapshot,
    CatalogueUnavailable,
    ConfiguredSubnet,
    SubnetIdentityConflict,
    SubnetIdExhausted,
    VerifiedSubnet,
)
from ..subnet_catalogue import display as subnet_catalogue
from ..utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    export_table,
    kea_error_hint,
)
from ._base import (
    _POOL_RE,
    _catalogue_subnet_row,
    _diagnostic_messages,
    _enrich_subnet_statistics,
    _KeaChangeMixin,
    _run_config_change,
)

logger = logging.getLogger(__name__)

_NO_SUBNET_CIDR = (
    "The page did not send a valid Subnet CIDR, so nothing was sent to Kea. Reload the page and try again."
)

# Single consolidated "Subnets" tab covering subnets AND shared networks for both
# protocols. Owned by ServerDHCP4SubnetsView (the one class-level tab); the other
# three list views (subnets6, shared_networks4/6) inject it via render context.
# Two in-page toggles — section (Subnets | Shared Networks) and family (v4 | v6) —
# switch between the four underlying URLs, which are all unchanged.
_SUBNETS_TAB = OptionalViewTab(label="Subnets", weight=1020, is_enabled=lambda s: s.dhcp4 or s.dhcp6)


def subnets_nav_context(server_pk: int, section: str, dhcp_version: Family) -> dict[str, Any]:
    """Build context for the Subnets/Shared-Networks section+family toggle nav.

    ``section`` is ``"subnets"`` or ``"shared_networks"``. Returns the shared tab
    plus the four URLs the toggles link to.
    """
    return {
        "tab": _SUBNETS_TAB,
        "section": section,
        "dhcp_version": dhcp_version,
        "nav_subnets_url": reverse(f"plugins:netbox_kea:server_subnets{dhcp_version}", args=[server_pk]),
        "nav_shared_url": reverse(f"plugins:netbox_kea:server_shared_networks{dhcp_version}", args=[server_pk]),
        "nav_v4_url": reverse(f"plugins:netbox_kea:server_{section}4", args=[server_pk]),
        "nav_v6_url": reverse(f"plugins:netbox_kea:server_{section}6", args=[server_pk]),
    }


class BaseServerDHCPSubnetsView(generic.ObjectChildrenView):
    """Base view for the subnet list tab; fetches subnet data from Kea config."""

    table = tables.SubnetTable
    queryset = Server.objects.all()
    template_name = "netbox_kea/server_dhcp_subnets.html"

    def get_children(self, request: HttpRequest, parent: Server) -> list[dict[str, Any]]:
        """Return safe Subnet Catalogue rows for this Server."""
        snapshot = subnet_catalogue(parent, self.dhcp_version)
        _diagnostic_messages(
            request, snapshot.diagnostics, messages.ERROR if snapshot.unavailable else messages.WARNING
        )
        if snapshot.unavailable:
            messages.error(request, "Failed to load subnet configuration from Kea.")
            return []
        can_change = Server.objects.restrict(request.user, "change").filter(pk=parent.pk).exists()
        subnets: tuple[VerifiedSubnet | ConfiguredSubnet, ...] = (*snapshot.subnets, *snapshot.configured_subnets)
        rows = [_catalogue_subnet_row(subnet, parent, self.dhcp_version, can_change) for subnet in subnets]
        _enrich_subnet_statistics(rows, parent, self.dhcp_version)
        return rows

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
                **subnets_nav_context(instance.pk, "subnets", self.dhcp_version),
            },
        )


@register_model_view(Server, "subnets6")
class ServerDHCP6SubnetsView(BaseServerDHCPSubnetsView):
    """DHCPv6 subnets view (rendered under the shared Subnets tab)."""

    dhcp_version = 6


@register_model_view(Server, "subnets4")
class ServerDHCP4SubnetsView(BaseServerDHCPSubnetsView):
    """DHCPv4 subnets view; owns the shared Subnets tab."""

    tab = _SUBNETS_TAB
    dhcp_version = 4

    def get(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        """Redirect to the v6 view on v6-only servers so the merged tab works."""
        instance = self.get_object(**kwargs)
        if not instance.dhcp4 and instance.dhcp6:
            return redirect(reverse("plugins:netbox_kea:server_subnets6", args=[instance.pk]))
        return super().get(request, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 10: Pool management views
# ─────────────────────────────────────────────────────────────────────────────


def _warn_reservations_in_pool(
    request: HttpRequest,
    server: Server,
    catalogue: CatalogueSnapshot,
    subnet: VerifiedSubnet,
    pool: Pool,
) -> None:
    """Add a non-blocking warning when a Reservation of *subnet* has an address in *pool*.

    Warns when the check cannot run.
    """
    check_failed_message = (
        f"The Reservation overlap check did not run. Pool {pool.range} was not checked against existing Reservations."
    )
    try:
        client = server.get_client(version=catalogue.family)
        snapshot = client.reservation_snapshot(catalogue.family, catalogue, page_size=200, subnet_id=subnet.subnet_id)
        if not snapshot.complete:
            # No warning below would otherwise read as "no overlapping reservation".
            messages.warning(
                request,
                f"Could not read every reservation on this server, so pool {pool.range} was checked "
                "against an incomplete list.",
            )
        addresses = [
            address
            for reservation in snapshot.records
            if isinstance(reservation.scope, InSubnetReservationScope)
            and reservation.scope.subnet.subnet_id == subnet.subnet_id
            for address in reservation.addresses
        ]
        overlapping = [str(address) for address, _pool in addresses_in_pools(addresses, (pool,))]
        if overlapping:
            sample = ", ".join(overlapping[:5])
            extra = f" (+{len(overlapping) - 5} more)" if len(overlapping) > 5 else ""
            messages.warning(
                request,
                f"Pool {pool.range} overlaps {len(overlapping)} existing reservation(s): {sample}{extra}. "
                "Kea allows this. Reservations take priority over pool allocation.",
            )
    except (KeaException, requests.RequestException, OSError, RuntimeError, ValueError):
        logger.warning("Could not check Pool and Reservation overlap for subnet %s", subnet.subnet_id, exc_info=True)
        messages.warning(request, check_failed_message)


def _unconfirmed_subnet(request: HttpRequest, subnet_id: int, return_url: str) -> HttpResponse:
    """Leave a change page whose Subnet NetBox cannot confirm, so no form carries an empty CIDR."""
    messages.error(
        request, f"NetBox could not confirm Subnet {subnet_id} in Kea. Reload the Subnets page and try again."
    )
    return redirect(return_url)


def _displayed_subnet(
    request: HttpRequest, server: Server, family: Family, subnet_id: int
) -> tuple[CatalogueSnapshot, VerifiedSubnet | None]:
    """Return the displayed Subnet Catalogue and its Verified Subnet with *subnet_id*; show why it is missing."""
    catalogue = subnet_catalogue(server, family)
    found = catalogue.find_by_id(subnet_id)
    if isinstance(found, VerifiedSubnet):
        return catalogue, found
    _diagnostic_messages(request, catalogue.diagnostics, messages.ERROR if catalogue.unavailable else messages.WARNING)
    return catalogue, None


class _BasePoolAddView(_KeaChangeMixin, generic.ObjectView):
    """Base view for adding a pool to a subnet."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_pool_add.html"
    dhcp_version: Family  # set on subclasses

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def _render(self, request: HttpRequest, server: Server, subnet_id: int, form: forms.PoolAddForm) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "subnet_id": subnet_id,
                "subnet_cidr": form.subnet.cidr if form.subnet is not None else "",
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(server.pk),
                "tab": self.tab,
            },
        )

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        catalogue, subnet = _displayed_subnet(request, server, self.dhcp_version, subnet_id)
        initial = {"subnet_cidr": subnet.cidr} if subnet is not None else {}
        form = forms.PoolAddForm(initial=initial, subnet=subnet, absence_confirmed=catalogue.confirms_absence)
        return self._render(request, server, subnet_id, form)

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        catalogue, subnet = _displayed_subnet(request, server, self.dhcp_version, subnet_id)
        form = forms.PoolAddForm(request.POST, subnet=subnet, absence_confirmed=catalogue.confirms_absence)
        if not form.is_valid() or form.subnet is None:
            return self._render(request, server, subnet_id, form)
        pool: Pool = form.cleaned_data["pool"]
        cidr: str = form.cleaned_data["subnet_cidr"]
        outcome = _run_config_change(
            request,
            # Kea accepts an explicit range for both families, and the Subnets table shows this text.
            f"Pool {pool.range} added to subnet {subnet_id}.",
            lambda: config_write.add_pool(server, self.dhcp_version, subnet_id, cidr, pool),
        )
        if outcome is not None:
            _warn_reservations_in_pool(request, server, catalogue, form.subnet, pool)
        return redirect(self._subnets_url(pk))


class ServerSubnet4PoolAddView(_BasePoolAddView):
    """Add a pool to a DHCPv4 subnet."""

    dhcp_version = 4
    tab = _SUBNETS_TAB


class ServerSubnet6PoolAddView(_BasePoolAddView):
    """Add a pool to a DHCPv6 subnet."""

    dhcp_version = 6
    tab = _SUBNETS_TAB


class _BasePoolDeleteView(_KeaChangeMixin, generic.ObjectView):
    """Base view for deleting a pool from a subnet."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_pool_delete.html"
    dhcp_version: Family

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int, subnet_id: int, pool: str) -> HttpResponse:
        pool = pool.strip()
        if not _POOL_RE.match(re.sub(r"\s+", "", pool)):
            return HttpResponse("Invalid pool format.", status=400)
        server = self.get_object(pk=pk)
        _, subnet = _displayed_subnet(request, server, self.dhcp_version, subnet_id)
        if subnet is None:
            return _unconfirmed_subnet(request, subnet_id, self._subnets_url(pk))
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "pool": pool,
                "subnet_id": subnet_id,
                "subnet_cidr": subnet.cidr,
                "form": forms.SubnetConfirmForm(initial={"subnet_cidr": subnet.cidr}, family=self.dhcp_version),
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, pool: str) -> HttpResponse:
        pool = pool.strip()
        if not _POOL_RE.match(re.sub(r"\s+", "", pool)):
            return HttpResponse("Invalid pool format.", status=400)
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        form = forms.SubnetConfirmForm(request.POST, family=self.dhcp_version)
        if not form.is_valid():
            messages.error(request, _NO_SUBNET_CIDR)
            return redirect(return_url)
        cidr: str = form.cleaned_data["subnet_cidr"]
        try:
            parsed_pool = parse_pool(pool, subnet_network(cidr, self.dhcp_version))
        except ValueError:
            messages.error(request, f"Pool {pool} is not a valid Pool of Subnet {cidr}. Nothing was sent to Kea.")
            return redirect(return_url)
        _run_config_change(
            request,
            f"Pool {parsed_pool.range} removed from subnet {subnet_id}.",
            lambda: config_write.delete_pool(server, self.dhcp_version, subnet_id, cidr, parsed_pool),
        )
        return redirect(return_url)


class ServerSubnet4PoolDeleteView(_BasePoolDeleteView):
    """Delete a pool from a DHCPv4 subnet."""

    dhcp_version = 4
    tab = _SUBNETS_TAB


class ServerSubnet6PoolDeleteView(_BasePoolDeleteView):
    """Delete a pool from a DHCPv6 subnet."""

    dhcp_version = 6
    tab = _SUBNETS_TAB


# ---------------------------------------------------------------------------
# Subnet add / delete views
# ---------------------------------------------------------------------------


def _network_choices(snapshot: server_configuration.ServerConfigurationSnapshot) -> list[tuple[str, str]]:
    """Return declared Shared Networks, including those without members."""
    return [("", "— (global pool) —"), *((network.name, network.name) for network in snapshot.shared_networks)]


def _inherited_subnet_options(
    snapshot: server_configuration.ServerConfigurationSnapshot,
    current_network: str,
    form_values: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Return option hints not overridden by the Subnet form."""
    inherited = {
        field: {"value": value, "source": "global"}
        for field, value in form_option_fields(snapshot.global_options, snapshot.family).items()
    }
    network = next((network for network in snapshot.shared_networks if network.name == current_network), None)
    if network is not None:
        inherited.update(
            {
                field: {"value": value, "source": f"shared-network: {current_network}"}
                for field, value in form_option_fields(network.options, snapshot.family).items()
            }
        )
    return {field: hint for field, hint in inherited.items() if not form_values.get(field)}


class _BaseSubnetAddView(_KeaChangeMixin, generic.ObjectView):
    """Base view for adding a new subnet to Kea."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_subnet_add.html"
    dhcp_version: Family

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def _render(self, request: HttpRequest, server: Server, form: forms.SubnetAddForm) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(server.pk),
                "tab": self.tab,
            },
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        form = forms.SubnetAddForm()
        configuration = server_configuration.display(server, self.dhcp_version)
        _diagnostic_messages(
            request, configuration.diagnostics, messages.WARNING if configuration.available else messages.ERROR
        )
        if configuration.shared_networks_complete:
            form.fields["shared_network"].choices = _network_choices(configuration)
        else:
            messages.warning(request, "Could not load shared networks from Kea — retry later.")
            form.fields["shared_network"].choices = [("", "— failed to load networks —")]
            form.fields["shared_network"].disabled = True
        return self._render(request, server, form)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        configuration = server_configuration.for_verification(server, self.dhcp_version)
        network_choices = _network_choices(configuration)
        _diagnostic_messages(request, configuration.diagnostics, messages.WARNING)
        if not configuration.shared_networks_complete:
            form = forms.SubnetAddForm(request.POST)
            form.fields["shared_network"].choices = [("", "— (global pool) —")]
            form.fields["shared_network"].disabled = True
            form.add_error(None, "Could not load shared networks from Kea. Please try again.")
            return self._render(request, server, form)

        form = forms.SubnetAddForm(request.POST)
        form.fields["shared_network"].choices = network_choices
        if not form.is_valid():
            return self._render(request, server, form)
        cd = form.cleaned_data
        cidr: str = cd["subnet"]
        if ipaddress.ip_network(cidr).version != self.dhcp_version:
            form.add_error("subnet", f"Enter an IPv{self.dhcp_version} Subnet CIDR.")
            return self._render(request, server, form)
        shared_network: str | None = cd["shared_network"] or None
        fields = form.to_fields()

        def added(outcome: config_write.SubnetAddOutcome) -> str:
            joined = f" to Shared Network '{shared_network}'" if shared_network else ""
            return f"Subnet {outcome.subnet_id} ({cidr}) added{joined}."

        try:
            outcome = _run_config_change(
                request,
                added,
                lambda: config_write.add_subnet(
                    server, self.dhcp_version, cidr, cd["subnet_id"], fields, shared_network
                ),
            )
        except SubnetIdentityConflict as exc:
            form.add_error("subnet" if exc.part == "network" else "subnet_id", str(exc))
            return self._render(request, server, form)
        except SubnetIdExhausted as exc:
            form.add_error("subnet_id", str(exc))
            return self._render(request, server, form)
        except CatalogueUnavailable:
            logger.warning("Subnet add blocked: incomplete Subnet identity for server %s", pk, exc_info=True)
            form.add_error(
                None,
                "Kea did not return a complete Subnet list, so the new Subnet identity cannot be checked. "
                "No Subnet was created. Make sure the subnet_cmds hook library is loaded, then try again.",
            )
            return self._render(request, server, form)
        if outcome is None:
            # A rejected change is not live, so the form keeps the input for another try.
            return self._render(request, server, form)
        return redirect(self._subnets_url(pk))


class ServerSubnet4AddView(_BaseSubnetAddView):
    """Add a DHCPv4 subnet."""

    dhcp_version = 4
    tab = _SUBNETS_TAB


class ServerSubnet6AddView(_BaseSubnetAddView):
    """Add a DHCPv6 subnet."""

    dhcp_version = 6
    tab = _SUBNETS_TAB


class _BaseSubnetEditView(_KeaChangeMixin, generic.ObjectView):
    """Base view for editing an existing subnet's configuration in Kea."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_subnet_edit.html"
    dhcp_version: Family

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        snapshot = subnet_catalogue(server, self.dhcp_version)
        configuration = server_configuration.for_verification(server, self.dhcp_version)
        configured_target = configuration.subnet_with_membership(subnet_id)
        subnet_configuration = None
        subnet_cidr = ""
        # Membership comes from the live declaration alone, never from the cached catalogue.
        display_network = configured_target.shared_network_name or "" if configured_target is not None else ""
        if configured_target is not None and configured_target.complete:
            subnet_configuration = configured_target.configuration
            subnet_cidr = configured_target.declared_cidr
        if subnet_configuration is None:
            declaration = server_configuration.subnet_for_display(server, self.dhcp_version, subnet_id)
            if declaration is not None:
                subnet_configuration = declaration.configuration
                subnet_cidr = declaration.declared_cidr
        shown = (
            None
            if subnet_configuration is None
            else server_configuration.shown_subnet(subnet_configuration, self.dhcp_version)
        )
        if shown is None:
            _diagnostic_messages(request, snapshot.diagnostics, messages.ERROR)
            messages.error(request, "Could not load subnet configuration from Kea.")
            return redirect(self._subnets_url(pk))
        _diagnostic_messages(
            request,
            snapshot.diagnostics + configuration.diagnostics,
            messages.WARNING if configuration.available else messages.ERROR,
        )
        membership_confirmed = configuration.available and configured_target is not None
        if not membership_confirmed:
            messages.warning(
                request,
                "Could not confirm the Shared Network of this Subnet, so this form cannot be saved. "
                "Reload the page and try again.",
            )
        initial = {
            "subnet_cidr": subnet_cidr,
            **forms.SubnetEditForm.initial_for(shown),
            "shared_network": display_network,
            "original_network": display_network,
            "original_network_confirmed": membership_confirmed,
        }
        form = forms.SubnetEditForm(initial=initial)
        form.fields["shared_network"].choices = _network_choices(configuration)
        inherited_options = (
            _inherited_subnet_options(configuration, display_network, initial)
            if configured_target is not None and configuration.shared_networks_complete
            else {}
        )
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "subnet_id": subnet_id,
                "subnet_cidr": subnet_cidr,
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
                "inherited_options": inherited_options,
                "tab": self.tab,
            },
        )

    def _render_post(
        self,
        request: HttpRequest,
        server: Server,
        subnet_id: int,
        form: forms.SubnetEditForm,
        configuration: server_configuration.ServerConfigurationSnapshot,
    ) -> HttpResponse:
        """Show the submitted form again, with the inherited option hints of the chosen Shared Network."""
        submitted = {name: value for name, value in form.data.items() if name in form.fields}
        inherited_options = (
            _inherited_subnet_options(configuration, form.data.get("shared_network", ""), submitted)
            if configuration.shared_networks_complete
            else {}
        )
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "subnet_id": subnet_id,
                "subnet_cidr": form.data.get("subnet_cidr", ""),
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(server.pk),
                "inherited_options": inherited_options,
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        # The form reads the Shared Networks for its choices; config_write checks the membership that the page showed.
        configuration = server_configuration.for_verification(server, self.dhcp_version)
        _diagnostic_messages(request, configuration.diagnostics, messages.WARNING)
        form = forms.SubnetEditForm(request.POST)
        complete = configuration.shared_networks_complete
        form.fields["shared_network"].choices = (
            _network_choices(configuration) if complete else [("", "— failed to load networks —")]
        )
        if not complete:
            form.fields["shared_network"].disabled = True
            form.add_error(None, "Could not load shared networks from Kea. Please try again.")
            return self._render_post(request, server, subnet_id, form, configuration)
        if not form.is_valid():
            return self._render_post(request, server, subnet_id, form, configuration)
        cd = form.cleaned_data
        cidr: str = cd["subnet_cidr"]
        if ipaddress.ip_network(cidr, strict=False).version != self.dhcp_version:
            form.add_error("subnet_cidr", f"Enter an IPv{self.dhcp_version} Subnet CIDR.")
            return self._render_post(request, server, subnet_id, form, configuration)
        edit, shown = form.to_edit(), form.shown()
        outcome = _run_config_change(
            request,
            f"Subnet {subnet_id} ({cidr}) updated.",
            lambda: config_write.edit_subnet(
                server,
                self.dhcp_version,
                subnet_id,
                cidr,
                edit,
                shown=shown,
                original_network=cd["original_network"] or None,
                shared_network=cd["shared_network"] or None,
            ),
        )
        if outcome is None:
            # A rejected change is not live, so the form keeps the input for another try.
            return self._render_post(request, server, subnet_id, form, configuration)
        return redirect(self._subnets_url(pk))


class ServerSubnet4EditView(_BaseSubnetEditView):
    """Edit a DHCPv4 subnet's configuration."""

    dhcp_version = 4
    tab = _SUBNETS_TAB


class ServerSubnet6EditView(_BaseSubnetEditView):
    """Edit a DHCPv6 subnet's configuration."""

    dhcp_version = 6
    tab = _SUBNETS_TAB


class _BaseSubnetDeleteView(_KeaChangeMixin, generic.ObjectView):
    """Base view for deleting a subnet from Kea."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_subnet_delete.html"
    dhcp_version: Family

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        configuration = server_configuration.for_verification(server, self.dhcp_version)
        _diagnostic_messages(
            request, configuration.diagnostics, messages.WARNING if configuration.available else messages.ERROR
        )
        declared = [subnet for subnet in configuration.subnets if subnet.declared_subnet_id == subnet_id]
        if len(declared) != 1:
            return _unconfirmed_subnet(request, subnet_id, self._subnets_url(pk))
        subnet_cidr = declared[0].declared_cidr
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "subnet_id": subnet_id,
                "subnet_cidr": subnet_cidr,
                "form": forms.SubnetConfirmForm(initial={"subnet_cidr": subnet_cidr}, family=self.dhcp_version),
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        form = forms.SubnetConfirmForm(request.POST, family=self.dhcp_version)
        if not form.is_valid():
            messages.error(request, _NO_SUBNET_CIDR)
            return redirect(return_url)
        cidr: str = form.cleaned_data["subnet_cidr"]
        _run_config_change(
            request,
            f"Subnet {subnet_id} ({cidr}) deleted.",
            lambda: config_write.delete_subnet(server, self.dhcp_version, subnet_id, cidr),
        )
        return redirect(return_url)


class ServerSubnet4DeleteView(_BaseSubnetDeleteView):
    """Delete a DHCPv4 subnet."""

    dhcp_version = 4
    tab = _SUBNETS_TAB


class ServerSubnet6DeleteView(_BaseSubnetDeleteView):
    """Delete a DHCPv6 subnet."""

    dhcp_version = 6
    tab = _SUBNETS_TAB


class _BaseSubnetWipeView(_KeaChangeMixin, generic.ObjectView):
    """Base view for wiping all leases in a subnet."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_subnet_wipe.html"
    dhcp_version: Family

    def _subnets_url(self, pk: int) -> str:
        return reverse(f"plugins:netbox_kea:server_subnets{self.dhcp_version}", args=[pk])

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        configuration = server_configuration.for_verification(server, self.dhcp_version)
        _diagnostic_messages(
            request, configuration.diagnostics, messages.WARNING if configuration.available else messages.ERROR
        )
        declared = [subnet for subnet in configuration.subnets if subnet.declared_subnet_id == subnet_id]
        subnet_cidr = declared[0].declared_cidr if len(declared) == 1 else ""
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "subnet_id": subnet_id,
                "subnet_cidr": subnet_cidr,
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        try:
            client = server.get_client(version=self.dhcp_version)
        except (requests.RequestException, ValueError):
            logger.exception("Failed to connect to Kea for lease wipe on server %s", pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return redirect(return_url)
        try:
            client.lease_wipe(version=self.dhcp_version, subnet_id=subnet_id)
            messages.success(request, f"All leases in subnet {subnet_id} wiped.")
        except KeaException as exc:
            logger.exception("Failed to wipe leases in subnet %s", subnet_id)
            if exc.unsupported_command:
                messages.error(
                    request,
                    "Failed to wipe leases: ensure the lease_cmds hook is loaded.",
                )
            else:
                messages.error(request, kea_error_hint(exc))
        except requests.RequestException:
            logger.exception("Failed to wipe leases in subnet %s (network error)", subnet_id)
            messages.error(request, "Network error communicating with Kea: see server logs.")
        except ValueError:
            logger.exception("Failed to wipe leases in subnet %s", subnet_id)
            messages.error(request, "Failed to wipe leases: see server logs for details.")
        return redirect(return_url)


class ServerSubnet4WipeView(_BaseSubnetWipeView):
    """Wipe all DHCPv4 leases in a subnet."""

    dhcp_version = 4
    tab = _SUBNETS_TAB


class ServerSubnet6WipeView(_BaseSubnetWipeView):
    """Wipe all DHCPv6 leases in a subnet."""

    dhcp_version = 6
    tab = _SUBNETS_TAB
