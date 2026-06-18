# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Views for the optional "Sync to DHCP plugin" integration (import + drift, v1).

These are inert unless the NetBox DHCP plugin (``netbox_dhcp``) is installed and
the server has ``sync_dhcp_plugin_enabled`` set: the per-server tab hides itself
and the sync action refuses.  All reads against Kea are read-only (``config-get``).
"""

from __future__ import annotations

import logging

import requests
from django.contrib import messages
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from netbox.views import generic
from utilities.views import register_model_view

from ..integrations import dhcp_plugin
from ..kea import KeaException
from ..mappers.kea_to_dhcp import parse_dhcp_config
from ..models import Server
from ..utilities import OptionalViewTab

logger = logging.getLogger(__name__)


def _tab_enabled(server: Server) -> bool:
    """Show the DHCP-plugin tab only when the plugin is installed and the server opts in."""
    return dhcp_plugin.is_available() and server.sync_dhcp_plugin_enabled


def _enabled_versions(server: Server) -> list[int]:
    """Return the DHCP protocol versions enabled on *server* (4 and/or 6)."""
    versions = []
    if server.dhcp4:
        versions.append(4)
    if server.dhcp6:
        versions.append(6)
    return versions


def _extract_dhcp_conf(resp, version: int) -> dict | None:
    """Pull the ``Dhcp4``/``Dhcp6`` block out of a ``config-get`` response, or ``None``."""
    dhcp_key = f"Dhcp{version}"
    if not isinstance(resp, list) or not resp or not isinstance(resp[0], dict):
        return None
    if resp[0].get("result") != 0:
        return None
    args = resp[0].get("arguments") or {}
    conf = args.get(dhcp_key) if isinstance(args, dict) else None
    return conf if isinstance(conf, dict) else None


def _fetch_config_intent(server: Server, version: int):
    """Read live ``config-get`` for one version and parse it to intent (read-only).

    Returns the :class:`ServerConfigIntent`, or ``None`` if Kea could not be read.
    """
    try:
        client = server.get_client(version=version)
        resp = client.command("config-get", service=[f"dhcp{version}"])
    except (KeaException, requests.RequestException, ValueError):
        logger.warning("DHCP-plugin sync: config-get failed for %s (v%s)", server.name, version, exc_info=True)
        return None
    conf = _extract_dhcp_conf(resp, version)
    if conf is None:
        return None
    return parse_dhcp_config(conf, version)


def run_dhcp_plugin_import(server: Server) -> list[tuple[int, object]]:
    """Import every enabled version's live Kea config into the DHCP plugin.

    Returns a list of ``(version, ImportSummary)`` for the versions that were read.
    """
    results: list[tuple[int, object]] = []
    for version in _enabled_versions(server):
        intent = _fetch_config_intent(server, version)
        if intent is None:
            continue
        results.append((version, dhcp_plugin.import_server_config(server, intent)))
    return results


def compute_drift(server: Server) -> dict:
    """Compare live Kea subnets against the imported DHCP-plugin records.

    Returns ``{"versions": [...], "kea_unreachable": bool}`` where each version
    entry lists subnet rows tagged ``imported`` (in both), ``new`` (in Kea, not
    yet imported), or ``orphaned`` (imported, no longer in Kea).
    """
    from ..models import KeaDhcpLink

    versions = []
    kea_unreachable = False
    for version in _enabled_versions(server):
        intent = _fetch_config_intent(server, version)
        links = {
            link.kea_subnet_id: link
            for link in KeaDhcpLink.objects.filter(server=server, family=version, kea_subnet_id__isnull=False)
        }
        rows = []
        if intent is None:
            kea_unreachable = True
            # Without a live read we can still list what was imported before.
            for sid, link in sorted(links.items()):
                rows.append(
                    {
                        "kea_subnet_id": sid,
                        "cidr": _link_cidr(link),
                        "status": "unknown",
                        "pools": "—",
                        "reservations": "—",
                    }
                )
            versions.append({"version": version, "rows": rows, "live": False})
            continue

        live_ids = set()
        for subnet in intent.subnets:
            live_ids.add(subnet.kea_subnet_id)
            rows.append(
                {
                    "kea_subnet_id": subnet.kea_subnet_id,
                    "cidr": subnet.cidr,
                    "status": "imported" if subnet.kea_subnet_id in links else "new",
                    "pools": len(subnet.pools),
                    "reservations": len(subnet.reservations),
                }
            )
        for sid, link in sorted(links.items()):
            if sid not in live_ids:
                rows.append(
                    {
                        "kea_subnet_id": sid,
                        "cidr": _link_cidr(link),
                        "status": "orphaned",
                        "pools": "—",
                        "reservations": "—",
                    }
                )
        versions.append({"version": version, "rows": rows, "live": True})

    return {"versions": versions, "kea_unreachable": kea_unreachable}


def _link_cidr(link) -> str:
    """Best-effort CIDR for a link's imported subnet (the DHCP-plugin prefix)."""
    obj = link.sys4_object
    prefix = getattr(obj, "prefix", None)
    return str(getattr(prefix, "prefix", "")) if prefix is not None else ""


_DHCP_PLUGIN_TAB = OptionalViewTab(label="DHCP Plugin", weight=1060, is_enabled=_tab_enabled)


@register_model_view(Server, "dhcp_plugin")
class ServerDhcpPluginView(generic.ObjectView):
    """Per-server tab: import status + drift between live Kea and DHCP-plugin records."""

    queryset = Server.objects.all()
    tab = _DHCP_PLUGIN_TAB
    template_name = "netbox_kea/server_dhcp_plugin.html"

    def get_extra_context(self, request, instance):
        """Return drift context for the template (live, read-only Kea read)."""
        return {
            "plugin_available": dhcp_plugin.is_available(),
            "drift": compute_drift(instance) if dhcp_plugin.is_available() else None,
            "can_sync": _user_can_sync(request.user, instance),
        }


def _user_can_sync(user, server: Server) -> bool:
    """Sync requires server change + IPAM add/change (the DHCP-plugin rows share IPAM)."""
    return (
        user.has_perm("netbox_kea.change_server")
        and user.has_perm("ipam.add_ipaddress")
        and user.has_perm("ipam.change_ipaddress")
        and Server.objects.restrict(user, "change").filter(pk=server.pk).exists()
    )


class ServerDhcpPluginSyncNowView(View):
    """POST-only: import this server's live Kea data-tier config into the DHCP plugin."""

    def post(self, request, pk):
        """Run the import and report a per-version summary."""
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        redirect = HttpResponseRedirect(reverse("plugins:netbox_kea:server_dhcp_plugin", args=[pk]))

        if not dhcp_plugin.is_available():
            messages.error(request, "The NetBox DHCP plugin (netbox_dhcp) is not installed.")
            return redirect
        if not server.sync_dhcp_plugin_enabled:
            messages.error(request, "Enable 'Sync to DHCP plugin' on this server first.")
            return redirect
        if not _user_can_sync(request.user, server):
            return HttpResponseForbidden("You do not have permission to sync to the DHCP plugin.")

        try:
            results = run_dhcp_plugin_import(server)
        except Exception:
            logger.exception("DHCP-plugin import failed for server %s", server.name)
            messages.error(request, "An internal error occurred during the DHCP-plugin import.")
            return redirect

        if not results:
            messages.warning(request, "No Kea configuration could be read (is the server reachable?).")
            return redirect

        for version, summary in results:
            text = (
                f"DHCPv{version}: {summary.subnets_created} subnets created, "
                f"{summary.subnets_updated} updated, {summary.pools_created} pools, "
                f"{summary.reservations_created} reservations created, "
                f"{summary.reservations_updated} updated"
            )
            deferred = []
            if summary.shared_networks_deferred:
                deferred.append(
                    f"{summary.shared_networks_deferred} shared-network subnet(s) imported "
                    "individually (grouping not represented)"
                )
            if summary.options_deferred:
                deferred.append(f"{summary.options_deferred} options skipped")
            if deferred:
                text += f" ({'; '.join(deferred)})"
            if summary.errors:
                messages.warning(request, f"{text} — {summary.errors} errors (see logs).")
            else:
                messages.success(request, text + ".")
        return redirect
