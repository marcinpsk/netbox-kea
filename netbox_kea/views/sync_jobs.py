# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Views for Kea IPAM sync job management."""

from __future__ import annotations

import logging
from collections import defaultdict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View
from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from .. import forms
from ..jobs import KeaIpamSyncJob
from ..models import Server, SyncConfig

logger = logging.getLogger(__name__)

_JOB_HISTORY_COUNT = 5  # rows shown in the per-server tab mini-table


def _configured_default_interval() -> int:
    """Return the sync interval from PLUGINS_CONFIG, falling back to 5."""
    return settings.PLUGINS_CONFIG.get("netbox_kea", {}).get("sync_interval_minutes", 5)


def get_recent_jobs_for_servers(
    pks: list[int],
    *,
    name: str = "Kea IPAM Sync",
    limit: int = 1,
) -> dict[int, list]:
    """Return ``{server_pk: [jobs newest-first, up to *limit*]}`` for each pk.

    Queries both object-bound jobs (from ``KeaIpamSyncJob.enqueue``) and
    unbound periodic jobs (``object_id=NULL``, from ``@system_job`` runs).
    Unbound jobs are attributed to individual servers via ``job.data["summary"]``.
    Servers with no matching job map to an empty list.

    Args:
        pks: Server primary keys to look up.
        name: Job name filter — defaults to ``"Kea IPAM Sync"``.
        limit: Maximum number of jobs to return per server.

    """
    from core.models import Job

    ct = ContentType.objects.get_for_model(Server)
    result: dict[int, list] = {pk: [] for pk in pks}

    # 1. Object-bound jobs ordered newest-first per server.
    bound_qs = (
        Job.objects.filter(object_type=ct, object_id__in=pks, name=name)
        .order_by("object_id", "-created")
        .only("pk", "object_id", "created", "status", "data")
    )
    for job in bound_qs:
        oid = job.object_id
        if oid in result and len(result[oid]) < limit:
            result[oid].append(job)

    # 2. Unbound periodic jobs — supplement any server still below its limit.
    needs_more = {pk: limit - len(result[pk]) for pk in pks if len(result[pk]) < limit}
    if needs_more:
        scan_window = limit * 10 * max(1, len(pks))
        unbound_qs = (
            Job.objects.filter(object_id__isnull=True, name=name)
            .order_by("-created")
            .only("pk", "object_id", "created", "status", "data")
        )[:scan_window]
        for job in unbound_qs:
            if not needs_more:
                break
            for entry in (job.data or {}).get("summary", []):
                server_pk = entry.get("pk")
                if server_pk in needs_more:
                    result[server_pk].append(job)
                    needs_more[server_pk] -= 1
                    if needs_more[server_pk] <= 0:
                        del needs_more[server_pk]

    # Re-sort each merged list (bound and unbound may interleave by timestamp).
    for pk in pks:
        if len(result[pk]) > 1:
            result[pk].sort(key=lambda j: j.created, reverse=True)

    return result


def _get_latest_jobs(servers: list[Server]) -> defaultdict:
    """Return a defaultdict mapping server.pk to the most recent Job (or None).

    Thin wrapper around :func:`get_recent_jobs_for_servers` with ``limit=1``.
    """
    pks = [s.pk for s in servers]
    jobs_by_pk = get_recent_jobs_for_servers(pks, limit=1)
    return defaultdict(lambda: None, {pk: jobs[0] for pk, jobs in jobs_by_pk.items() if jobs})


class SyncJobsView(LoginRequiredMixin, View):
    """Plugin-level Sync Jobs page: global config + cross-server summary table.

    GET: readable by any authenticated user (via the view_server menu guard).
    POST: requires netbox_kea.change_syncconfig.
    """

    template_name = "netbox_kea/sync_jobs.html"

    def get(self, request):
        """Render the sync jobs overview page with config form and server table."""
        sync_cfg = SyncConfig.get(default_interval=_configured_default_interval())
        form = forms.SyncConfigForm(
            initial={
                "interval_minutes": sync_cfg.interval_minutes,
                "sync_enabled": sync_cfg.sync_enabled,
                "sync_leases_enabled": sync_cfg.sync_leases_enabled,
                "sync_reservations_enabled": sync_cfg.sync_reservations_enabled,
                "sync_prefixes_enabled": sync_cfg.sync_prefixes_enabled,
                "sync_ip_ranges_enabled": sync_cfg.sync_ip_ranges_enabled,
            }
        )
        servers = list(Server.objects.restrict(request.user, "view").order_by("name"))
        allowed_server_pks = set(Server.objects.restrict(request.user, "change").values_list("pk", flat=True))
        latest_jobs = _get_latest_jobs(servers)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "servers": servers,
                "latest_jobs": latest_jobs,
                "allowed_server_pks": allowed_server_pks,
            },
        )

    def post(self, request):
        """Process SyncConfig form submission and re-schedule the background job."""
        if not request.user.has_perm("netbox_kea.change_syncconfig"):
            return HttpResponseForbidden()

        form = forms.SyncConfigForm(request.POST)
        if form.is_valid():
            try:
                sync_cfg = SyncConfig.get(default_interval=_configured_default_interval())
                sync_cfg.interval_minutes = form.cleaned_data["interval_minutes"]
                sync_cfg.sync_enabled = form.cleaned_data["sync_enabled"]
                sync_cfg.sync_leases_enabled = form.cleaned_data["sync_leases_enabled"]
                sync_cfg.sync_reservations_enabled = form.cleaned_data["sync_reservations_enabled"]
                sync_cfg.sync_prefixes_enabled = form.cleaned_data["sync_prefixes_enabled"]
                sync_cfg.sync_ip_ranges_enabled = form.cleaned_data["sync_ip_ranges_enabled"]
                sync_cfg.save()
            except Exception:
                logger.exception("Failed to save SyncConfig")
                messages.error(request, "An internal error occurred")
                servers = list(Server.objects.restrict(request.user, "view").order_by("name"))
                allowed_server_pks = set(Server.objects.restrict(request.user, "change").values_list("pk", flat=True))
                latest_jobs = _get_latest_jobs(servers)
                return render(
                    request,
                    self.template_name,
                    {
                        "form": form,
                        "servers": servers,
                        "latest_jobs": latest_jobs,
                        "allowed_server_pks": allowed_server_pks,
                    },
                )
            try:
                from netbox.registry import registry

                if KeaIpamSyncJob in registry["system_jobs"]:
                    registry["system_jobs"][KeaIpamSyncJob]["interval"] = sync_cfg.interval_minutes
            except Exception:
                logger.exception("Could not update KeaIpamSyncJob interval in registry after config change")
            messages.success(request, "Sync configuration saved.")
            return HttpResponseRedirect(reverse("plugins:netbox_kea:sync_jobs"))

        servers = list(Server.objects.restrict(request.user, "view").order_by("name"))
        allowed_server_pks = set(Server.objects.restrict(request.user, "change").values_list("pk", flat=True))
        latest_jobs = _get_latest_jobs(servers)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "servers": servers,
                "latest_jobs": latest_jobs,
                "allowed_server_pks": allowed_server_pks,
            },
        )


@register_model_view(Server, "sync_status")
class ServerSyncStatusView(generic.ObjectView):
    """Per-server Sync Status tab."""

    queryset = Server.objects.all()
    tab = ViewTab(label="Sync", weight=1005)
    template_name = "netbox_kea/server_sync_status.html"

    def get_extra_context(self, request, instance):
        """Return recent sync jobs and status context for the template."""
        jobs_by_pk = get_recent_jobs_for_servers([instance.pk], limit=_JOB_HISTORY_COUNT)
        recent_jobs = jobs_by_pk[instance.pk]
        latest = recent_jobs[0] if recent_jobs else None

        jobs_list_url = reverse("core:job_list") + f"?object_type=netbox_kea.server&object_id={instance.pk}"
        sync_cfg = SyncConfig.get(default_interval=_configured_default_interval())
        return {
            "recent_jobs": recent_jobs,
            "latest_job": latest,
            "jobs_list_url": jobs_list_url,
            "can_change_server": Server.objects.restrict(request.user, "change").filter(pk=instance.pk).exists(),
            "sync_cfg": sync_cfg,
        }


class ServerSyncNowView(LoginRequiredMixin, View):
    """POST-only: enqueue KeaIpamSyncJob for one server immediately."""

    def post(self, request, pk):
        """Enqueue a one-off sync job for the given server."""
        if not request.user.has_perm("netbox_kea.change_server"):
            return HttpResponseForbidden()
        server = get_object_or_404(Server.objects.restrict(request.user, "change"), pk=pk)
        try:
            KeaIpamSyncJob.enqueue(instance=server, server_pk=server.pk)
            messages.success(request, f"Sync job enqueued for {server.name}.")
        except Exception:
            logger.exception("Failed to enqueue sync job for server %s", server.name)
            messages.error(request, "An internal error occurred when enqueuing the sync job.")
        return HttpResponseRedirect(reverse("plugins:netbox_kea:server_sync_status", args=[pk]))


class ServerSyncToggleView(LoginRequiredMixin, View):
    """POST-only: toggle Server.sync_enabled for one server."""

    def post(self, request, pk):
        """Toggle the sync_enabled flag for the given server."""
        if not request.user.has_perm("netbox_kea.change_server"):
            return HttpResponseForbidden()
        server = get_object_or_404(Server.objects.restrict(request.user, "change"), pk=pk)
        server.sync_enabled = not server.sync_enabled
        try:
            server.save(update_fields=["sync_enabled"])
        except Exception:
            logger.exception("Failed to toggle sync for server %s", server.name)
            messages.error(request, "An internal error occurred when toggling sync.")
            return HttpResponseRedirect(reverse("plugins:netbox_kea:server_sync_status", args=[pk]))
        state = "enabled" if server.sync_enabled else "disabled"
        messages.success(request, f"IPAM sync {state} for {server.name}.")
        return HttpResponseRedirect(reverse("plugins:netbox_kea:server_sync_status", args=[pk]))
