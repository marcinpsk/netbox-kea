# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the curated standard DHCP option lists used by the option editors."""

from __future__ import annotations

import re

from django.test import SimpleTestCase

from netbox_kea.dhcp_options import (
    _SHOWN_OPTIONS,
    KEA_DHCP4_STD_OPTIONS,
    KEA_DHCP6_STD_OPTIONS,
    form_managed_options,
    kea_std_options,
)

_NAME_RE = re.compile(r"^[\w-]+$")


class TestStdOptionLists(SimpleTestCase):
    def test_lists_non_empty(self):
        self.assertGreater(len(KEA_DHCP4_STD_OPTIONS), 0)
        self.assertGreater(len(KEA_DHCP6_STD_OPTIONS), 0)

    def test_v4_names_and_codes_valid(self):
        for name, code in KEA_DHCP4_STD_OPTIONS:
            self.assertTrue(_NAME_RE.match(name), name)
            self.assertTrue(1 <= code <= 254, f"{name}={code}")

    def test_v6_names_and_codes_valid(self):
        for name, code in KEA_DHCP6_STD_OPTIONS:
            self.assertTrue(_NAME_RE.match(name), name)
            self.assertTrue(1 <= code <= 65535, f"{name}={code}")

    def test_no_duplicate_names_within_version(self):
        for opts in (KEA_DHCP4_STD_OPTIONS, KEA_DHCP6_STD_OPTIONS):
            names = [n for n, _ in opts]
            self.assertEqual(len(names), len(set(names)))

    def test_no_duplicate_codes_within_version(self):
        for opts in (KEA_DHCP4_STD_OPTIONS, KEA_DHCP6_STD_OPTIONS):
            codes = [c for _, c in opts]
            self.assertEqual(len(codes), len(set(codes)))

    def test_dispatch_by_version(self):
        self.assertIs(kea_std_options(4), KEA_DHCP4_STD_OPTIONS)
        self.assertIs(kea_std_options(6), KEA_DHCP6_STD_OPTIONS)
        with self.assertRaises(KeyError):
            kea_std_options(0)


class TestStandardTableCoversManagedOptions(SimpleTestCase):
    """Each option that a form edits or the Subnet table shows takes its code from the standard table."""

    def test_each_form_managed_option_has_a_standard_definition(self):
        for version in (4, 6):
            for option in form_managed_options(version).values():
                with self.subTest(version=version, name=option.name):
                    self.assertEqual(option.code, dict(kea_std_options(version)).get(option.name))

    def test_each_shown_option_has_a_standard_definition(self):
        for version, shown in _SHOWN_OPTIONS.items():
            self.assertLessEqual(set(form_managed_options(version).values()), set(shown))
            for option in shown:
                with self.subTest(version=version, name=option.name):
                    self.assertIn(option.name, dict(kea_std_options(version)))
