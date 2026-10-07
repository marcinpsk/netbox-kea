# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2023-2024 Devon Mar <devon-mar@users.noreply.github.com>
# SPDX-License-Identifier: Apache-2.0
from typing import Any

from netbox.plugins import PluginConfig

from . import branching
from .plugin_settings import DEFAULT_SETTINGS, validate_settings

__version__ = "1.14.0"


class NetBoxKeaConfig(PluginConfig):
    """NetBox plugin configuration for the Kea DHCP integration."""

    name = "netbox_kea"
    verbose_name = "Kea"
    description = "Kea integration for NetBox"
    version = __version__
    base_url = "kea"
    middleware = ("netbox_kea.branching.BranchRefusalMiddleware",)
    default_settings = DEFAULT_SETTINGS

    @classmethod
    def validate(cls, user_config: dict[str, Any], netbox_version: str) -> None:
        """Refuse an invalid setting at startup, after NetBox fills in the defaults."""
        super().validate(user_config, netbox_version)
        validate_settings(user_config)

    def ready(self) -> None:
        """Register optional integrations after Django is fully initialised."""
        super().ready()
        branching.register()
        from .dhcp_mapping_lifecycle import register as register_mapping_lifecycle
        from .integrations.dhcp_plugin import register_link_cleanup

        register_link_cleanup()
        register_mapping_lifecycle()


config = NetBoxKeaConfig
