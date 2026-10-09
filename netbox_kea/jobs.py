# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Background jobs for netbox-kea-ng plugin.

Registers periodic Kea→NetBox IPAM sync jobs using NetBox's built-in
``JobRunner`` / ``@system_job`` infrastructure so they run automatically via
``manage.py rqworker`` without any external scheduler.

The sync interval comes from ``SyncConfig.interval_minutes``, which the Sync Jobs
page edits. ``enqueue_once`` and each periodic run read it, so a saved value
applies after the next scheduled run.

The ``PLUGINS_CONFIG["netbox_kea"]`` settings and their rules are in ``plugin_settings.SETTINGS``.
"""

from __future__ import annotations

import logging
from contextvars import copy_context
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from core import signals as core_signals
from core.exceptions import JobFailed
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.http import HttpRequest
from netbox.context_managers import event_tracking
from netbox.jobs import JobRunner, system_job

if TYPE_CHECKING:
    from .models import Server

# Runtime imports: get_type_hints() resolves this module's annotations, so a
# TYPE_CHECKING-only Family or SyncReport would raise NameError.
from . import branching
from .constants import Family
from .ipam_reconciliation import SyncReport, complete_job_observation, upgrade_counts
from .plugin_settings import plugin_setting

logger = logging.getLogger(__name__)

# NetBox requires a registry interval; enqueue_once replaces it with SyncConfig.interval_minutes.
_DEFAULT_INTERVAL = 5


#: How many conflicting IPs to name in the job summary and log line.  A bare count
#: tells an operator nothing about which manually-curated IPs the sync left alone.
_CONFLICT_SAMPLE_SIZE = 20
_SYNC_USER = "netbox-kea-sync"


class _SyncJobRequest(HttpRequest):
    """An empty worker request with NetBox's change-tracking request ID."""

    id: UUID


def _sync_one_server(
    server: Server,
    sync_leases: bool,
    sync_reservations: bool,
    sync_prefixes: bool,
    sync_ip_ranges: bool,
    max_leases: int,
) -> SyncReport:
    """Reconcile every enabled family and publish completion from the whole run."""
    from .ipam_reconciliation import (
        LeasePhase,
        LeasePrefixPhase,
        Phase,
        PoolPhase,
        ReservationPhase,
        SubnetPhase,
        lease_prefixes_enabled,
        read_catalogue,
        read_leases,
        reconcile,
    )

    combined = SyncReport()
    if not any((sync_leases, sync_reservations, sync_prefixes, sync_ip_ranges)):
        return combined
    reports: dict[Family, SyncReport] = {}
    versions: tuple[tuple[Family, bool], ...] = ((4, server.dhcp4), (6, server.dhcp6))
    for version, enabled in versions:
        if not enabled:
            continue

        observation = read_catalogue(server, version)
        catalogue = observation.catalogue
        if catalogue is None:
            logger.warning("Server %s (v%s): Subnet Catalogue unavailable", server.name, version)
        subnet_prefix_map = (
            {subnet.identity.subnet_id: subnet.identity.network.prefixlen for subnet in catalogue.subnets}
            if catalogue is not None
            else None
        )
        if catalogue is None and sync_leases:
            logger.info(
                "Server %s (v%s): Subnet Catalogue unavailable; lease masks fall back to NetBox prefix matching",
                server.name,
                version,
            )

        phases: list[Phase] = []
        leases = read_leases(server, version, max_leases or None) if sync_leases else None
        if leases is not None:
            phases.append(LeasePhase(leases, subnet_prefix_lengths=subnet_prefix_map))
        if sync_reservations:
            phases.append(ReservationPhase(catalogue=catalogue))
        if sync_prefixes:
            phases.append(SubnetPhase(observation))
        if sync_ip_ranges:
            phases.append(PoolPhase(observation))
        if leases is not None and lease_prefixes_enabled(version, sync_leases, sync_prefixes):
            phases.append(LeasePrefixPhase(leases, catalogue))
        reports[version] = reconcile(server, version, phases)
        combined.merge(reports[version])

    complete_job_observation(server, reports)

    if combined.disagreements:
        sample = sorted(combined.disagreements)[:_CONFLICT_SAMPLE_SIZE]
        logger.warning(
            "Server %s: %d NetBox IPAM object(s) keep their facts because their owners report different facts; first %d: %s",
            server.name,
            len(combined.disagreements),
            len(sample),
            ", ".join(sample),
        )
    if combined.unclassified:
        logger.warning(
            "Server %s: %d NetBox IP address(es) keep a stale DHCPv6 lease link of unknown allocation kind;"
            " a later Kea report of the address or of its delegated prefix classifies the link,"
            " and removing the sync marker releases it",
            server.name,
            combined.unclassified,
        )
    if combined.conflicts:
        sample = sorted(combined.conflicts)[:_CONFLICT_SAMPLE_SIZE]
        logger.warning(
            "Server %s: %d NetBox IPAM object(s) left untouched: the description does not start with the sync marker,"
            " or the new marker and the note do not fit; first %d: %s",
            server.name,
            len(combined.conflicts),
            len(sample),
            ", ".join(sample),
        )

    return combined


@system_job(interval=_DEFAULT_INTERVAL)
class KeaIpamSyncJob(JobRunner):
    """Periodic Kea→NetBox IPAM sync job.

    Iterates over all configured ``Server`` objects and syncs their active
    leases and/or reservations into NetBox ``IPAddress`` records.  Error on
    one server does not prevent syncing the remaining servers.

    The sync is idempotent: existing ``IPAddress`` objects are updated in place
    when their fields have changed; unchanged records are left untouched.
    """

    class Meta:
        name = "Kea IPAM Sync"

    def __init__(self, job: Any) -> None:
        """Use the same job logger on each supported native runner."""
        super().__init__(job)
        self.logger = logging.getLogger(f"netbox.jobs.{type(self).__name__}")
        self.logger.setLevel(logging.DEBUG)

    @classmethod
    def handle(cls, job: Any, *args: Any, **kwargs: Any) -> None:
        """Refresh the queued actor before native lifecycle writes can restore a deleted user."""
        job.refresh_from_db(fields=["user"])
        super().handle(job, *args, **kwargs)

    @classmethod
    def enqueue_once(
        cls, instance: Any = None, schedule_at: Any = None, interval: int | None = None, *args: Any, **kwargs: Any
    ) -> Any:
        """Heal ghost scheduled-job records before delegating to NetBox.

        NetBox schedules system jobs by calling ``enqueue_once`` on the job class
        once per ``rqworker`` startup (``core/management/commands/rqworker.py``).
        That runs after app initialisation and on the worker process only — so it
        is the correct place to reconcile stale schedule state, unlike
        ``AppConfig.ready()`` which also fires during ``collectstatic``/``migrate``
        (where the DB may be unreachable) and triggers Django's "database access
        during app initialization" warning.

        We clean ghost records first, then delegate to the stock
        ``enqueue_once`` so it can create a fresh schedule.  The signature is
        NetBox's, so a positional ``interval`` binds once; ``SyncConfig.interval_minutes``
        replaces it, and the other arguments are forwarded verbatim.
        """
        from .models import SyncConfig

        cls._heal_ghost_scheduled_jobs()
        return super().enqueue_once(instance, schedule_at, SyncConfig.get().interval_minutes, *args, **kwargs)

    @classmethod
    def _heal_ghost_scheduled_jobs(cls) -> None:
        """Remove ghost scheduled-job DB records that have no live RQ counterpart.

        A ghost record arises when a periodic job's execution fails at the DB level
        (e.g. PostgreSQL in recovery mode) — the DB record stays ``scheduled`` forever
        while RQ marks the job ``failed``.  NetBox's ``enqueue_once()`` trusts the DB
        status and skips re-scheduling, silently breaking the periodic chain.

        Deleting stale DB records here lets the immediately-following
        ``super().enqueue_once()`` call create a fresh schedule.

        Safe to call in all contexts:
        - Never enqueues jobs (the caller handles that).
        - All DB and Redis I/O is wrapped so a missing DB or a Redis still coming
          up at worker startup never breaks scheduling.
        """
        try:
            import django_rq
            from rq.exceptions import NoSuchJobError
            from rq.job import Job as RQJob

            candidates = cls.get_jobs(None).filter(status__in=("scheduled", "pending"))
            if not candidates.exists():
                return

            conn = django_rq.get_connection("default")
            _DEAD = {"failed", "canceled", "stopped"}

            deleted = 0
            for db_job in candidates:
                try:
                    job_id = str(db_job.job_id) if db_job.job_id else None
                    if job_id is None:
                        db_job.delete()
                        deleted += 1
                        continue
                    try:
                        rq_job = RQJob.fetch(job_id, connection=conn)
                        status = rq_job.get_status()
                        # Handle both enum (rq ≥ 1.16) and plain string
                        status_str = status.value if hasattr(status, "value") else str(status)
                        if status_str in _DEAD:
                            db_job.delete()
                            deleted += 1
                    except NoSuchJobError:
                        db_job.delete()
                        deleted += 1
                except Exception:
                    logger.debug(
                        "netbox_kea: skipping ghost-job check for record %r due to per-record error.",
                        getattr(db_job, "pk", None),
                        exc_info=True,
                    )

            if deleted:
                logger.warning(
                    "netbox_kea: removed %d ghost scheduled-job record(s) with a dead or missing "
                    "RQ counterpart. Periodic IPAM sync will resume now.",
                    deleted,
                )
        except Exception:
            logger.warning(
                "netbox_kea: ghost-job self-heal skipped (DB or Redis not available at scheduling time).",
                exc_info=True,
            )

    def _fail_in_branch(self) -> None:
        """Raise JobFailed when a branch is active in the worker: the job reads and writes main only."""
        if (branch := branching.active_branch()) is not None:
            # NetBox saves the error when it marks the job failed; the job itself writes nothing here.
            self.job.error = f"Branch {branch} is active in the worker. The Kea IPAM sync runs on main only."
            raise JobFailed(self.job.error)

    def run(self, *args: Any, **kwargs: Any) -> None:
        """Execute the sync across all servers. It fails before any read when a branch is active in the worker."""
        self._fail_in_branch()
        copy_context().run(self._run_tracked, *args, **kwargs)

    def _run_tracked(self, *args: Any, **kwargs: Any) -> None:
        """Track one execution without leaking native tracking state into the caller."""
        # Older supported NetBox releases have no delete suppression history.
        clear_history = getattr(core_signals, "clear_signal_history", None)
        if clear_history is not None:
            clear_history(sender=type(self))
        try:
            request = _SyncJobRequest()
            request.method = "POST"
            request.path = "/plugins/kea/sync-jobs/"
            request.user = self._change_actor()
            request.id = uuid4()
            with event_tracking(request):
                self._run_sync(*args, **kwargs)
        finally:
            if clear_history is not None:
                clear_history(sender=type(self))

    def _change_actor(self) -> Any:
        """Return the initiator, or reserve a disabled account for system attribution."""
        if self.job.user is not None:
            return self.job.user
        user_model: Any = get_user_model()
        actor, _ = user_model.objects.get_or_create(
            username__iexact=_SYNC_USER,
            defaults={
                "username": _SYNC_USER,
                "is_active": False,
                "is_superuser": False,
                "password": make_password(None),
            },
        )
        if (
            actor.username != _SYNC_USER
            or actor.is_active
            or actor.is_superuser
            or getattr(actor, "is_staff", False)
            or actor.has_usable_password()
            or actor.groups.exists()
            or actor.user_permissions.exists()
            or actor.object_permissions.exists()
        ):
            self.job.error = f"Reserved user {_SYNC_USER} must be inactive, without a usable password or permissions."
            raise JobFailed(self.job.error)
        return actor

    def _run_sync(self, *args: Any, **kwargs: Any) -> None:
        """Apply the configured synchronization and retain the per-server summary."""
        from .models import Server, SyncConfig

        summary: list[dict] = []
        try:
            sync_cfg = SyncConfig.get()
            if self.job.interval:
                # handle() schedules the successor with job.interval after run() returns.
                self.job.interval = sync_cfg.interval_minutes
            if not sync_cfg.sync_enabled:
                self.logger.info("Global sync kill-switch is active (SyncConfig.sync_enabled=False) — skipping.")
                return

            sync_leases = sync_cfg.sync_leases_enabled
            sync_reservations = sync_cfg.sync_reservations_enabled
            sync_prefixes = sync_cfg.sync_prefixes_enabled
            sync_ip_ranges = sync_cfg.sync_ip_ranges_enabled
            max_leases = plugin_setting("sync_max_leases_per_server")

            if not any([sync_leases, sync_reservations, sync_prefixes, sync_ip_ranges]):
                self.logger.info("All sync type flags are False — nothing to do.")
                return

            server_pk = kwargs.get("server_pk")
            server_qs = Server.objects.filter(pk=server_pk) if server_pk is not None else Server.objects.all()
            servers = list(server_qs)

            if not servers:
                self.logger.info("No Kea servers configured. Count unowned IPAM objects.")

            self.logger.info(f"Starting Kea IPAM sync for {len(servers)} server(s).")
            total = SyncReport()
            total_conflicts = 0
            total_disagreements = 0

            for server in servers:
                # In Run Now mode (server_pk provided), honour the explicit selection
                # and skip the per-server enabled check.
                if server_pk is None and not server.sync_enabled:
                    self.logger.info(f"Server {server.name}: sync_enabled=False — skipping.")
                    continue

                # Per-server type overrides: AND global flag with server flag.
                effective_leases = sync_leases and server.sync_leases_enabled
                effective_reservations = sync_reservations and server.sync_reservations_enabled
                effective_prefixes = sync_prefixes and server.sync_prefixes_enabled
                effective_ip_ranges = sync_ip_ranges and server.sync_ip_ranges_enabled

                self.logger.debug(f"Syncing server: {server.name} (pk={server.pk})")
                report = SyncReport()

                try:
                    report = _sync_one_server(
                        server,
                        effective_leases,
                        effective_reservations,
                        effective_prefixes,
                        effective_ip_ranges,
                        max_leases,
                    )
                except Exception:
                    self.logger.exception(f"Unhandled error syncing server {server.name}; see server logs")
                    report.errors += 1

                self.logger.info(
                    f"Server {server.name}: created={report.created}"
                    f" updated={report.updated} errors={report.errors}"
                    f" prefix_errors={report.prefix_errors}"
                    f" conflicts={len(report.conflicts)}"
                    f" disagreements={len(report.disagreements)}"
                    f" skipped={len(report.skipped_reservations)}"
                    f" waiting={report.waiting}"
                    f" unclassified={report.unclassified}"
                )
                # No row pks here: the list URL applies the viewer's own IPAM permissions.
                for dup in report.duplicates:
                    self.logger.error(
                        f"Server {server.name}: Kea {dup.kea_object} matches duplicate NetBox {dup.rows};"
                        f" the sync leaves them unchanged. Review them at {dup.list_url}"
                    )
                total.merge(report)
                total_conflicts += len(report.conflicts)
                total_disagreements += len(report.disagreements)

                summary.append(
                    {
                        "name": server.name,
                        "pk": server.pk,
                        "created": report.created,
                        "updated": report.updated,
                        "errors": report.errors,
                        "prefix_errors": report.prefix_errors,
                        "conflicts": len(report.conflicts),
                        "conflict_sample": sorted(report.conflicts)[:_CONFLICT_SAMPLE_SIZE],
                        "conflicts_truncated": max(0, len(report.conflicts) - _CONFLICT_SAMPLE_SIZE),
                        "disagreements": len(report.disagreements),
                        "skipped": len(report.skipped_reservations),
                        "waiting": report.waiting,
                        "unclassified": report.unclassified,
                    }
                )

            counts = upgrade_counts()
            total.unowned_objects = counts.unowned_objects
            total.waiting_objects = counts.waiting_objects
            self.logger.info(
                f"Kea IPAM sync complete — servers={len(summary)}"
                f" created={total.created} updated={total.updated}"
                f" errors={total.errors} prefix_errors={total.prefix_errors}"
                f" conflicts={total_conflicts} disagreements={total_disagreements} skipped={len(total.skipped_reservations)}"
                f" unowned={total.unowned} waiting={total.waiting} unclassified={total.unclassified}"
            )
            if total.errors > 0 or total.prefix_errors > 0:
                raise JobFailed
        finally:
            if not isinstance(self.job.data, dict):
                self.job.data = {}
            self.job.data["summary"] = summary
            try:
                self.job.save(update_fields=["data"])
            except Exception:
                logger.exception("Failed to persist sync job summary data")
