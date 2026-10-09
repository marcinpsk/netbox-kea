# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
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

from .. import server_configuration
from ..constants import Family
from ..dhcp_mapping_lifecycle import MetadataBusy
from ..integrations import dhcp_plugin
from ..ipam_reconciliation import complete_import_observation
from ..kea import KeaCommand, KeaException, KeaResponse
from ..mappers.kea_to_dhcp import parse_dhcp_config
from ..models import Server
from ..utilities import OptionalViewTab

logger = logging.getLogger(__name__)


def _tab_enabled(server: Server) -> bool:
    """Show the DHCP-plugin tab only when the plugin is installed and the server opts in."""
    return dhcp_plugin.is_available() and server.sync_dhcp_plugin_enabled


def _enabled_versions(server: Server) -> list[Family]:
    """Return the DHCP protocol versions enabled on *server* (4 and/or 6)."""
    versions: list[Family] = []
    if server.dhcp4:
        versions.append(4)
    if server.dhcp6:
        versions.append(6)
    return versions


def _extract_dhcp_conf(resp: list[KeaResponse], version: Family) -> dict | None:
    """Pull the ``Dhcp4``/``Dhcp6`` block out of a checked ``config-get`` reply, or ``None`` when it is absent.

    Raises ``RuntimeError`` when ``arguments`` is not a mapping.
    """
    args = resp[0].get("arguments") or {}
    if not isinstance(args, dict):
        raise RuntimeError("Malformed Kea config-get response: 'arguments' must be a dict")
    conf = args.get(f"Dhcp{version}")
    return conf if isinstance(conf, dict) else None


def _fetch_config_intent(server: Server, version: Family):
    """Read live ``config-get`` for one version and parse it to intent (read-only).

    ``config-get`` is issued exactly once here. Reservations use their separate
    typed Snapshot read. Returns the :class:`ServerConfigIntent`, or ``None`` if
    Kea could not be read.
    """
    try:
        client = server.get_client(version=version)
        resp = client.command(KeaCommand.CONFIG_GET, version)
        conf = _extract_dhcp_conf(resp, version)
        if conf is None:
            return None
        intent = parse_dhcp_config(conf, version)
        observed = server_configuration.observed_snapshot(server, version, conf)
        intent.configuration_complete = not observed.subnet_diagnostics
    except (KeaException, requests.RequestException, ValueError, RuntimeError):
        logger.warning("DHCP-plugin sync: config-get failed for %s (v%s)", server.name, version, exc_info=True)
        return None
    else:
        return intent


def _fetch_reservation_snapshot(server: Server, version: Family):
    """Return a typed Reservation Snapshot, possibly incomplete, or ``None`` after a read failure."""
    from ..ipam_reconciliation import ReservationObservation
    from ..models import next_confirmation_number
    from ..subnet_catalogue import for_synchronization

    cutoff = next_confirmation_number()
    try:
        client = server.get_client(version=version)
        catalogue = for_synchronization(server, version)
        return ReservationObservation(client.reservation_snapshot(version, catalogue), cutoff)
    except (KeaException, requests.RequestException, RuntimeError, ValueError):
        logger.warning(
            "DHCP-plugin sync: Reservation Snapshot failed for %s (v%s)", server.name, version, exc_info=True
        )
        return None


def _summary_problems(summary) -> list[str]:
    """Return one note per non-zero problem count in *summary*.

    Every note is reported together: an if/elif chain hid the later counts whenever
    an earlier one was set, so one import could look cleaner than it was.
    """
    problems = []
    if summary.reservations_unread:
        problems.append(
            "The Reservation Snapshot could not be read in full. "
            "Check Snapshot diagnostics and logs for read or validation errors. Reservation counts may be incomplete."
        )
    if summary.reservations_quarantined:
        problems.append(f"{summary.reservations_quarantined} malformed reservation(s) were quarantined.")
    if summary.reservations_skipped:
        problems.append(f"{summary.reservations_skipped} reservation(s) were skipped after they were read.")
    if summary.foreign_addresses_skipped:
        problems.append(
            f"{summary.foreign_addresses_skipped} manually curated NetBox IP(s) were left unchanged. "
            "Use the per-reservation Sync to claim one."
        )
    if summary.owner_disagreements:
        problems.append(f"{summary.owner_disagreements} IPAM owner disagreement(s) left the shared objects unchanged.")
    if summary.addresses_unattached:
        problems.append(
            f"{summary.addresses_unattached} reserved address(es) were not attached because no NetBox IP "
            "holds them. A Global Reservation has no Subnet to size an address from."
        )
    if summary.errors:
        problems.append(f"{summary.errors} errors occurred. See the logs.")
    return problems


def run_dhcp_plugin_import(server: Server) -> list[tuple[Family, dhcp_plugin.ImportSummary]]:
    """Import every enabled version's live Kea config into the DHCP plugin.

    Returns a list of ``(version, ImportSummary)`` for the versions that were read.
    """
    results: list[tuple[Family, dhcp_plugin.ImportSummary]] = []
    for version in _enabled_versions(server):
        intent = _fetch_config_intent(server, version)
        if intent is None:
            continue
        snapshot = _fetch_reservation_snapshot(server, version)
        results.append((version, dhcp_plugin.import_server_config(server, intent, snapshot)))
    complete_import_observation(server, {family: summary.ownership for family, summary in results})
    return results


def compute_drift(server: Server) -> dict:
    """Compare live Kea subnets against the imported DHCP-plugin records.

    Returns ``{"versions": [...], "kea_unreachable": bool}`` where each version
    entry lists subnet rows tagged ``imported`` (in both), ``new`` (in Kea, not
    yet imported), or ``orphaned`` (imported, no longer in Kea).
    """
    from ..dhcp_mapping_lifecycle import require_branch_mappings
    from ..models import KeaDhcpLink

    require_branch_mappings()

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
                    "reservations": "Not scanned",
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
        from ..dhcp_mapping_lifecycle import MappingUnavailable

        available = dhcp_plugin.is_available()
        unavailable = None
        drift = None
        if available:
            try:
                drift = compute_drift(instance)
            except MappingUnavailable as error:
                unavailable = str(error)
        return {
            "plugin_available": available,
            "mapping_unavailable": unavailable,
            "drift": drift,
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
        except MetadataBusy as error:
            messages.error(request, error.message)
            return redirect
        except (KeaException, requests.RequestException, ValueError):
            # Expected external-boundary failures (Kea read / validation).
            logger.exception("DHCP-plugin import failed for server %s (Kea read/validation)", server.name)
            messages.error(request, "An internal error occurred during the DHCP-plugin import.")
            return redirect
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
                f"{summary.reservations_updated} updated, "
                f"{summary.options_created} options created, {summary.options_updated} updated, "
                f"{summary.client_classes_created} client classes created, "
                f"{summary.client_classes_updated} updated"
            )
            notes = []
            if summary.option_defs_created:
                notes.append(f"{summary.option_defs_created} custom option definition(s) created")
            if summary.options_skipped:
                notes.append(f"{summary.options_skipped} option(s) skipped (unresolved/invalid)")
            if summary.shared_networks_deferred:
                notes.append(
                    f"{summary.shared_networks_deferred} shared-network subnet(s) imported "
                    "individually (grouping not represented)"
                )
            if notes:
                text += f" ({'; '.join(notes)})"
            problems = _summary_problems(summary)
            if problems:
                messages.warning(request, f"{text}. {' '.join(problems)}")
            else:
                messages.success(request, text + ".")
        return redirect
