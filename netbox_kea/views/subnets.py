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

from .. import forms, server_configuration, tables
from ..constants import Family
from ..kea import KeaClient, KeaException, PartialPersistError
from ..models import Server
from ..reservations import InSubnetReservationScope
from ..subnet_catalogue import ConfiguredSubnet, Diagnostic, VerifiedSubnet
from ..subnet_catalogue import display as subnet_catalogue
from ..utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    export_table,
    kea_error_hint,
    parse_pool_range,
)
from ._base import (
    _catalogue_subnet_row,
    _enrich_subnet_statistics,
    _KeaChangeMixin,
    _subnet_option_fields,
    _unsupported_command,
)

logger = logging.getLogger(__name__)

_POOL_RE = re.compile(r"^[0-9a-fA-F.:/-]{3,100}$")

# Single consolidated "Subnets" tab covering subnets AND shared networks for both
# protocols. Owned by ServerDHCP4SubnetsView (the one class-level tab); the other
# three list views (subnets6, shared_networks4/6) inject it via render context.
# Two in-page toggles — section (Subnets | Shared Networks) and family (v4 | v6) —
# switch between the four underlying URLs, which are all unchanged.
_SUBNETS_TAB = OptionalViewTab(label="Subnets", weight=1020, is_enabled=lambda s: s.dhcp4 or s.dhcp6)


def _diagnostic_messages(request: HttpRequest, diagnostics: tuple[Diagnostic, ...], level: int) -> None:
    """Show each distinct Snapshot diagnostic at its presentation level."""
    for message in dict.fromkeys(diagnostic.message for diagnostic in diagnostics):
        messages.add_message(request, level, message)


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


def _warn_pool_reservation_overlap(
    request: HttpRequest,
    server: Server,
    client: "KeaClient",
    version: Family,
    subnet_id: int,
    pool_str: str,
) -> None:
    """Add a non-blocking warning if any existing reservation IP falls within *pool_str*.

    Uses the shared typed Reservation Snapshot and checks each verified record
    against the pool range. Warns when the check cannot run.
    """
    check_failed_message = (
        f"The Reservation overlap check did not run. Pool {pool_str} was not checked against existing Reservations."
    )
    try:
        from netaddr import IPAddress

        pool_range = parse_pool_range(pool_str)

        snapshot = client.reservation_snapshot(
            version, subnet_catalogue(server, version), page_size=200, subnet_id=subnet_id
        )
        if not snapshot.complete:
            # No warning below would otherwise read as "no overlapping reservation".
            messages.warning(
                request,
                f"Could not read every reservation on this server, so pool {pool_str} was checked "
                "against an incomplete list.",
            )
        overlapping: list[str] = []
        for reservation in snapshot.records:
            if (
                not isinstance(reservation.scope, InSubnetReservationScope)
                or reservation.scope.subnet.subnet_id != subnet_id
            ):
                continue
            overlapping.extend(
                str(address) for address in reservation.addresses if IPAddress(str(address)) in pool_range
            )

        if overlapping:
            sample = ", ".join(overlapping[:5])
            extra = f" (+{len(overlapping) - 5} more)" if len(overlapping) > 5 else ""
            messages.warning(
                request,
                f"Pool {pool_str} overlaps {len(overlapping)} existing reservation(s): {sample}{extra}. "
                "Kea allows this. Reservations take priority over pool allocation.",
            )
    except (KeaException, requests.RequestException, RuntimeError, ValueError):
        logger.warning("Could not check Pool and Reservation overlap for subnet %s", subnet_id, exc_info=True)
        messages.warning(request, check_failed_message)
    except Exception:
        logger.exception("Failed to check pool/reservation overlap for subnet %s", subnet_id)
        messages.warning(request, check_failed_message)


class _BasePoolAddView(_KeaChangeMixin, generic.ObjectView):
    """Base view for adding a pool to a subnet."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_pool_add.html"
    dhcp_version: Family  # set on subclasses

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
                "tab": self.tab,
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
                    "tab": self.tab,
                },
            )
        pool = form.cleaned_data["pool"]
        try:
            client = server.get_client(version=self.dhcp_version)
        except (ValueError, requests.RequestException):
            logger.exception("Failed to create Kea client for server %s", pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return redirect(return_url)
        # F4: Warn (non-blocking) when any reservation IP falls in the new pool range
        _warn_pool_reservation_overlap(request, server, client, self.dhcp_version, subnet_id, pool)
        try:
            client.pool_add(version=self.dhcp_version, subnet_id=subnet_id, pool=pool)
            messages.success(request, f"Pool {pool} added to subnet {subnet_id}.")
        except PartialPersistError:
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.exception("Failed to add pool to subnet %s", subnet_id)
            messages.error(request, kea_error_hint(exc))
        except requests.RequestException:
            logger.exception("Failed to add pool to subnet %s (network error)", subnet_id)
            messages.error(request, "Network error communicating with Kea: see server logs.")
        except (ValueError, RuntimeError):
            logger.exception("Failed to add pool to subnet %s", subnet_id)
            messages.error(request, "Failed to add pool: see server logs for details.")
        return redirect(return_url)


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
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "pool": pool,
                "subnet_id": subnet_id,
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
        try:
            client = server.get_client(version=self.dhcp_version)
        except (requests.RequestException, ValueError):
            logger.exception("Failed to connect to Kea for pool delete on server %s", pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return redirect(return_url)
        try:
            client.pool_del(version=self.dhcp_version, subnet_id=subnet_id, pool=pool)
            messages.success(request, f"Pool {pool} removed from subnet {subnet_id}.")
        except PartialPersistError:
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.exception("Failed to remove pool from subnet %s", subnet_id)
            messages.error(request, kea_error_hint(exc))
        except requests.RequestException:
            logger.exception("Failed to remove pool from subnet %s (network error)", subnet_id)
            messages.error(request, "Network error communicating with Kea: see server logs.")
        except (ValueError, RuntimeError):
            logger.exception("Failed to remove pool from subnet %s", subnet_id)
            messages.error(request, "Failed to remove pool: see server logs for details.")
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
        for field, value in _subnet_option_fields(snapshot.global_options, snapshot.family).items()
    }
    network = next((network for network in snapshot.shared_networks if network.name == current_network), None)
    if network is not None:
        inherited.update(
            {
                field: {"value": value, "source": f"shared-network: {current_network}"}
                for field, value in _subnet_option_fields(network.options, snapshot.family).items()
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
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "return_url": self._subnets_url(pk),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)

        try:
            client = server.get_client(version=self.dhcp_version)
        except (requests.RequestException, ValueError):
            logger.exception("Failed to get Kea client for server %s", pk)
            messages.error(request, "Unable to connect to the Kea server.")
            form = forms.SubnetAddForm(request.POST)
            form.fields["shared_network"].choices = [("", "— (global pool) —")]
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "tab": self.tab,
                },
            )

        configuration = server_configuration.for_verification(server, self.dhcp_version)
        network_choices = _network_choices(configuration)
        _diagnostic_messages(request, configuration.diagnostics, messages.WARNING)
        if not configuration.shared_networks_complete:
            form = forms.SubnetAddForm(request.POST)
            form.fields["shared_network"].choices = [("", "— (global pool) —")]
            form.fields["shared_network"].disabled = True
            form.add_error(None, "Could not load shared networks from Kea. Please try again.")
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "tab": self.tab,
                },
            )

        form = forms.SubnetAddForm(request.POST)
        form.fields["shared_network"].choices = network_choices
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "tab": self.tab,
                },
            )
        cd = form.cleaned_data
        try:
            assigned_id = client.subnet_add(
                version=self.dhcp_version,
                subnet_cidr=cd["subnet"],
                subnet_id=cd.get("subnet_id") or None,
                pools=cd["pools"],
                gateway=cd["gateway"] or None,
                dns_servers=cd["dns_servers"],
                ntp_servers=cd["ntp_servers"],
                ddns_qualifying_suffix=cd.get("ddns_qualifying_suffix") or None,
            )
            messages.success(request, f"Subnet {cd['subnet']} added.")
            shared_network = cd.get("shared_network", "")
            if shared_network and assigned_id is not None:
                try:
                    client.network_subnet_add(version=self.dhcp_version, name=shared_network, subnet_id=assigned_id)
                    messages.success(request, f"Subnet assigned to shared network '{shared_network}'.")
                except PartialPersistError:
                    messages.warning(
                        request,
                        f"Subnet assigned to '{shared_network}' but config-write failed (change may not survive restart).",
                    )
                except (KeaException, requests.RequestException, ValueError):
                    logger.exception(
                        "Subnet %s created but failed to assign to network %s", cd["subnet"], shared_network
                    )
                    messages.warning(request, f"Subnet created but could not be assigned to '{shared_network}'.")
            elif shared_network:
                logger.warning(
                    "Subnet %s added but no ID returned — cannot assign to network %s", cd.get("subnet"), shared_network
                )
                messages.warning(
                    request,
                    f"Subnet added but no ID was returned by Kea; could not assign to '{shared_network}'.",
                )
        except PartialPersistError as exc:
            messages.warning(request, "Subnet added but config-write failed (change may not survive a Kea restart).")
            # The subnet is live; attempt network assignment if we have the ID.
            partial_id = getattr(exc, "subnet_id", None)
            shared_network = cd.get("shared_network", "")
            if shared_network and partial_id is not None:
                try:
                    client.network_subnet_add(version=self.dhcp_version, name=shared_network, subnet_id=partial_id)
                    messages.success(request, f"Subnet assigned to shared network '{shared_network}'.")
                except PartialPersistError:
                    messages.warning(
                        request,
                        f"Subnet assigned to '{shared_network}' but config-write failed (change may not survive restart).",
                    )
                except (KeaException, requests.RequestException, ValueError):
                    logger.exception(
                        "Partially-persisted subnet %s could not be assigned to network %s", partial_id, shared_network
                    )
                    messages.warning(request, f"Could not assign subnet to '{shared_network}'.")
            return redirect(return_url)
        except KeaException as exc:
            logger.exception("Failed to add subnet %s", cd.get("subnet"))
            messages.error(request, kea_error_hint(exc))
        except requests.RequestException:
            logger.exception("Failed to add subnet %s (network error)", cd.get("subnet"))
            messages.error(request, "Network error communicating with Kea: see server logs.")
            return render(
                request,
                self.template_name,
                {
                    "object": server,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "tab": self.tab,
                },
            )
        except ValueError:
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
                    "tab": self.tab,
                },
            )
        return redirect(return_url)


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
        subnet = snapshot.find_by_id(subnet_id)
        configuration = server_configuration.for_verification(server, self.dhcp_version)
        configured_target = configuration.subnet_with_membership(subnet_id)
        subnet_configuration = None
        subnet_cidr = ""
        display_network = subnet.shared_network.name if subnet and subnet.shared_network else ""
        if subnet is not None and configured_target is not None and configured_target.complete:
            subnet_configuration = configured_target.configuration
            subnet_cidr = configured_target.declared_cidr
            display_network = configured_target.shared_network_name or ""
        if subnet_configuration is None:
            declaration = server_configuration.subnet_for_display(server, self.dhcp_version, subnet_id)
            if declaration is not None:
                subnet_configuration = declaration.configuration
                subnet_cidr = declaration.declared_cidr
        if subnet_configuration is None:
            _diagnostic_messages(request, snapshot.diagnostics, messages.ERROR)
            messages.error(request, "Could not load subnet configuration from Kea.")
            return redirect(self._subnets_url(pk))
        _diagnostic_messages(
            request,
            snapshot.diagnostics + configuration.diagnostics,
            messages.WARNING if configuration.available else messages.ERROR,
        )
        if not configuration.available or configured_target is None:
            messages.warning(request, "Could not load shared-network data; network assignment may be inaccurate.")
        settings = subnet_configuration.settings
        initial = {
            "subnet_cidr": subnet_cidr,
            "pools": "\n".join(pool.range for pool in subnet_configuration.pools),
            **_subnet_option_fields(subnet_configuration.options, self.dhcp_version),
            "valid_lft": settings.valid_lifetime,
            "min_valid_lft": settings.min_valid_lifetime,
            "max_valid_lft": settings.max_valid_lifetime,
            "renew_timer": settings.renew_timer,
            "rebind_timer": settings.rebind_timer,
            "ddns_qualifying_suffix": settings.ddns_qualifying_suffix or "",
            "shared_network": display_network,
            "current_network": display_network,
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

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:  # noqa: C901
        server = self.get_object(pk=pk)
        return_url = self._subnets_url(pk)
        try:
            client = server.get_client(version=self.dhcp_version)
        except (requests.RequestException, ValueError):
            logger.exception("Failed to get Kea client for server %s (subnet edit POST)", pk)
            messages.error(request, "Unable to connect to the Kea server.")
            return redirect(return_url)
        configuration = server_configuration.for_verification(server, self.dhcp_version)
        network_choices = _network_choices(configuration)
        _diagnostic_messages(request, configuration.diagnostics, messages.WARNING)
        if not configuration.available or not configuration.shared_networks_complete:
            logger.warning(
                "Could not determine current shared-network for subnet %s on server %s — aborting edit", subnet_id, pk
            )
            messages.error(request, "Could not determine current network state; edit aborted to prevent data loss.")
            return redirect(return_url)
        declaration = configuration.subnet_with_membership(subnet_id)
        if declaration is None:
            messages.error(request, "Could not determine current network state; edit aborted to prevent data loss.")
            return redirect(return_url)
        server_current_network = declaration.shared_network_name or ""
        form = forms.SubnetEditForm(request.POST)
        form.fields["shared_network"].choices = network_choices
        if not form.is_valid():
            display_network = form.data.get("shared_network", server_current_network or "")
            initial = {k: v for k, v in form.data.items() if k in form.fields}
            inherited_options = _inherited_subnet_options(configuration, display_network, initial)
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
                    "inherited_options": inherited_options,
                    "tab": self.tab,
                },
            )
        cd = form.cleaned_data
        # Use the authoritative server-side value so a user cannot forge current_network
        # via POST data to remove a subnet from a network it doesn't actually belong to.
        old_network = server_current_network
        new_network = cd.get("shared_network", "")

        # Pre-compute inherited_options for error branches that re-render the form.
        display_network = form.data.get("shared_network", server_current_network or "")
        initial = {k: v for k, v in form.data.items() if k in form.fields}
        inherited_options = _inherited_subnet_options(configuration, display_network, initial)

        # Apply subnet config changes first — only move the network if the update succeeds.
        try:
            client.subnet_update(
                version=self.dhcp_version,
                subnet_id=subnet_id,
                subnet_cidr=cd["subnet_cidr"],
                pools=cd["pools"],
                gateway=cd["gateway"] or None,
                dns_servers=cd["dns_servers"] or None,
                ntp_servers=cd["ntp_servers"] or None,
                ddns_qualifying_suffix=cd.get("ddns_qualifying_suffix"),
                valid_lft=cd.get("valid_lft"),
                min_valid_lft=cd.get("min_valid_lft"),
                max_valid_lft=cd.get("max_valid_lft"),
                renew_timer=cd.get("renew_timer"),
                rebind_timer=cd.get("rebind_timer"),
            )
            messages.success(request, f"Subnet {cd['subnet_cidr']} updated.")
        except PartialPersistError:
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.exception("Failed to update subnet %s on server %s", subnet_id, pk)
            messages.error(request, kea_error_hint(exc))
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
                    "inherited_options": inherited_options,
                    "tab": self.tab,
                },
            )
        except requests.RequestException:
            logger.exception("Failed to update subnet %s on server %s (network error)", subnet_id, pk)
            messages.error(request, "Network error communicating with Kea: see server logs.")
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
                    "inherited_options": inherited_options,
                    "tab": self.tab,
                },
            )
        except (ValueError, RuntimeError):
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
                    "inherited_options": inherited_options,
                    "tab": self.tab,
                },
            )

        # Handle shared-network membership change only after a successful update.
        if old_network != new_network:
            add_partial_error: PartialPersistError | None = None
            try:
                if new_network:
                    try:
                        client.network_subnet_add(version=self.dhcp_version, name=new_network, subnet_id=subnet_id)
                    except PartialPersistError as exc:
                        # add is live but config-write failed; continue to attempt del, then re-raise
                        add_partial_error = exc
                if old_network:
                    try:
                        client.network_subnet_del(version=self.dhcp_version, name=old_network, subnet_id=subnet_id)
                    except (KeaException, PartialPersistError, requests.RequestException, ValueError) as del_exc:
                        # add succeeded but del failed — only rollback if mutation is NOT already live
                        if isinstance(del_exc, PartialPersistError):
                            # del is live (running config changed); do not rollback
                            raise
                        if isinstance(del_exc, KeaException) and new_network:
                            # Kea definitively rejected the del — safe to rollback the add
                            try:
                                client.network_subnet_del(
                                    version=self.dhcp_version, name=new_network, subnet_id=subnet_id
                                )
                            except (KeaException, requests.RequestException, ValueError):
                                logger.exception(
                                    "Rollback of network_subnet_add failed for subnet %s on server %s",
                                    subnet_id,
                                    pk,
                                )
                        elif not isinstance(del_exc, KeaException):
                            # Transport/parse error — state is ambiguous, do NOT rollback
                            logger.warning(
                                "network_subnet_del for subnet %s on server %s failed with ambiguous error; "
                                "skipping rollback to avoid inconsistent state",
                                subnet_id,
                                pk,
                                exc_info=True,
                            )
                        raise
                if add_partial_error is not None:
                    raise add_partial_error
            except PartialPersistError as exc:
                logger.warning(
                    "network_subnet_add applied but config-write failed for subnet %s on server %s: %s",
                    subnet_id,
                    pk,
                    exc,
                )
                messages.warning(
                    request,
                    "Network assignment may have applied to the running config but could not be persisted. "
                    "Check Kea logs and reapply if needed.",
                )
            except KeaException as exc:
                logger.warning("network_subnet change failed for subnet %s on server %s: %s", subnet_id, pk, exc)
                messages.error(request, f"Network assignment error: {kea_error_hint(exc)}")
            except requests.RequestException:
                logger.exception("Transport error changing network for subnet %s on server %s", subnet_id, pk)
                messages.error(request, "Transport error communicating with Kea during network assignment.")
            except ValueError:
                logger.exception("Unexpected error changing network for subnet %s on server %s", subnet_id, pk)
                messages.error(request, "An internal error occurred during network assignment.")
        return redirect(return_url)


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
            logger.exception("Failed to connect to Kea for subnet delete on server %s", pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return redirect(return_url)
        try:
            client.subnet_del(version=self.dhcp_version, subnet_id=subnet_id)
            messages.success(request, f"Subnet {subnet_id} deleted.")
        except PartialPersistError:
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.exception("Failed to delete subnet %s", subnet_id)
            messages.error(request, kea_error_hint(exc))
        except requests.RequestException:
            logger.exception("Failed to delete subnet %s (network error)", subnet_id)
            messages.error(request, "Network error communicating with Kea: see server logs.")
        except ValueError:
            logger.exception("Failed to delete subnet %s", subnet_id)
            messages.error(request, "Failed to delete subnet: see server logs for details.")
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
            if _unsupported_command(exc):
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
