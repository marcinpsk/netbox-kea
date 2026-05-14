import logging

from netbox.plugins import PluginConfig

logger = logging.getLogger(__name__)

__version__ = "1.4.2"


class NetBoxKeaConfig(PluginConfig):
    """NetBox plugin configuration for the Kea DHCP integration."""

    name = "netbox_kea"
    verbose_name = "Kea"
    description = "Kea integration for NetBox"
    version = __version__
    base_url = "kea"
    default_settings = {
        "kea_timeout": 30,
        # stale_ip_cleanup: "remove" (delete stale IPs), "deprecate" (set status=deprecated), "none" (skip cleanup)
        "stale_ip_cleanup": "remove",
        # Background IPAM sync settings (Kea → NetBox via django-rq)
        "sync_interval_minutes": 5,
        "sync_leases_enabled": True,
        "sync_reservations_enabled": True,
        "sync_prefixes_enabled": True,
        "sync_ip_ranges_enabled": True,
        "sync_max_leases_per_server": 50000,
    }

    def ready(self) -> None:
        """Apply runtime configuration overrides after Django is fully initialised."""
        super().ready()
        self._configure_sync_job_interval()
        self._heal_ghost_scheduled_jobs()

    def _configure_sync_job_interval(self) -> None:
        """Seed the KeaIpamSyncJob interval from PLUGINS_CONFIG at startup.

        The ``@system_job`` decorator registers a static default interval.  We
        patch the in-memory registry here so the worker uses the operator's
        configured value from PLUGINS_CONFIG on startup.

        We intentionally do NOT query the database here.  ``ready()`` is called
        during every Django management command — including ``collectstatic`` and
        ``migrate`` run inside Docker image builds where the database is not yet
        reachable.  The UI (SyncJobsView) updates the registry live whenever the
        operator saves a new interval, so runtime changes take effect immediately
        without a restart.
        """
        try:
            from django.conf import settings
            from netbox.registry import registry

            from .jobs import KeaIpamSyncJob

            config = getattr(settings, "PLUGINS_CONFIG", {}).get("netbox_kea", {})
            interval = max(1, int(config.get("sync_interval_minutes", 5)))

            if KeaIpamSyncJob in registry["system_jobs"]:
                registry["system_jobs"][KeaIpamSyncJob]["interval"] = interval
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to apply netbox_kea sync interval override; using decorator default.",
                exc_info=True,
            )

    def _heal_ghost_scheduled_jobs(self) -> None:
        """Remove ghost scheduled-job DB records that have no live RQ counterpart.

        A ghost record arises when a periodic job's execution fails at the DB level
        (e.g. PostgreSQL in recovery mode) — the DB record stays ``scheduled`` forever
        while RQ marks the job ``failed``.  NetBox's ``enqueue_once()`` trusts the DB
        status and skips re-scheduling, silently breaking the periodic chain.

        Deleting stale DB records here lets the worker's ``enqueue_once()`` call
        (which runs immediately after ``ready()``) create a fresh schedule.

        Safe to call in all contexts:
        - Never enqueues jobs (deferred to ``rqworker``).
        - All DB and Redis I/O is wrapped so a missing DB or Redis at image-build
          time never breaks startup.
        """
        try:
            import django_rq
            from rq.exceptions import NoSuchJobError
            from rq.job import Job as RQJob

            from .jobs import KeaIpamSyncJob

            candidates = KeaIpamSyncJob.get_jobs(None).filter(status__in=("scheduled", "pending"))
            if not candidates.exists():
                return

            conn = django_rq.get_connection("default")
            _DEAD = {"failed", "canceled", "stopped"}

            deleted = 0
            for db_job in candidates:
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

            if deleted:
                logger.warning(
                    "netbox_kea: removed %d ghost scheduled-job record(s) with a dead or missing "
                    "RQ counterpart. Periodic IPAM sync will resume on the next worker startup.",
                    deleted,
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "netbox_kea: ghost-job self-heal skipped (DB or Redis not available at startup).",
                exc_info=True,
            )


config = NetBoxKeaConfig
