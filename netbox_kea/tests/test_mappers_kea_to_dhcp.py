# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the pure Kea-config → DHCP-plugin intent mapper.

These exercise only ``netbox_kea.mappers.kea_to_dhcp`` — no DB, no ORM, and no
``netbox_dhcp``; they run wherever NetBox is importable (CI + devcontainer).
"""

from __future__ import annotations

from django.test import SimpleTestCase

from netbox_kea.mappers.kea_to_dhcp import (
    RESERVATION_IDENTIFIER_TYPES,
    OptionIntent,
    parse_dhcp_config,
)


class TestParseDhcpConfigStructure(SimpleTestCase):
    """Subnet / shared-network grouping and basic field extraction."""

    def test_standalone_subnet_has_no_shared_network(self):
        conf = {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}
        result = parse_dhcp_config(conf, 4)
        self.assertEqual(len(result.subnets), 1)
        subnet = result.subnets[0]
        self.assertEqual(subnet.kea_subnet_id, 1)
        self.assertEqual(subnet.cidr, "10.0.0.0/24")
        self.assertEqual(subnet.family, 4)
        self.assertIsNone(subnet.shared_network)
        self.assertEqual(result.shared_networks, [])

    def test_shared_network_subnet_carries_network_name(self):
        conf = {
            "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}],
            "shared-networks": [
                {"name": "office", "subnet4": [{"id": 2, "subnet": "10.0.1.0/24"}]},
            ],
        }
        result = parse_dhcp_config(conf, 4)
        self.assertEqual({sn.name for sn in result.shared_networks}, {"office"})
        by_id = {s.kea_subnet_id: s for s in result.subnets}
        self.assertIsNone(by_id[1].shared_network)
        self.assertEqual(by_id[2].shared_network, "office")

    def test_v6_uses_subnet6_key_and_family_6(self):
        conf = {"subnet6": [{"id": 7, "subnet": "2001:db8::/64"}]}
        result = parse_dhcp_config(conf, 6)
        self.assertEqual(len(result.subnets), 1)
        self.assertEqual(result.subnets[0].family, 6)
        self.assertEqual(result.subnets[0].kea_subnet_id, 7)

    def test_subnet4_key_ignored_when_parsing_v6(self):
        conf = {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}
        self.assertEqual(parse_dhcp_config(conf, 6).subnets, [])

    def test_string_subnet_id_is_coerced_to_int(self):
        conf = {"subnet4": [{"id": "5", "subnet": "10.0.0.0/24"}]}
        self.assertEqual(parse_dhcp_config(conf, 4).subnets[0].kea_subnet_id, 5)

    def test_unparseable_subnet_id_becomes_none(self):
        conf = {"subnet4": [{"id": "abc", "subnet": "10.0.0.0/24"}]}
        self.assertIsNone(parse_dhcp_config(conf, 4).subnets[0].kea_subnet_id)


class TestParseDhcpConfigPools(SimpleTestCase):
    def test_pools_extracted_in_order(self):
        conf = {
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.0.0.0/24",
                    "pools": [{"pool": "10.0.0.10-10.0.0.99"}, {"pool": "10.0.0.128/25"}],
                }
            ]
        }
        pools = parse_dhcp_config(conf, 4).subnets[0].pools
        self.assertEqual([p.pool for p in pools], ["10.0.0.10-10.0.0.99", "10.0.0.128/25"])

    def test_pool_match_key_is_stripped(self):
        conf = {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": "  10.0.0.10-10.0.0.99 "}]}]}
        self.assertEqual(parse_dhcp_config(conf, 4).subnets[0].pools[0].match_key, "10.0.0.10-10.0.0.99")

    def test_empty_and_non_dict_pool_entries_skipped(self):
        conf = {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": ""}, "garbage", {}]}]}
        self.assertEqual(parse_dhcp_config(conf, 4).subnets[0].pools, ())


class TestParseDhcpConfigReservations(SimpleTestCase):
    def test_v4_reservation_hw_address_and_ip(self):
        conf = {
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.0.0.0/24",
                    "reservations": [
                        {"hw-address": "00:11:22:33:44:55", "ip-address": "10.0.0.50", "hostname": "host-a"}
                    ],
                }
            ]
        }
        res = parse_dhcp_config(conf, 4).subnets[0].reservations[0]
        self.assertEqual(res.identifier_type, "hw-address")
        self.assertEqual(res.identifier, "00:11:22:33:44:55")
        self.assertEqual(res.ip_address, "10.0.0.50")
        self.assertEqual(res.hostname, "host-a")
        self.assertEqual(res.all_addresses, ("10.0.0.50",))
        self.assertEqual(res.match_key, ("hw-address", "00:11:22:33:44:55"))

    def test_v6_reservation_duid_addresses_and_prefixes(self):
        conf = {
            "subnet6": [
                {
                    "id": 1,
                    "subnet": "2001:db8::/64",
                    "reservations": [
                        {
                            "duid": "01:02:03:04",
                            "ip-addresses": ["2001:db8::5", "2001:db8::6"],
                            "prefixes": ["2001:db8:1::/48"],
                            "hostname": "host-v6",
                        }
                    ],
                }
            ]
        }
        res = parse_dhcp_config(conf, 6).subnets[0].reservations[0]
        self.assertEqual(res.identifier_type, "duid")
        self.assertIsNone(res.ip_address)
        self.assertEqual(res.ip_addresses, ("2001:db8::5", "2001:db8::6"))
        self.assertEqual(res.prefixes, ("2001:db8:1::/48",))
        self.assertEqual(res.all_addresses, ("2001:db8::5", "2001:db8::6"))

    def test_identifier_priority_prefers_hw_address_over_others(self):
        # hw-address comes first in Kea's priority order even if others are present.
        conf = {
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.0.0.0/24",
                    "reservations": [
                        {"client-id": "aa:bb", "hw-address": "00:11:22:33:44:55", "ip-address": "10.0.0.9"}
                    ],
                }
            ]
        }
        res = parse_dhcp_config(conf, 4).subnets[0].reservations[0]
        self.assertEqual(res.identifier_type, "hw-address")

    def test_identifier_priority_order_matches_constant(self):
        # circuit-id wins only when hw-address and duid are absent.
        conf = {
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.0.0.0/24",
                    "reservations": [{"flex-id": "f", "circuit-id": "c", "ip-address": "10.0.0.9"}],
                }
            ]
        }
        res = parse_dhcp_config(conf, 4).subnets[0].reservations[0]
        # circuit-id precedes flex-id in the documented order.
        self.assertLess(
            RESERVATION_IDENTIFIER_TYPES.index("circuit-id"),
            RESERVATION_IDENTIFIER_TYPES.index("flex-id"),
        )
        self.assertEqual(res.identifier_type, "circuit-id")

    def test_reservation_without_identifier_has_none_key(self):
        conf = {
            "subnet4": [
                {"id": 1, "subnet": "10.0.0.0/24", "reservations": [{"ip-address": "10.0.0.9", "hostname": "h"}]}
            ]
        }
        res = parse_dhcp_config(conf, 4).subnets[0].reservations[0]
        self.assertEqual(res.match_key, (None, None))


class TestParseDhcpConfigOptions(SimpleTestCase):
    def test_subnet_options_normalized(self):
        conf = {
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.0.0.0/24",
                    "option-data": [
                        {"code": 3, "name": "routers", "data": "10.0.0.1", "space": "dhcp4", "always-send": True}
                    ],
                }
            ]
        }
        opt = parse_dhcp_config(conf, 4).subnets[0].options[0]
        self.assertEqual(
            opt, OptionIntent(code=3, name="routers", space="dhcp4", data="10.0.0.1", csv_format=None, always_send=True)
        )
        self.assertEqual(opt.match_key, ("dhcp4", 3))

    def test_option_match_key_falls_back_to_name_without_code(self):
        conf = {
            "subnet4": [
                {"id": 1, "subnet": "10.0.0.0/24", "option-data": [{"name": "custom", "data": "x", "space": "dhcp4"}]}
            ]
        }
        self.assertEqual(parse_dhcp_config(conf, 4).subnets[0].options[0].match_key, ("dhcp4", "custom"))

    def test_shared_network_options_captured(self):
        conf = {
            "shared-networks": [
                {"name": "n", "subnet4": [], "option-data": [{"code": 6, "data": "1.1.1.1", "space": "dhcp4"}]}
            ]
        }
        self.assertEqual(parse_dhcp_config(conf, 4).shared_networks[0].options[0].code, 6)


class TestParseDhcpConfigRobustness(SimpleTestCase):
    def test_subnet_without_cidr_is_skipped(self):
        conf = {"subnet4": [{"id": 1}, {"id": 2, "subnet": "10.0.0.0/24"}]}
        result = parse_dhcp_config(conf, 4)
        self.assertEqual([s.kea_subnet_id for s in result.subnets], [2])

    def test_non_dict_subnet_entries_skipped(self):
        conf = {"subnet4": ["nope", None, {"id": 1, "subnet": "10.0.0.0/24"}]}
        self.assertEqual(len(parse_dhcp_config(conf, 4).subnets), 1)

    def test_shared_network_without_name_skipped(self):
        conf = {"shared-networks": [{"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}]}
        result = parse_dhcp_config(conf, 4)
        self.assertEqual(result.shared_networks, [])
        # Its subnets are dropped too (no parent name to attach them to).
        self.assertEqual(result.subnets, [])

    def test_non_dict_conf_returns_empty(self):
        result = parse_dhcp_config("not a dict", 4)
        self.assertEqual(result.subnets, [])
        self.assertEqual(result.shared_networks, [])

    def test_invalid_version_raises(self):
        with self.assertRaises(ValueError):
            parse_dhcp_config({}, 5)
