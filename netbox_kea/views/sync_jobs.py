# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Views for Kea IPAM sync job management."""

from __future__ import annotations

import logging

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


def _get_latest_jobs(servers: list[Server]) -> dict[int, object]:
    """Return a dict mapping server.pk to most recent Job for that server (or None)."""
    from core.models import Job

    ct = ContentType.objects.get_for_model(Server)
    pks = [s.pk for s in servers]
    jobs = Job.objects.filter(object_type=ct, object_id__in=pks, name="Kea IPAM Sync").order_by("object_id", "-created")
    seen: set[int] = set()
    latest: dict[int, object] = {}
    for job in jobs:
        oid = job.object_id
        if oid not in seen:
            latest[oid] = job
            seen.add(oid)
    return latest


class SyncJobsView(LoginRequiredMixin, View):
    """Plugin-level Sync Jobs page: global config + cross-server summary table."""

    template_name = "netbox_kea/sync_jobs.html"

    def get(self, request):
        """Render the sync jobs overview page with config form and server table."""
        sync_cfg = SyncConfig.get()
        form = forms.SyncConfigForm(
            initial={"interval_minutes": sync_cfg.interval_minutes, "sync_enabled": sync_cfg.sync_enabled}
        )
        servers = list(Server.objects.order_by("name"))
        latest_jobs = _get_latest_jobs(servers)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "servers": servers,
                "latest_jobs": latest_jobs,
            },
        )

    def post(self, request):
        """Process SyncConfig form submission and re-schedule the background job."""
        if not request.user.has_perm("netbox_kea.change_syncconfig"):
            return HttpResponseForbidden()

        form = forms.SyncConfigForm(request.POST)
        if form.is_valid():
            sync_cfg = SyncConfig.get()
            sync_cfg.interval_minutes = form.cleaned_data["interval_minutes"]
            sync_cfg.sync_enabled = form.cleaned_data["sync_enabled"]
            sync_cfg.save()
            try:
                KeaIpamSyncJob.enqueue_once(interval=sync_cfg.interval_minutes)
            except Exception:
                logger.warning("Could not re-schedule KeaIpamSyncJob after interval change", exc_info=True)
            messages.success(request, "Sync configuration saved.")
            return HttpResponseRedirect(reverse("plugins:netbox_kea:sync_jobs"))

        servers = list(Server.objects.order_by("name"))
        latest_jobs = _get_latest_jobs(servers)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "servers": servers,
                "latest_jobs": latest_jobs,
            },
        )


@register_model_view(Server, "sync_status")
class ServerSyncStatusView(generic.ObjectView):
    """Per-server Sync Status tab."""

    queryset = Server.objects.all()
    tab = ViewTab(label="Sync Status", weight=1080)
    template_name = "netbox_kea/server_sync_status.html"

    def get_extra_context(self, request, instance):
        """Return recent sync jobs and status context for the template."""
        from core.models import Job

        ct = ContentType.objects.get_for_model(Server)
        recent_jobs = list(
            Job.objects.filter(object_type=ct, object_id=instance.pk, name="Kea IPAM Sync").order_by("-created")[
                :_JOB_HISTORY_COUNT
            ]
        )
        latest = recent_jobs[0] if recent_jobs else None
        return {
            "recent_jobs": recent_jobs,
            "latest_job": latest,
            "jobs_list_url": (f"/core/jobs/?object_type=netbox_kea.server&object_id={instance.pk}"),
        }


class ServerSyncNowView(LoginRequiredMixin, View):
    """POST-only: enqueue KeaIpamSyncJob for one server immediately."""

    def post(self, request, pk):
        """Enqueue a one-off sync job for the given server."""
        server = get_object_or_404(Server, pk=pk)
        if not request.user.has_perm("netbox_kea.change_server"):
            return HttpResponseForbidden()
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
        server = get_object_or_404(Server, pk=pk)
        if not request.user.has_perm("netbox_kea.change_server"):
            return HttpResponseForbidden()
        server.sync_enabled = not server.sync_enabled
        server.save(update_fields=["sync_enabled"])
        state = "enabled" if server.sync_enabled else "disabled"
        messages.success(request, f"IPAM sync {state} for {server.name}.")
        return HttpResponseRedirect(reverse("plugins:netbox_kea:server_sync_status", args=[pk]))
