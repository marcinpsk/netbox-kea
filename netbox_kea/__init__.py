import logging

from netbox.plugins import PluginConfig

logger = logging.getLogger(__name__)

__version__ = "1.4.1"


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


config = NetBoxKeaConfig
