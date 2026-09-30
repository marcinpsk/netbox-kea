# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""NetBox validates the plugin settings at startup, before any sync reads them."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from utilities.release import load_release_data

from netbox_kea import NetBoxKeaConfig
from netbox_kea.constants import STALE_CLEANUP_MODES

NETBOX_VERSION = load_release_data().version


class StaleCleanupSettingTest(SimpleTestCase):
    def test_an_unknown_stale_ip_cleanup_value_fails_at_startup(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "stale_ip_cleanup"):
            NetBoxKeaConfig.validate({"stale_ip_cleanup": "delete"}, NETBOX_VERSION)

    def test_every_known_value_and_the_default_pass(self):
        for mode in STALE_CLEANUP_MODES:
            with self.subTest(mode=mode):
                NetBoxKeaConfig.validate({"stale_ip_cleanup": mode}, NETBOX_VERSION)
        NetBoxKeaConfig.validate({}, NETBOX_VERSION)
