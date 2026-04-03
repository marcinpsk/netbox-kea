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

from .. import forms, tables
from ..kea import KeaClient, KeaException, PartialPersistError
from ..models import Server
from ..utilities import (
    OptionalViewTab,
    check_dhcp_enabled,
    export_table,
    kea_error_hint,
)
from ._base import _KeaChangeMixin

logger = logging.getLogger(__name__)

_POOL_RE = re.compile(r"^[0-9a-fA-F.:/-]{3,100}$")


class BaseServerDHCPSubnetsView(generic.ObjectChildrenView):
    """Base view for the subnet list tab; fetches subnet data from Kea config."""

    table = tables.SubnetTable
    queryset = Server.objects.all()
    template_name = "netbox_kea/server_dhcp_subnets.html"

    def get_children(self, request: HttpRequest, parent: Server) -> list[dict[str, Any]]:
        """Return the subnet list for *parent* by delegating to :meth:`get_subnets`."""
        return self.get_subnets(parent, request)

    def _subnet_to_row(
        self,
        s: dict,
        server_pk: int,
        can_change: bool,
        shared_network: str = "",
    ) -> dict[str, Any] | None:
        """Convert a Kea subnet dict to a table row dict, or ``None`` if invalid."""
        from ..utilities import format_option_data

        if not isinstance(s, dict):
            return None
        if "id" not in s or "subnet" not in s:
            return None
        try:
            sort_key = int(ipaddress.ip_network(s["subnet"], strict=False).network_address)
        except (ValueError, TypeError):
            logger.warning("Skipping subnet with malformed CIDR: %s", s.get("subnet"))
            return None
        row: dict[str, Any] = {
            "id": s["id"],
            "subnet": s["subnet"],
            "dhcp_version": self.dhcp_version,
            "server_pk": server_pk,
            "_subnet_sort_key": sort_key,
            "options": format_option_data(s.get("option-data") or [], version=self.dhcp_version),
            "pools": [p.get("pool", "") for p in (s.get("pools") or []) if isinstance(p, dict) and p.get("pool")],
            "can_change": can_change,
        }
        if shared_network:
            row["shared_network"] = shared_network
        return row

    def get_subnets(self, server: Server, request: HttpRequest) -> list[dict[str, Any]]:
        """Fetch all subnets (including shared-network subnets) from the Kea config.

        Also fetches per-subnet utilisation statistics from ``stat-lease{v}-get``
        when the ``stat_cmds`` hook is loaded.  Degrades gracefully when the hook
        is absent.
        """
        from ..utilities import parse_subnet_stats

        try:
            client = server.get_client(version=self.dhcp_version)
            config = client.command("config-get", service=[f"dhcp{self.dhcp_version}"])
            args = config[0]["arguments"] if config and isinstance(config[0], dict) else None
            if not isinstance(args, dict):
                logger.warning(
                    "config-get returned non-dict arguments for dhcp%s on server %s: %r",
                    self.dhcp_version,
                    server.pk,
                    type(args),
                )
                return []
            dhcp_conf = args.get(f"Dhcp{self.dhcp_version}", {})
            if not isinstance(dhcp_conf, dict):
                dhcp_conf = {}
        except (KeaException, requests.RequestException, ValueError, IndexError, KeyError):
            logger.exception("Failed to fetch subnet config for dhcp%s on server %s", self.dhcp_version, server.pk)
            messages.error(request, "Failed to load subnet configuration from Kea.")
            return []
        can_change = Server.objects.restrict(request.user, "change").filter(pk=server.pk).exists()
        subnets = dhcp_conf.get(f"subnet{self.dhcp_version}") or []
        subnet_list = []
        for s in subnets:
            row = self._subnet_to_row(s, server.pk, can_change)
            if row is not None:
                subnet_list.append(row)

        for sn in dhcp_conf.get("shared-networks") or []:
            if not isinstance(sn, dict):
                continue
            for s in sn.get(f"subnet{self.dhcp_version}") or []:
                row = self._subnet_to_row(s, server.pk, can_change, shared_network=sn.get("name", ""))
                if row is not None:
                    subnet_list.append(row)

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
        except (KeaException, requests.RequestException):  # noqa: BLE001
            logger.debug("stat_cmds hook unavailable or failed", exc_info=True)

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
                "tab": self.tab,
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

    tab = OptionalViewTab(label="DHCPv4 Subnets", weight=1020, is_enabled=lambda s: s.dhcp4)
    dhcp_version = 4


# ─────────────────────────────────────────────────────────────────────────────
# Phase 10: Pool management views
# ─────────────────────────────────────────────────────────────────────────────


def _warn_pool_reservation_overlap(
    request: HttpRequest,
    client: "KeaClient",
    version: int,
    subnet_id: int,
    pool_str: str,
) -> None:
    """Add a non-blocking warning if any existing reservation IP falls within *pool_str*.

    Uses ``reservation-get-page`` to iterate all reservations for the subnet
    and checks each one against the pool range (IPRange or CIDR).  Silently
    skips on any error (host_cmds not loaded, network failure, etc.).
    """
    try:
        from netaddr import IPAddress, IPNetwork, IPRange

        if "-" in pool_str and "/" not in pool_str:
            start, end = pool_str.split("-", 1)
            pool_range: IPRange | IPNetwork = IPRange(start.strip(), end.strip())
        else:
            pool_range = IPNetwork(pool_str)

        overlapping: list[str] = []
        source_index, from_index = 0, 0
        while True:
            hosts, from_index, source_index = client.reservation_get_page(
                service=f"dhcp{version}",
                source_index=source_index,
                from_index=from_index,
                limit=200,
            )
            for host in hosts:
                if host.get("subnet-id") != subnet_id:
                    continue
                candidate_ips = list(filter(None, [host.get("ip-address")] + list(host.get("ip-addresses") or [])))
                for ip_str in candidate_ips:
                    try:
                        if IPAddress(ip_str) in pool_range:
                            overlapping.append(ip_str)
                    except Exception:  # noqa: BLE001, PERF203
                        pass
            if from_index == 0 and source_index == 0:
                break

        if overlapping:
            sample = ", ".join(overlapping[:5])
            extra = f" (+{len(overlapping) - 5} more)" if len(overlapping) > 5 else ""
            messages.warning(
                request,
                f"Pool {pool_str} overlaps {len(overlapping)} existing reservation(s): {sample}{extra}. "
                "Kea allows this — reservations take priority over pool allocation.",
            )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to check pool/reservation overlap for subnet %s", subnet_id)


def _warn_reservation_pool_overlap(
    request: HttpRequest,
    client: "KeaClient",
    version: int,
    subnet_id: int,
    ip_str: str,
) -> None:
    """Add a non-blocking warning if *ip_str* falls within an existing pool in *subnet_id*.

    Fetches the subnet configuration via ``subnet{version}-get`` and checks each
    pool entry.  Silently skips on any error.
    """
    try:
        from netaddr import IPAddress, IPNetwork, IPRange

        resp = client.command(
            f"subnet{version}-get",
            service=[f"dhcp{version}"],
            arguments={"id": subnet_id},
        )
        if not resp or not isinstance(resp[0], dict):
            return
        arguments = resp[0].get("arguments")
        if not isinstance(arguments, dict):
            return
        subnet_list = arguments.get(f"subnet{version}", [])
        if not isinstance(subnet_list, list) or not subnet_list:
            return
        subnet = subnet_list[0] if isinstance(subnet_list[0], dict) else {}
        ip = IPAddress(ip_str)

        for pool_entry in subnet.get("pools") or []:
            ps = pool_entry.get("pool", "")
            if not ps:
                continue
            if "-" in ps and "/" not in ps:
                start, end = ps.split("-", 1)
                pool_range: IPRange | IPNetwork = IPRange(start.strip(), end.strip())
            else:
                pool_range = IPNetwork(ps)
            if ip in pool_range:
                messages.warning(
                    request,
                    f"IP {ip_str} is within existing pool {ps}. "
                    "Kea allows this — reservations take priority over pool allocation.",
                )
                break
    except Exception:  # noqa: BLE001
        logger.debug("Failed to check reservation/pool overlap for %s in subnet %s", ip_str, subnet_id)


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
        try:
            client = server.get_client(version=self.dhcp_version)
        except (ValueError, requests.RequestException):
            logger.exception("Failed to create Kea client for server %s", pk)
            messages.error(request, "Failed to connect to Kea: see server logs.")
            return redirect(return_url)
        # F4: Warn (non-blocking) when any reservation IP falls in the new pool range
        _warn_pool_reservation_overlap(request, client, self.dhcp_version, subnet_id, pool)
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
        except (KeaException, requests.RequestException, ValueError):
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
        except (KeaException, requests.RequestException, ValueError):
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

    def _get_network_choices(self, client: "KeaClient") -> list[tuple[str, str]]:
        """Return shared-network name choices for the subnet-add form dropdown.

        Raises:
            KeaException: If Kea returns an error or an unexpected response.
            requests.RequestException: If the Kea server is unreachable.
            ValueError: If the Kea response is structurally invalid.

        """
        resp = client.command("config-get", service=[f"dhcp{self.dhcp_version}"])
        args = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(args, dict):
            raise ValueError(f"config-get returned unexpected arguments: {type(args)}")
        dhcp_conf = args.get(f"Dhcp{self.dhcp_version}", {})
        if not isinstance(dhcp_conf, dict):
            raise ValueError(f"config-get returned unexpected Dhcp{self.dhcp_version} structure: {type(dhcp_conf)}")
        networks = dhcp_conf.get("shared-networks") or []
        if networks and not isinstance(networks, list):
            raise ValueError(f"config-get returned non-list shared-networks: {type(networks)}")
        choices: list[tuple[str, str]] = [("", "— (global pool) —")]
        for sn in networks:
            if not isinstance(sn, dict):
                continue
            name = sn.get("name", "")
            if name:
                choices.append((name, name))
        return choices

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        form = forms.SubnetAddForm()
        try:
            client = server.get_client(version=self.dhcp_version)
            form.fields["shared_network"].choices = self._get_network_choices(client)
        except (KeaException, requests.RequestException, ValueError):
            logger.exception("Failed to load shared networks for subnet add form (server %s)", pk)
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
                {"object": server, "form": form, "dhcp_version": self.dhcp_version, "return_url": return_url},
            )

        try:
            network_choices = self._get_network_choices(client)
        except (KeaException, requests.RequestException, ValueError):
            logger.exception("Failed to load shared networks for server %s", pk)
            form = forms.SubnetAddForm(request.POST)
            form.fields["shared_network"].choices = [("", "— (global pool) —")]
            form.add_error(None, "Could not load shared networks from Kea. Please try again.")
            return render(
                request,
                self.template_name,
                {"object": server, "form": form, "dhcp_version": self.dhcp_version, "return_url": return_url},
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
                },
            )
        except (KeaException, requests.RequestException, ValueError):
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

    def _fetch_subnet(self, server: Server, subnet_id: int) -> dict[str, Any] | None:
        """Fetch current subnet config from Kea.  Returns None on error or if the subnet is not found."""
        try:
            key = f"subnet{self.dhcp_version}"
            client = server.get_client(version=self.dhcp_version)
            resp = client.command(
                f"{key}-get",
                service=[f"dhcp{self.dhcp_version}"],
                arguments={"id": subnet_id},
            )
            if not resp or not isinstance(resp[0], dict):
                return None
            arguments = resp[0].get("arguments")
            if not isinstance(arguments, dict):
                return None
            subnets = arguments.get(key, [])
            if not isinstance(subnets, list) or not subnets:
                return None
            if not isinstance(subnets[0], dict):
                return None
            return subnets[0]
        except (KeaException, requests.RequestException, ValueError):
            logger.warning("Failed to fetch subnet %s for editing", subnet_id)
            return None

    def _get_network_data(self, client: "KeaClient", subnet_id: int) -> tuple[list[tuple[str, str]], str | None, dict]:
        """Return ``(choices, current_network_name, dhcp_conf)`` for the shared-network dropdown.

        ``choices`` is suitable for a ``ChoiceField``: ``[("", "— global pool —"), ("net-a", "net-a"), ...]``.
        ``current_network_name`` is the name of the network the subnet currently belongs to,
        ``""`` for global pool, or ``None`` when the network state could not be determined.
        ``dhcp_conf`` is the raw Dhcp4/Dhcp6 config dict (for deriving inherited options).
        """
        try:
            resp = client.command("config-get", service=[f"dhcp{self.dhcp_version}"])
            args = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
            if not isinstance(args, dict):
                logger.warning("config-get returned unexpected arguments for network data: %r", args)
                return [("", "— (global pool) —")], None, {}
            dhcp_conf = args.get(f"Dhcp{self.dhcp_version}", {})
            if not isinstance(dhcp_conf, dict):
                logger.warning("config-get returned non-dict Dhcp%s config: %r", self.dhcp_version, type(dhcp_conf))
                return [("", "— (global pool) —")], None, {}
            networks = dhcp_conf.get("shared-networks") or []
            if networks and not isinstance(networks, list):
                logger.warning("config-get returned non-list shared-networks for network data: %r", type(networks))
                return [("", "— (global pool) —")], None, {}
        except (KeaException, requests.RequestException, ValueError):
            logger.warning("Failed to fetch shared networks for subnet edit dropdown")
            return [("", "— (global pool) —")], None, {}

        current_network = ""
        choices: list[tuple[str, str]] = [("", "— (global pool) —")]
        for sn in networks:
            if not isinstance(sn, dict):
                continue
            name = sn.get("name", "")
            if not name:
                continue
            choices.append((name, name))
            sn_subnets = sn.get(f"subnet{self.dhcp_version}", [])
            if not isinstance(sn_subnets, list):
                continue
            malformed = False
            for sub in sn_subnets:
                if not isinstance(sub, dict):
                    malformed = True
                    break
            if malformed:
                current_network = None
                break
            try:
                subnet_ids = {sub["id"] for sub in sn_subnets if isinstance(sub.get("id"), (int, str))}
            except (KeyError, TypeError):
                current_network = None
                break
            if len(subnet_ids) != len(sn_subnets):
                current_network = None
                break
            if subnet_id in subnet_ids:
                current_network = name
        return choices, current_network, dhcp_conf

    def _form_initial(self, subnet: dict[str, Any]) -> dict[str, Any]:
        """Build SubnetEditForm initial values from a Kea subnet dict."""
        initial: dict[str, Any] = {"subnet_cidr": subnet.get("subnet", "")}

        # Pools
        pools = subnet.get("pools") or []
        if pools:
            initial["pools"] = "\n".join(p.get("pool", "") for p in pools if isinstance(p, dict) and p.get("pool"))

        # Options
        for opt in subnet.get("option-data") or []:
            if not isinstance(opt, dict):
                continue
            name = opt.get("name", "")
            data = opt.get("data", "")
            if name == "routers":
                initial["gateway"] = data
            elif name in ("domain-name-servers", "dns-servers"):
                initial["dns_servers"] = data
            elif name in ("ntp-servers", "sntp-servers"):
                initial["ntp_servers"] = data

        # Lease lifetimes
        if subnet.get("valid-lft") is not None:
            initial["valid_lft"] = subnet["valid-lft"]
        if subnet.get("min-valid-lft") is not None:
            initial["min_valid_lft"] = subnet["min-valid-lft"]
        if subnet.get("max-valid-lft") is not None:
            initial["max_valid_lft"] = subnet["max-valid-lft"]
        if subnet.get("renew-timer") is not None:
            initial["renew_timer"] = subnet["renew-timer"]
        if subnet.get("rebind-timer") is not None:
            initial["rebind_timer"] = subnet["rebind-timer"]

        return initial

    def _get_inherited_options(
        self,
        dhcp_conf: dict[str, Any],
        current_network: str,
        form_initial: dict[str, Any],
    ) -> dict[str, dict[str, str]]:
        """Return option hints inherited from shared-network or global config.

        Only includes options NOT already set by the subnet itself (i.e., absent
        from *form_initial*).  Each value is a dict with ``"value"`` and
        ``"source"`` keys so the template can display e.g.
        *inherited from global: 8.8.8.8*.
        """

        def _parse_opts(option_list: list) -> dict[str, str]:
            result: dict[str, str] = {}
            for opt in option_list:
                name = opt.get("name", "")
                data = opt.get("data", "")
                if name == "routers":
                    result["gateway"] = data
                elif name in ("domain-name-servers", "dns-servers"):
                    result["dns_servers"] = data
                elif name in ("ntp-servers", "sntp-servers"):
                    result["ntp_servers"] = data
            return result

        global_opts = _parse_opts(dhcp_conf.get("option-data") or [])

        network_opts: dict[str, str] = {}
        if current_network:
            for sn in dhcp_conf.get("shared-networks") or []:
                if sn.get("name") == current_network:
                    network_opts = _parse_opts(sn.get("option-data") or [])
                    break

        inherited: dict[str, dict[str, str]] = {}
        for field in ("gateway", "dns_servers", "ntp_servers"):
            if form_initial.get(field):
                continue  # subnet already overrides this option
            if field in network_opts:
                inherited[field] = {"value": network_opts[field], "source": f"shared-network: {current_network}"}
            elif field in global_opts:
                inherited[field] = {"value": global_opts[field], "source": "global"}
        return inherited

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        subnet = self._fetch_subnet(server, subnet_id)
        if subnet is None:
            messages.error(request, "Could not load subnet configuration from Kea.")
            return redirect(self._subnets_url(pk))
        try:
            client = server.get_client(version=self.dhcp_version)
        except (requests.RequestException, ValueError):
            logger.exception("Failed to get Kea client for server %s (subnet edit GET)", pk)
            messages.error(request, "Unable to connect to the Kea server.")
            return redirect(self._subnets_url(pk))
        network_choices, current_network, dhcp_conf = self._get_network_data(client, subnet_id)
        if current_network is None:
            logger.warning(
                "Could not determine current shared-network for subnet %s on server %s — rendering without network data",
                subnet_id,
                pk,
            )
            messages.warning(request, "Could not load shared-network data; network assignment may be inaccurate.")
        display_network = current_network or ""
        initial = self._form_initial(subnet)
        initial["shared_network"] = display_network
        initial["current_network"] = display_network
        form = forms.SubnetEditForm(initial=initial)
        form.fields["shared_network"].choices = network_choices
        inherited_options = (
            self._get_inherited_options(dhcp_conf, display_network, initial) if current_network is not None else {}
        )
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
                "inherited_options": inherited_options,
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
        network_choices, server_current_network, dhcp_conf = self._get_network_data(client, subnet_id)
        if server_current_network is None:
            logger.warning(
                "Could not determine current shared-network for subnet %s on server %s — aborting edit", subnet_id, pk
            )
            messages.error(request, "Could not determine current network state; edit aborted to prevent data loss.")
            return redirect(return_url)
        form = forms.SubnetEditForm(request.POST)
        form.fields["shared_network"].choices = network_choices
        if not form.is_valid():
            display_network = (
                form.data["shared_network"] if "shared_network" in form.data else (server_current_network or "")
            )
            initial = {k: v for k, v in form.data.items() if k in form.fields}
            inherited_options = (
                self._get_inherited_options(dhcp_conf, display_network, initial)
                if server_current_network is not None
                else {}
            )
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
                },
            )
        cd = form.cleaned_data
        # Use the authoritative server-side value so a user cannot forge current_network
        # via POST data to remove a subnet from a network it doesn't actually belong to.
        old_network = server_current_network
        new_network = cd.get("shared_network", "")

        # Pre-compute inherited_options for error branches that re-render the form.
        display_network = (
            form.data["shared_network"] if "shared_network" in form.data else (server_current_network or "")
        )
        initial = {k: v for k, v in form.data.items() if k in form.fields}
        inherited_options = (
            self._get_inherited_options(dhcp_conf, display_network, initial)
            if server_current_network is not None
            else {}
        )

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
                },
            )
        except (KeaException, requests.RequestException, ValueError):
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
                            raise del_exc
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
                        raise del_exc
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
            except (KeaException, requests.RequestException, ValueError):
                logger.exception("Unexpected error changing network for subnet %s on server %s", subnet_id, pk)
                messages.error(request, "An internal error occurred during network assignment.")
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
        subnet_cidr = ""
        try:
            client = server.get_client(version=self.dhcp_version)
            resp = client.command(
                f"subnet{self.dhcp_version}-get",
                service=[f"dhcp{self.dhcp_version}"],
                arguments={"id": subnet_id},
            )
            key = f"subnet{self.dhcp_version}"
            if resp and isinstance(resp[0], dict):
                arguments = resp[0].get("arguments")
                if isinstance(arguments, dict):
                    subnets = arguments.get(key, [])
                    if isinstance(subnets, list) and subnets and isinstance(subnets[0], dict):
                        subnet_cidr = subnets[0].get("subnet", "")
        except (KeaException, requests.RequestException, ValueError):
            logger.debug("Could not resolve subnet CIDR for subnet %s on server %s", subnet_id, pk)
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
        except (KeaException, requests.RequestException, ValueError):
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
        subnet_cidr = ""
        try:
            client = server.get_client(version=self.dhcp_version)
            resp = client.command(
                f"subnet{self.dhcp_version}-get",
                service=[f"dhcp{self.dhcp_version}"],
                arguments={"id": subnet_id},
            )
            key = f"subnet{self.dhcp_version}"
            if resp and isinstance(resp[0], dict):
                arguments = resp[0].get("arguments")
                if isinstance(arguments, dict):
                    subnets = arguments.get(key, [])
                    if isinstance(subnets, list) and subnets and isinstance(subnets[0], dict):
                        subnet_cidr = subnets[0].get("subnet", "")
        except (KeaException, requests.RequestException, ValueError):
            logger.debug("CIDR lookup failed in wipe GET for subnet %s on server %s", subnet_id, pk)
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
            if isinstance(exc.response, dict) and exc.response.get("result") == 2:
                messages.error(
                    request,
                    "Failed to wipe leases: ensure the lease_cmds hook is loaded.",
                )
            else:
                messages.error(request, kea_error_hint(exc))
        except requests.RequestException:
            logger.exception("Failed to wipe leases in subnet %s (network error)", subnet_id)
            messages.error(request, "Network error communicating with Kea: see server logs.")
        except (KeaException, requests.RequestException, ValueError):
            logger.exception("Failed to wipe leases in subnet %s", subnet_id)
            messages.error(request, "Failed to wipe leases: see server logs for details.")
        return redirect(return_url)


class ServerSubnet4WipeView(_BaseSubnetWipeView):
    """Wipe all DHCPv4 leases in a subnet."""

    dhcp_version = 4


class ServerSubnet6WipeView(_BaseSubnetWipeView):
    """Wipe all DHCPv6 leases in a subnet."""

    dhcp_version = 6
