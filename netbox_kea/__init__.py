import logging

from netbox.plugins import PluginConfig

logger = logging.getLogger(__name__)

__version__ = "1.0.4"


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
        "sync_max_leases_per_server": 50000,
    }

    def ready(self) -> None:
        """Apply runtime configuration overrides after Django is fully initialised."""
        super().ready()
        self._configure_sync_job_interval()

    def _configure_sync_job_interval(self) -> None:
        """Override the KeaIpamSyncJob interval from persisted SyncConfig (falling back to PLUGINS_CONFIG).

        The ``@system_job`` decorator registers a static default interval.  We
        patch the registry here so the configured interval is used by the
        worker when it starts.  The persisted ``SyncConfig.interval_minutes``
        value is the single source of truth; ``sync_interval_minutes`` in
        PLUGINS_CONFIG is only the seed value used before the user has saved
        any configuration via the UI.
        """
        try:
            from django.conf import settings
            from netbox.registry import registry

            from .jobs import KeaIpamSyncJob
            from .models import SyncConfig

            # Seed the SyncConfig with PLUGINS_CONFIG on first creation so the
            # config file value is honoured until the operator saves via the UI.
            # SyncConfig.get() only uses default_interval when the row doesn't yet exist.
            config = getattr(settings, "PLUGINS_CONFIG", {}).get("netbox_kea", {})
            default_interval = int(config.get("sync_interval_minutes", 5))
            interval = SyncConfig.get(default_interval=default_interval).interval_minutes

            if interval < 1:
                interval = 1
            if KeaIpamSyncJob in registry["system_jobs"]:
                registry["system_jobs"][KeaIpamSyncJob]["interval"] = interval
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to apply netbox_kea sync interval override; using decorator default.",
                exc_info=True,
            )


config = NetBoxKeaConfig
