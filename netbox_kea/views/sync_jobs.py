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
from django.core.exceptions import PermissionDenied
from django.db.models import Q
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

    # 1. Object-bound jobs (manual "Run Now") — one indexed query per server with SQL LIMIT.
    bound_by_pk: dict[int, list] = {pk: [] for pk in pks}
    for pk in pks:
        bound_by_pk[pk] = list(
            Job.objects.filter(object_type=ct, object_id=pk, name=name)
            .order_by("-created")
            .only("pk", "object_id", "created", "status", "data")[:limit]
        )

    # 2. Unbound periodic jobs — always scanned independently so that a recent
    #    periodic run is not hidden by older manual (bound) runs filling the slot.
    #    Heuristic window: scan far enough back to cover periodic runs for all servers.
    unbound_by_pk: dict[int, list] = {pk: [] for pk in pks}
    needs_unbound = set(pks)
    scan_window = limit * 10 * max(1, len(pks))
    unbound_qs = (
        Job.objects.filter(object_id__isnull=True, name=name)
        .order_by("-created")
        .only("pk", "object_id", "created", "status", "data")
    )[:scan_window]
    for job in unbound_qs:
        if not needs_unbound:
            break
        if not isinstance(job.data, dict):
            continue
        summary = job.data.get("summary")
        if not isinstance(summary, list):
            continue
        for entry in summary:
            if not isinstance(entry, dict):
                continue
            server_pk = entry.get("pk")
            if server_pk in needs_unbound and job.pk not in {j.pk for j in unbound_by_pk[server_pk]}:
                unbound_by_pk[server_pk].append(job)
                if len(unbound_by_pk[server_pk]) >= limit:
                    needs_unbound.discard(server_pk)

    if needs_unbound:
        logger.warning(
            "get_recent_jobs_for_servers: scan_window=%d exhausted; periodic jobs may be missing for server pks: %s",
            scan_window,
            sorted(needs_unbound),
        )

    # 3. Merge both sources, deduplicate, sort newest-first, keep top *limit*.
    result: dict[int, list] = {}
    for pk in pks:
        merged = bound_by_pk[pk] + unbound_by_pk[pk]
        seen: set[int] = set()
        deduped = []
        for job in merged:
            if job.pk not in seen:
                seen.add(job.pk)
                deduped.append(job)
        deduped.sort(key=lambda j: j.created, reverse=True)
        result[pk] = deduped[:limit]

    return result


def get_all_jobs_for_server(pk: int, *, name: str = "Kea IPAM Sync"):
    """Return a queryset of every Kea sync Job attributed to *pk*.

    Includes both object-bound jobs (manual "Run Now") and unbound periodic
    jobs whose ``data["summary"]`` references this server. Sorted newest-first.

    Periodic jobs have ``object_id=NULL`` so the standard ObjectJobsView
    misses them; this helper bridges the gap. Periodic-job lookup uses
    PostgreSQL JSONField containment so it stays cheap even with thousands
    of historical runs.
    """
    from core.models import Job

    ct = ContentType.objects.get_for_model(Server)
    bound_q = Q(object_type=ct, object_id=pk, name=name)
    # JSONField `contains` on a list matches any element containing the given dict.
    unbound_q = Q(object_id__isnull=True, name=name, data__summary__contains=[{"pk": pk}])
    return Job.objects.filter(bound_q | unbound_q).order_by("-created")


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


def _replace_auto_jobs_view(model, replacement_view) -> None:
    """Replace NetBox's auto-registered ``jobs`` view in the registry with our own.

    NetBox's ``JobsMixin.__init_subclass__`` registers a default ``ObjectJobsView``
    for every job-aware model. That view filters by ``object_type+object_id`` and
    therefore cannot show periodic (system) jobs whose ``object_id`` is NULL.
    We swap the entry so the per-server "Jobs" tab points at our subclass instead.
    """
    from netbox.registry import registry

    app_label = model._meta.app_label
    model_name = model._meta.model_name
    # Ensure the nested registry structure exists so the fallback append below
    # actually mutates the registry (chained .get() with defaults would create
    # a detached list and silently drop the registration).
    app_views = registry["views"].setdefault(app_label, {})
    views = app_views.setdefault(model_name, [])
    for entry in views:
        if entry.get("name") == "jobs":
            entry["view"] = replacement_view
            entry["kwargs"] = {"model": model}
            return
    views.append(
        {
            "name": "jobs",
            "view": replacement_view,
            "path": "jobs",
            "detail": True,
            "kwargs": {"model": model},
        }
    )


class ServerJobsView(View):
    """Per-server Jobs tab, including periodic (unbound) Kea sync jobs.

    The default :class:`netbox.views.generic.ObjectJobsView` only shows jobs
    whose ``object_id`` matches the server. Periodic ``KeaIpamSyncJob`` runs
    are stored unbound (``object_id=NULL``) and reference servers only via
    their ``data["summary"]`` payload, so they are invisible there. This
    subclass merges both sources, matching core's ``ObjectJobsView`` shape
    so the existing ``core/object_jobs.html`` template works unchanged.
    """

    tab = ViewTab(
        label="Jobs",
        badge=lambda obj: get_all_jobs_for_server(obj.pk).count(),
        permission="core.view_job",
        weight=11000,
    )

    def get(self, request, model, **kwargs):
        """Render the Jobs tab using core's object_jobs template + JobTable."""
        if not request.user.has_perm("core.view_job"):
            raise PermissionDenied
        from core.tables import JobTable

        obj = get_object_or_404(model.objects.restrict(request.user, "view"), **kwargs)
        jobs = get_all_jobs_for_server(obj.pk)
        table = JobTable(data=jobs, orderable=False)
        table.configure(request)

        base_template = f"{model._meta.app_label}/{model._meta.model_name}.html"
        return render(
            request,
            "core/object_jobs.html",
            {
                "object": obj,
                "table": table,
                "base_template": base_template,
                "tab": self.tab,
            },
        )


_replace_auto_jobs_view(Server, ServerJobsView)
