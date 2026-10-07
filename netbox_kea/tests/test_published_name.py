# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The published-name rule against the names that a live Kea 3.2.0 gave clients (issue #304)."""

from django.test import SimpleTestCase

from netbox_kea.published_name import lease_published_name, published_name, stored_hostname

# Observed on Kea 3.2.0 with ddns-qualifying-suffix "dhcp.example.com".
_OBSERVED = (
    ("host", "host.dhcp.example.com"),
    ("host.example.org", "host.example.org.dhcp.example.com"),
    ("host.example.org.", "host.example.org"),
    ("host.dhcp.example.com", "host.dhcp.example.com"),
)


class TestPublishedName(SimpleTestCase):
    def test_observed_kea_names(self):
        for stored, expected in _OBSERVED:
            for suffix in ("dhcp.example.com", "dhcp.example.com."):
                with self.subTest(stored=stored, suffix=suffix):
                    self.assertEqual(published_name(stored, suffix), expected)

    def test_an_empty_suffix_publishes_the_stored_name_without_a_trailing_dot(self):
        self.assertEqual(published_name("host", ""), "host")
        self.assertEqual(published_name("host.example.org", ""), "host.example.org")
        self.assertEqual(published_name("host.example.org.", ""), "host.example.org")

    def test_an_empty_hostname_publishes_no_name(self):
        self.assertEqual(published_name("", "dhcp.example.com"), "")

    def test_the_suffix_match_is_case_sensitive_and_the_result_is_lower_case(self):
        # Kea compares the suffix byte by byte, then lowers the whole name.
        self.assertEqual(published_name("host", "DHCP.Example.com"), "host.dhcp.example.com")
        self.assertEqual(published_name("Host.DHCP.example.com", "DHCP.example.com"), "host.dhcp.example.com")
        self.assertEqual(
            published_name("host.DHCP.example.com", "dhcp.example.com"),
            "host.dhcp.example.com.dhcp.example.com",
        )

    def test_the_suffix_must_start_at_a_label(self):
        self.assertEqual(published_name("foo.barexample.com", "example.com"), "foo.barexample.com.example.com")
        self.assertEqual(published_name("example.com", "example.com"), "example.com")


class TestStoredHostname(SimpleTestCase):
    def test_the_stored_form_publishes_the_entered_name(self):
        for name in (
            "host.dhcp.example.com",
            "host.example.org",
            "a.b.dhcp.example.com",
            "dhcp.example.com",
            "dhcp.example.com.dhcp.example.com",
            "Host.DHCP.Example.com",
        ):
            for suffix in ("dhcp.example.com", "dhcp.example.com.", "DHCP.example.com", ""):
                with self.subTest(name=name, suffix=suffix):
                    self.assertEqual(published_name(stored_hostname(name, suffix), suffix), name.lower())

    def test_the_stored_forms(self):
        self.assertEqual(stored_hostname("host.dhcp.example.com", "dhcp.example.com."), "host")
        self.assertEqual(stored_hostname("host.DHCP.example.com", "dhcp.example.com"), "host")
        self.assertEqual(stored_hostname("host.example.org", "dhcp.example.com"), "host.example.org.")
        self.assertEqual(stored_hostname("host.example.org.", "dhcp.example.com"), "host.example.org.")
        self.assertEqual(stored_hostname("host.example.org", ""), "host.example.org")
        self.assertEqual(stored_hostname("host.example.org.", ""), "host.example.org")
        self.assertEqual(stored_hostname("", "dhcp.example.com"), "")

    def test_a_single_label_is_stored_as_entered_so_kea_qualifies_it(self):
        self.assertEqual(stored_hostname("host", "dhcp.example.com"), "host")
        self.assertEqual(
            published_name(stored_hostname("host", "dhcp.example.com"), "dhcp.example.com"), "host.dhcp.example.com"
        )


class TestLeasePublishedName(SimpleTestCase):
    def test_the_trailing_dot_of_a_lease_hostname_is_dropped(self):
        self.assertEqual(lease_published_name("host.dhcp.example.com."), "host.dhcp.example.com")
        self.assertEqual(lease_published_name("host.dhcp.example.com"), "host.dhcp.example.com")
        self.assertEqual(lease_published_name(""), "")
