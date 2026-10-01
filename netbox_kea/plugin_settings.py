# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The one schema for ``PLUGINS_CONFIG["netbox_kea"]``: each key's default and the values it accepts.

NetBox fills in the defaults and calls ``NetBoxKeaConfig.validate`` at startup, so a reader gets a valid value.
"""

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .constants import STALE_CLEANUP_MODES

PLUGIN_NAME = "netbox_kea"


@dataclass(frozen=True)
class IntSetting:
    """An integer setting from ``minimum`` to ``maximum``, both included. ``bool`` and ``str`` are not integers."""

    default: int
    minimum: int
    maximum: int | None = None

    def accepts(self, value: object) -> bool:
        """Return True when *value* is an ``int`` in range."""
        return type(value) is int and value >= self.minimum and (self.maximum is None or value <= self.maximum)

    def allowed(self) -> str:
        """Describe the accepted values."""
        if self.maximum is None:
            return f"an integer of at least {self.minimum}"
        return f"an integer from {self.minimum} to {self.maximum}"


@dataclass(frozen=True)
class BoolSetting:
    """A ``True`` or ``False`` setting."""

    default: bool

    def accepts(self, value: object) -> bool:
        """Return True when *value* is a ``bool``."""
        return type(value) is bool

    def allowed(self) -> str:
        """Describe the accepted values."""
        return "True or False"


@dataclass(frozen=True)
class ChoiceSetting:
    """A string setting with a fixed set of values."""

    default: str
    choices: tuple[str, ...]

    def accepts(self, value: object) -> bool:
        """Return True when *value* is one of the choices."""
        return type(value) is str and value in self.choices

    def allowed(self) -> str:
        """Describe the accepted values."""
        return f"one of {', '.join(self.choices)}"


SETTINGS: dict[str, IntSetting | BoolSetting | ChoiceSetting] = {
    # Seconds.
    "kea_timeout": IntSetting(default=30, minimum=1),
    # 0 disables the guard.
    "lease_query_max_unpaged_leases": IntSetting(default=1000, minimum=0),
    "stale_ip_cleanup": ChoiceSetting(default="remove", choices=STALE_CLEANUP_MODES),
    # The SyncConfig.interval_minutes check constraint.
    "sync_interval_minutes": IntSetting(default=5, minimum=1, maximum=1440),
    "sync_leases_enabled": BoolSetting(default=True),
    "sync_reservations_enabled": BoolSetting(default=True),
    "sync_prefixes_enabled": BoolSetting(default=True),
    "sync_ip_ranges_enabled": BoolSetting(default=True),
    # 0 disables the cap.
    "sync_max_leases_per_server": IntSetting(default=50000, minimum=0),
}

DEFAULT_SETTINGS: dict[str, Any] = {key: setting.default for key, setting in SETTINGS.items()}


def validate_settings(config: dict[str, Any]) -> None:
    """Raise ImproperlyConfigured for the first value in *config* that its setting does not accept."""
    for key, setting in SETTINGS.items():
        value = config[key]
        if not setting.accepts(value):
            raise ImproperlyConfigured(f"{PLUGIN_NAME}: {key} must be {setting.allowed()}, not {value!r}")


def plugin_setting(key: str) -> Any:
    """Return the validated value of *key*; NetBox has filled in the default when the user did not set it."""
    return settings.PLUGINS_CONFIG[PLUGIN_NAME][key]
