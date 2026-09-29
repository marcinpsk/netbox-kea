from netbox.plugins import PluginConfig

from . import branching

__version__ = "1.11.0"


class NetBoxKeaConfig(PluginConfig):
    """NetBox plugin configuration for the Kea DHCP integration."""

    name = "netbox_kea"
    verbose_name = "Kea"
    description = "Kea integration for NetBox"
    version = __version__
    base_url = "kea"
    default_settings = {
        "kea_timeout": 30,
        "lease_query_max_unpaged_leases": 1000,
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
        """Register the netbox-branching integration after Django is fully initialised."""
        super().ready()
        branching.register()


config = NetBoxKeaConfig
