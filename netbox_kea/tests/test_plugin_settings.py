# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""NetBox validates the plugin settings at startup, before any reader uses them."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from utilities.release import load_release_data

from netbox_kea import NetBoxKeaConfig
from netbox_kea.constants import STALE_CLEANUP_MODES
from netbox_kea.models import _get_kea_timeout, _get_max_unpaged_leases
from netbox_kea.sync import _get_stale_cleanup_mode
from netbox_kea.tests.utils import plugins_config

NETBOX_VERSION = load_release_data().version

# key -> (the allowed values as the error names them, bad values of a wrong type or out of range)
BAD_VALUES = {
    "kea_timeout": ("an integer of at least 1", ["30", True, 2.5, None, 0, -1]),
    "lease_query_max_unpaged_leases": ("an integer of at least 0", ["1000", False, 2.5, None, -1]),
    "stale_ip_cleanup": ("one of remove, deprecate, none", [1, None, ["remove"], "delete", "Remove"]),
    "sync_interval_minutes": ("an integer from 1 to 1440", ["5", True, 5.0, 0, 1441]),
    "sync_enabled": ("True or False", [1, 0, "false", None]),
    "sync_leases_enabled": ("True or False", [1, 0, "True", None]),
    "sync_reservations_enabled": ("True or False", [1, 0, "False", None]),
    "sync_prefixes_enabled": ("True or False", [1, 0, "yes", None]),
    "sync_ip_ranges_enabled": ("True or False", [1, 0, "no", None]),
    "sync_max_leases_per_server": ("an integer of at least 0", ["50000", True, 1e5, None, -1]),
}

VALID_CUSTOM = {
    "kea_timeout": 5,
    "lease_query_max_unpaged_leases": 0,
    "stale_ip_cleanup": "deprecate",
    "sync_interval_minutes": 1440,
    "sync_enabled": False,
    "sync_leases_enabled": False,
    "sync_reservations_enabled": False,
    "sync_prefixes_enabled": False,
    "sync_ip_ranges_enabled": False,
    "sync_max_leases_per_server": 0,
}


def validated(user_config: dict) -> dict:
    """Return a copy of *user_config* as NetBox leaves it after startup validation."""
    config = dict(user_config)
    NetBoxKeaConfig.validate(config, NETBOX_VERSION)
    return config


class PluginSettingsValidationTest(SimpleTestCase):
    def test_every_default_setting_is_covered_here(self):
        self.assertEqual(set(BAD_VALUES), set(NetBoxKeaConfig.default_settings))
        self.assertEqual(set(VALID_CUSTOM), set(NetBoxKeaConfig.default_settings))

    def test_a_bad_value_fails_at_startup_and_names_the_key_the_rule_and_the_value(self):
        for key, (allowed, values) in BAD_VALUES.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    message = f"netbox_kea: {key} must be {allowed}, not {value!r}"
                    with self.assertRaisesMessage(ImproperlyConfigured, message):
                        validated({key: value})

    def test_the_defaults_pass_and_netbox_fills_them_in(self):
        self.assertEqual(validated({}), NetBoxKeaConfig.default_settings)

    def test_a_valid_custom_config_passes_unchanged(self):
        self.assertEqual(validated(VALID_CUSTOM), VALID_CUSTOM)

    def test_every_stale_cleanup_mode_passes(self):
        for mode in STALE_CLEANUP_MODES:
            with self.subTest(mode=mode):
                validated({"stale_ip_cleanup": mode})

    def test_the_range_limits_pass(self):
        for key, value in (
            ("kea_timeout", 1),
            ("lease_query_max_unpaged_leases", 0),
            ("sync_interval_minutes", 1),
            ("sync_interval_minutes", 1440),
            ("sync_max_leases_per_server", 0),
        ):
            with self.subTest(key=key, value=value):
                validated({key: value})


class PluginSettingsReaderTest(SimpleTestCase):
    def test_each_reader_returns_the_configured_value(self):
        with override_settings(PLUGINS_CONFIG=plugins_config(**{**VALID_CUSTOM, "kea_timeout": 7})):
            self.assertEqual(_get_kea_timeout(), 7)
            self.assertIsNone(_get_max_unpaged_leases())
            self.assertEqual(_get_stale_cleanup_mode(), "deprecate")
        with override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=250)):
            self.assertEqual(_get_max_unpaged_leases(), 250)

    def test_a_reader_has_no_fallback_for_a_missing_key(self):
        for reader in (_get_kea_timeout, _get_max_unpaged_leases, _get_stale_cleanup_mode):
            with self.subTest(reader=reader.__name__), override_settings(PLUGINS_CONFIG={"netbox_kea": {}}):
                with self.assertRaises(KeyError):
                    reader()
