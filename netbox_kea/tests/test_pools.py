# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
import ipaddress

from django.test import SimpleTestCase

from netbox_kea import pools


class TestParsePool(SimpleTestCase):
    """One parser reads every Pool: a range or a prefix, always inside its Subnet."""

    V4 = ipaddress.ip_network("192.168.1.0/24")
    V6 = ipaddress.ip_network("2001:db8::/48")

    def test_explicit_ranges_and_prefixes_parse_to_inclusive_endpoints(self):
        for value, subnet, expected in (
            ("192.168.1.50-192.168.1.100", self.V4, "192.168.1.50-192.168.1.100"),
            ("  192.168.1.50 - 192.168.1.100  ", self.V4, "192.168.1.50-192.168.1.100"),
            ("192.168.1.128/25", self.V4, "192.168.1.128-192.168.1.255"),
            ("192.168.1.1/32", self.V4, "192.168.1.1-192.168.1.1"),
            ("2001:db8::1-2001:db8::ff", self.V6, "2001:db8::1-2001:db8::ff"),
            ("2001:db8::/64", self.V6, "2001:db8::-2001:db8::ffff:ffff:ffff:ffff"),
            ("2001:db8::1/128", self.V6, "2001:db8::1-2001:db8::1"),
        ):
            with self.subTest(value=value):
                self.assertEqual(pools.parse_pool(value, subnet).range, expected)

    def test_invalid_pools_raise_an_operator_message(self):
        for value, message in (
            (None, "Enter a Pool as a range (start-end) or a prefix (CIDR)."),
            ("  ", "Enter a Pool as a range (start-end) or a prefix (CIDR)."),
            ("not-a-pool", "Pool not-a-pool must be a range (start-end) or a prefix (CIDR)."),
            ("192.168.1.x-192.168.1.9", "Pool 192.168.1.x-192.168.1.9 has an invalid address"),
            ("192.168.1.1", "Pool 192.168.1.1 must be a range (start-end) or a prefix (CIDR)."),
            ("192.168.1.1-192.168.1.2-192.168.1.3", "must be a range (start-end) or a prefix (CIDR)."),
            ("192.168.1.1/24", "Pool 192.168.1.1/24 is not a valid prefix"),
            ("192.168.1.9-192.168.1.2", "Pool 192.168.1.9-192.168.1.2 starts after it ends."),
            ("2001:db8::1-2001:db8::2", "Pool 2001:db8::1-2001:db8::2 is not an IPv4 Pool."),
            ("192.168.2.1-192.168.2.9", "Pool 192.168.2.1-192.168.2.9 is outside Subnet 192.168.1.0/24."),
            ("192.168.1.250-192.168.2.9", "Pool 192.168.1.250-192.168.2.9 is outside Subnet 192.168.1.0/24."),
            ("192.168.0.0/23", "Pool 192.168.0.0/23 is outside Subnet 192.168.1.0/24."),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as ctx:
                pools.parse_pool(value, self.V4)
            self.assertIn(message, str(ctx.exception))

    def test_pool_contains_and_overlaps(self):
        pool = pools.parse_pool("192.168.1.10-192.168.1.20", self.V4)
        self.assertTrue(pool.contains(ipaddress.ip_address("192.168.1.10")))
        self.assertTrue(pool.contains(ipaddress.ip_address("192.168.1.20")))
        self.assertFalse(pool.contains(ipaddress.ip_address("192.168.1.21")))
        self.assertFalse(pool.contains(ipaddress.ip_address("::c0a8:10f")))
        for other, overlaps in (
            ("192.168.1.20-192.168.1.30", True),
            ("192.168.1.0/28", True),
            ("192.168.1.12-192.168.1.13", True),
            ("192.168.1.21-192.168.1.30", False),
            ("192.168.1.0-192.168.1.9", False),
        ):
            with self.subTest(other=other):
                other_pool = pools.parse_pool(other, self.V4)
                self.assertEqual(pool.overlaps(other_pool), overlaps)
                self.assertEqual(other_pool.overlaps(pool), overlaps)
        ipv6 = pools.parse_pool("::c0a8:100/120", ipaddress.ip_network("::/96"))
        self.assertFalse(pool.overlaps(ipv6))

    def test_addresses_in_pools_pairs_each_address_with_its_pool(self):
        first = pools.parse_pool("192.168.1.10-192.168.1.20", self.V4)
        second = pools.parse_pool("192.168.1.128/25", self.V4)
        addresses = [ipaddress.ip_address(value) for value in ("192.168.1.5", "192.168.1.15", "192.168.1.200")]

        self.assertEqual(
            pools.addresses_in_pools(addresses, iter((first, second))),
            ((addresses[1], first), (addresses[2], second)),
        )
        self.assertEqual(pools.addresses_in_pools(addresses, ()), ())
