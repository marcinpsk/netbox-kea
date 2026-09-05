import logging
from typing import Any

import requests
from django.http.request import HttpRequest
from django.urls import reverse
from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from .. import forms, tables
from ..filtersets import ServerFilterSet
from ..kea import KeaClient, KeaException, KeaResponse
from ..models import Server
from ..utilities import (
    format_duration,
)

logger = logging.getLogger(__name__)


def _response_arguments(response: list[KeaResponse], command: str, *, require_nonempty: bool = False) -> dict:
    """Return one validated Kea arguments mapping."""
    if not response or not isinstance(response[0], dict):
        raise RuntimeError(f"Kea {command} returned an invalid response entry")
    arguments = response[0].get("arguments")
    if not isinstance(arguments, dict) or (require_nonempty and not arguments):
        raise RuntimeError(f"Kea {command} returned invalid arguments")
    return arguments


def _status_duration(arguments: dict[str, Any], field: str) -> str:
    """Format one non-negative integer duration from a Kea status response."""
    value = arguments.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"Kea status-get returned an invalid {field}")
    formatted = format_duration(value)
    if formatted is None:
        raise RuntimeError(f"Kea status-get could not format {field}")
    return formatted


def _get_global_options(server: "Server") -> dict[str, dict[str, str]]:
    """Return parsed global DHCP option-data for each enabled DHCP version.

    Calls ``config-get`` on each enabled DHCP service and extracts the
    top-level ``option-data`` block.  Any per-service failure is logged and
    skipped so the status page always renders.

    Args:
        server: The Kea :class:`Server` to query.

    Returns:
        A ``{"DHCPv4": {field_name: value}, "DHCPv6": {...}}`` dict containing
        only the versions that returned valid options.

    """
    from ..utilities import format_option_data

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
            args = _response_arguments(resp, "config-get")
            dhcp_block = args.get(dhcp_key, {})
            if not isinstance(dhcp_block, dict):
                raise RuntimeError(f"Kea config-get returned an invalid {dhcp_key} block")
            option_data = dhcp_block.get("option-data", [])
            if not isinstance(option_data, list) or any(not isinstance(option, dict) for option in option_data):
                raise RuntimeError("Kea config-get returned invalid option-data")
            opts = format_option_data(option_data, version=version)
            if opts:
                # Convert snake_case keys to "Title Case" for display
                result[label] = {k.replace("_", " ").title(): v for k, v in opts.items()}
        except KeaException:  # noqa: PERF203
            logger.debug("config-get failed for %s (%s) — skipping global options", label, svc)
        except (requests.RequestException, RuntimeError, ValueError):
            logger.warning("Unexpected error fetching global options for %s (%s)", label, svc, exc_info=True)
    return result


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
        args = _response_arguments(status, "status-get", require_nonempty=True)

        version = client.command("version-get")
        version_args = _response_arguments(version, "version-get", require_nonempty=True)

        return {
            "PID": args.get("pid"),
            "Uptime": _status_duration(args, "uptime"),
            "Time since reload": _status_duration(args, "reload"),
            "Version": version_args.get("extended", "unknown"),
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
            try:
                version = int(svc[-1])
                svc_client = server.get_client(version=version)
                status = svc_client.command("status-get", service=[svc])
                version_resp = svc_client.command("version-get", service=[svc])

                args = _response_arguments(status, f"status-get for service {svc}")
                version_args = _response_arguments(version_resp, f"version-get for service {svc}")

                entry: dict[str, Any] = {
                    "PID": args.get("pid"),
                    "Uptime": _status_duration(args, "uptime") if "uptime" in args else "",
                    "Time since reload": _status_duration(args, "reload") if "reload" in args else "",
                    "Version": version_args.get("extended"),
                }

                if (
                    (ha := args.get("high-availability"))
                    and isinstance(ha, list)
                    and len(ha) > 0
                    and isinstance(ha[0], dict)
                ):
                    # https://kea.readthedocs.io/en/latest/arm/hooks.html#load-balancing-configuration
                    # Note that while the top-level parameter high-availability is a list,
                    # only a single entry is currently supported.
                    ha_servers = ha[0].get("ha-servers") or {}
                    if not isinstance(ha_servers, dict):
                        ha_servers = {}
                    ha_local = ha_servers.get("local") or {}
                    ha_remote = ha_servers.get("remote") or {}
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
            except (KeaException, requests.RequestException, ValueError, RuntimeError):  # noqa: PERF203
                logger.exception("Failed to fetch status for DHCP service %s on server %s", svc, server.pk)
        return resp

    def _get_statuses(self, server: Server) -> dict[str, dict[str, Any]]:
        """Return combined status dicts for CA (when present) and all enabled DHCP services."""
        result: dict[str, dict[str, Any]] = {}
        if server.has_control_agent:
            try:
                # Control Agent is version-agnostic (top-level CA endpoint, not dhcp4/dhcp6).
                ca_client = server.get_client()  # nosemgrep: kea-get-client-missing-version
                result["Control Agent"] = self._get_ca_status(ca_client)
            except (KeaException, requests.RequestException, ValueError, RuntimeError):
                logger.exception("Failed to fetch Control Agent status for server %s", server.pk)
        result.update(self._get_dhcp_status(server))
        return result

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Fetch live status and global options from Kea and expose them to the template."""
        try:
            statuses = self._get_statuses(instance)
        except (KeaException, requests.RequestException, ValueError, RuntimeError):
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

        # Build the services list from the configured service URLs (config-driven),
        # not from live status data. This ensures enable/disable buttons always render
        # even when the status fetch fails.
        services = [
            {
                "name": name,
                "status_data": statuses.get(name, {}),
                **urls,
            }
            for name, urls in service_urls.items()
        ]
        # Prepend Control Agent row if configured (no enable/disable URLs for CA).
        if instance.has_control_agent:
            services.insert(0, {"name": "Control Agent", "status_data": statuses.get("Control Agent", {})})

        can_change = Server.objects.restrict(request.user, "change").filter(pk=instance.pk).exists()
        return {
            "services": services,
            "global_options": _get_global_options(instance),
            "can_change_server": can_change,
        }
