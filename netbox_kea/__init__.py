# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2023-2024 Devon Mar <devon-mar@users.noreply.github.com>
# SPDX-License-Identifier: Apache-2.0
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from netbox.plugins import PluginConfig

from . import branching
from .constants import STALE_CLEANUP_MODES

__version__ = "1.12.0"


class NetBoxKeaConfig(PluginConfig):
    """NetBox plugin configuration for the Kea DHCP integration."""

    name = "netbox_kea"
    verbose_name = "Kea"
    description = "Kea integration for NetBox"
    version = __version__
    base_url = "kea"
    middleware = ("netbox_kea.branching.BranchRefusalMiddleware",)
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

    @classmethod
    def validate(cls, user_config: dict[str, Any], netbox_version: str) -> None:
        """Refuse an unknown ``stale_ip_cleanup`` at startup, before any sync reads it."""
        super().validate(user_config, netbox_version)
        mode = user_config.get("stale_ip_cleanup", cls.default_settings["stale_ip_cleanup"])
        if mode not in STALE_CLEANUP_MODES:
            raise ImproperlyConfigured(
                f"netbox_kea: stale_ip_cleanup must be one of {', '.join(STALE_CLEANUP_MODES)}, not {mode!r}"
            )

    def ready(self) -> None:
        """Register the netbox-branching integration after Django is fully initialised."""
        super().ready()
        branching.register()


config = NetBoxKeaConfig
