import ipaddress

import requests
from django.test import SimpleTestCase, TestCase, override_settings

from netbox_kea import server_configuration
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.utils import _PLUGINS_CONFIG, _make_db_server


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerConfiguration(TestCase):
    def setUp(self):
        self.server = _make_db_server()

    def response(self, configuration, family=4):
        return {"result": 0, "arguments": {f"Dhcp{family}": configuration, "hash": "hash-a"}}

    def test_typed_server_configuration(self):
        configuration = {
            "option-data": [{"code": 6, "data": "198.18.0.53"}],
            "option-def": [
                {
                    "code": 224,
                    "name": "example",
                    "space": "dhcp4",
                    "type": "record",
                    "array": False,
                    "record-types": "uint16, string",
                    "encapsulate": "",
                }
            ],
            "shared-networks": [
                {
                    "name": "empty",
                    "user-context": {"comment": "No members"},
                    "interface": "eth0",
                    "relay": {"ip-addresses": ["198.18.0.1"]},
                    "option-data": [{"code": 3, "data": "198.18.0.1"}],
                },
                {"name": "access", "subnet4": [{"id": 1, "subnet": "198.18.1.0/24"}]},
            ],
        }
        with stub_kea({"config-get": self.response(configuration)}) as kea:
            snapshot = server_configuration.display(self.server, 4)
        self.assertTrue(snapshot.available)
        self.assertTrue(snapshot.complete)
        self.assertIsNotNone(snapshot.observed_at.tzinfo)
        self.assertEqual(snapshot.server_id, self.server.pk)
        self.assertEqual(snapshot.configuration_hash, "hash-a")
        self.assertEqual(snapshot.global_options[0].code, 6)
        empty, access = snapshot.shared_networks
        self.assertEqual(empty.member_cidrs, ())
        self.assertEqual(empty.description, "No members")
        self.assertEqual(empty.interface, "eth0")
        self.assertEqual(empty.relay_addresses, (ipaddress.ip_address("198.18.0.1"),))
        self.assertEqual(empty.options[0].code, 3)
        self.assertEqual(access.member_cidrs, ("198.18.1.0/24",))
        self.assertEqual(snapshot.subnets[0].shared_network_name, "access")
        self.assertEqual(snapshot.option_definitions[0].record_types, ("uint16", "string"))
        self.assertTrue(snapshot.global_options_complete)
        self.assertTrue(empty.complete)
        self.assertTrue(access.complete)
        self.assertIsNone(snapshot.option_definitions[0].encapsulate)
        self.assertEqual(kea.commands(), ["config-get"])

    def test_per_collection_completeness_survives_one_invalid_entry(self):
        configuration = {
            "option-data": [{"code": 6, "data": "198.18.0.53"}, {"data": "no identity"}],
            "shared-networks": [
                {"name": "broken", "option-data": None, "subnet4": []},
                {"name": "fine", "subnet4": [{"id": 7, "subnet": "198.18.7.0/24"}]},
            ],
        }
        with stub_kea({"config-get": self.response(configuration)}):
            snapshot = server_configuration.display(self.server, 4)

        self.assertTrue(snapshot.available)
        self.assertFalse(snapshot.complete)
        self.assertFalse(snapshot.global_options_complete)
        self.assertEqual([option.code for option in snapshot.global_options], [6])
        self.assertEqual(len(snapshot.shared_networks), 2, snapshot.diagnostics)
        broken, fine = snapshot.shared_networks
        self.assertFalse(broken.complete)
        self.assertEqual(broken.options, ())
        self.assertTrue(fine.complete)

    def test_invalid_facts_survive_and_cache(self):
        configuration = {
            "subnet4": [{"id": 1, "subnet": "198.18.1.0/24", "pools": [{"pool": "bad"}]}],
            "option-data": [False, {"code": 6}],
            "shared-networks": [False, {"name": "empty"}],
            "option-def": [False, {"code": 224, "name": "example", "type": "string"}],
        }
        with stub_kea({"config-get": self.response(configuration)}) as kea:
            snapshot = server_configuration.display(self.server, 4)
            self.assertEqual(server_configuration.display(self.server, 4), snapshot)
        self.assertTrue(snapshot.available)
        self.assertFalse(snapshot.complete)
        self.assertEqual(len(snapshot.subnets), 1)
        self.assertEqual(len(snapshot.global_options), 1)
        self.assertEqual(len(snapshot.shared_networks), 1)
        self.assertEqual(len(snapshot.option_definitions), 1)
        self.assertGreaterEqual(len(snapshot.diagnostics), 4)
        self.assertEqual(kea.commands(), ["config-get"])

    def test_live_read_and_invalidation(self):
        with stub_kea({"config-get": self.response({})}) as kea:
            server_configuration.display(self.server, 4)
            server_configuration.for_verification(self.server, 4)
            server_configuration.invalidate(self.server, 4)
            server_configuration.display(self.server, 4)
        self.assertEqual(kea.commands(), ["config-get"] * 3)

    def test_public_operations_reject_an_invalid_family(self):
        with self.assertRaisesMessage(ValueError, "family must be 4 or 6"):
            server_configuration.display(self.server, 5)

    def test_unavailable_has_no_facts(self):
        with stub_kea({"config-get": requests.ConnectionError("unreachable")}):
            snapshot = server_configuration.display(self.server, 4)
        self.assertFalse(snapshot.available)
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.subnets, ())
        self.assertEqual(snapshot.shared_networks, ())
        self.assertTrue(snapshot.diagnostics)

    def test_dhcpv6_and_missing_declared_id(self):
        configuration = {
            "subnet6": [{"subnet": "2001:db8:1::/64", "pools": [{"pool": "2001:db8:1::10-2001:db8:1::20"}]}],
            "shared-networks": [{"name": "access", "relay": {"ip-addresses": ["2001:db8::1", "198.18.0.1"]}}],
            "option-data": [{"code": 23, "data": "2001:db8::53"}],
        }
        with stub_kea({"config-get": self.response(configuration, 6)}):
            snapshot = server_configuration.for_verification(self.server, 6)
        self.assertEqual(snapshot.family, 6)
        self.assertIsNone(snapshot.subnets[0].declared_subnet_id)
        self.assertEqual(snapshot.subnets[0].configuration.pools[0].range, "2001:db8:1::10-2001:db8:1::20")
        self.assertEqual(snapshot.shared_networks[0].relay_addresses, (ipaddress.ip_address("2001:db8::1"),))
        self.assertFalse(snapshot.complete)

    def test_malformed_envelopes_have_no_facts(self):
        for response in ([], {"result": 0}, {"result": 0, "arguments": {"Dhcp4": []}}):
            with self.subTest(response=response), stub_kea({"config-get": response}):
                snapshot = server_configuration.for_verification(self.server, 4)
                self.assertFalse(snapshot.available)
                self.assertEqual(snapshot.subnets, ())
                self.assertEqual(snapshot.global_options, ())
                self.assertEqual(snapshot.diagnostics[0].code, "malformed-configuration-response")

    def test_invalidation_expires_both_display_caches(self):
        from netbox_kea import subnet_catalogue

        subnet = {"id": 1, "subnet": "198.18.1.0/24"}
        with stub_kea(
            {
                "config-get": self.response({"subnet4": [subnet]}),
                "subnet4-list": {"result": 0, "arguments": {"subnets": [subnet]}},
            }
        ) as kea:
            server_configuration.display(self.server, 4)
            subnet_catalogue.display(self.server, 4)
            server_configuration.display(self.server, 4)
            subnet_catalogue.display(self.server, 4)
            self.assertEqual(kea.commands().count("config-get"), 2)
            server_configuration.invalidate(self.server, 4)
            server_configuration.display(self.server, 4)
            subnet_catalogue.display(self.server, 4)
        self.assertEqual(kea.commands().count("config-get"), 4)
        self.assertEqual(kea.commands().count("subnet4-list"), 2)

    def test_invalid_member_collection_preserves_shared_network_facts(self):
        shared = {
            "name": "access",
            "user-context": {"comment": "Access network"},
            "interface": "eth0",
            "relay": {"ip-addresses": ["198.18.0.1"]},
            "option-data": [{"code": 3, "data": "198.18.0.1"}],
            "subnet4": False,
        }
        with stub_kea({"config-get": self.response({"shared-networks": [shared]})}):
            snapshot = server_configuration.display(self.server, 4)
        self.assertTrue(snapshot.available)
        self.assertFalse(snapshot.complete)
        self.assertEqual(len(snapshot.shared_networks), 1)
        network = snapshot.shared_networks[0]
        self.assertEqual(network.name, "access")
        self.assertEqual(network.description, "Access network")
        self.assertEqual(network.interface, "eth0")
        self.assertEqual(network.relay_addresses, (ipaddress.ip_address("198.18.0.1"),))
        self.assertEqual(network.options[0].code, 3)
        self.assertEqual(network.member_cidrs, ())
        self.assertFalse(network.complete)
        self.assertEqual(snapshot.diagnostics[0].code, "invalid-subnet-collection")

    def test_transport_oserror_returns_unavailable_snapshot(self):
        with stub_kea({"config-get": OSError("TLS certificate file unavailable")}):
            snapshot = server_configuration.display(self.server, 4)
        self.assertFalse(snapshot.available)
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.subnets, ())
        self.assertTrue(snapshot.diagnostics)

    def test_invalid_shared_names_block_membership_changes(self):
        for networks in ([{"name": ""}], [{"name": "duplicate"}, {"name": "duplicate"}]):
            with (
                self.subTest(networks=networks),
                stub_kea({"config-get": self.response({"shared-networks": networks})}),
            ):
                snapshot = server_configuration.for_verification(self.server, 4)
                self.assertFalse(snapshot.shared_networks_complete)

    def test_invalid_members_mark_only_their_network_incomplete(self):
        for members in ([False], [{"id": True, "subnet": "198.18.1.0/24"}], [{"id": 1, "subnet": "bad"}]):
            networks = [{"name": "broken", "subnet4": members}, {"name": "fine", "subnet4": []}]
            with self.subTest(members=members), stub_kea({"config-get": self.response({"shared-networks": networks})}):
                snapshot = server_configuration.for_verification(self.server, 4)
                self.assertFalse(snapshot.shared_networks[0].complete)
                self.assertTrue(snapshot.shared_networks[1].complete)


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
                self.assertEqual(server_configuration.parse_pool(value, subnet).range, expected)

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
                server_configuration.parse_pool(value, self.V4)
            self.assertIn(message, str(ctx.exception))

    def test_pool_contains_and_overlaps(self):
        pool = server_configuration.parse_pool("192.168.1.10-192.168.1.20", self.V4)
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
                other_pool = server_configuration.parse_pool(other, self.V4)
                self.assertEqual(pool.overlaps(other_pool), overlaps)
                self.assertEqual(other_pool.overlaps(pool), overlaps)
        ipv6 = server_configuration.parse_pool("::c0a8:100/120", ipaddress.ip_network("::/96"))
        self.assertFalse(pool.overlaps(ipv6))

    def test_addresses_in_pools_pairs_each_address_with_its_pool(self):
        first = server_configuration.parse_pool("192.168.1.10-192.168.1.20", self.V4)
        second = server_configuration.parse_pool("192.168.1.128/25", self.V4)
        addresses = [ipaddress.ip_address(value) for value in ("192.168.1.5", "192.168.1.15", "192.168.1.200")]

        self.assertEqual(
            server_configuration.addresses_in_pools(addresses, iter((first, second))),
            ((addresses[1], first), (addresses[2], second)),
        )
        self.assertEqual(server_configuration.addresses_in_pools(addresses, ()), ())
