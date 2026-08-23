import ipaddress
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from netbox_kea.models import Server
from netbox_kea.subnet_catalogue import (
    MAX_SUBNET_ID,
    CatalogueUnavailable,
    CompleteCatalogueSnapshot,
    ConfigurationOnlyCatalogueSnapshot,
    IdentityOnlyCatalogueSnapshot,
    IncompleteCatalogueSnapshot,
    SubnetIdentityConflict,
    SubnetIdExhausted,
    display,
    for_synchronization,
    mutation,
)
from netbox_kea.tests.kea_stub import _subnet_list, queued, stub_kea
from netbox_kea.tests.utils import _PLUGINS_CONFIG, _drop_subnet_choices_cache, _make_db_server


def _identity(version, subnets, *, result=0):
    if result != 0:
        return {"result": result, "text": "subnet command unavailable"}
    return _subnet_list(version, subnets)


def _config(version, subnets, *, shared_networks=None, config_hash="hash-a", result=0):
    if result != 0:
        return {"result": result, "text": "configuration unavailable"}
    return {
        "result": 0,
        "arguments": {
            f"Dhcp{version}": {
                f"subnet{version}": list(subnets),
                "shared-networks": list(shared_networks or []),
            },
            "hash": config_hash,
        },
    }


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetCatalogue(TestCase):
    def setUp(self):
        self.server = _make_db_server()
        _drop_subnet_choices_cache(self, self.server)

    def test_display_reconciles_typed_configuration(self):
        identities = _identity(
            4,
            [
                {"id": 2, "subnet": "198.18.2.0/24", "shared-network-name": None},
                {"id": 1, "subnet": "198.18.1.0/24", "shared-network-name": "access"},
            ],
        )
        configuration = _config(
            4,
            [
                {
                    "id": 2,
                    "subnet": "198.18.2.0/24",
                    "pools": [{"pool": "198.18.2.64/26"}],
                    "option-data": [{"name": "domain-name-servers", "code": 6, "data": "198.18.0.53"}],
                    "valid-lifetime": 3600,
                    "ddns-qualifying-suffix": "example.invalid",
                }
            ],
            shared_networks=[
                {
                    "name": "access",
                    "subnet4": [
                        {
                            "id": 1,
                            "subnet": "198.18.1.0/24",
                            "pools": [{"pool": "198.18.1.10 - 198.18.1.20"}],
                            "option-data": [],
                        }
                    ],
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}) as kea:
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, CompleteCatalogueSnapshot)
        self.assertEqual(kea.commands(), ["subnet4-list", "config-get"])
        self.assertIsNotNone(snapshot.observed_at.tzinfo)
        self.assertEqual(snapshot.subnet_choices, (("198.18.1.0/24", 1), ("198.18.2.0/24", 2)))

        shared = snapshot.find_by_id(1)
        self.assertEqual(shared.shared_network.name, "access")
        self.assertEqual(shared.configuration.pools[0].start, ipaddress.ip_address("198.18.1.10"))
        self.assertEqual(shared.configuration.pools[0].end, ipaddress.ip_address("198.18.1.20"))

        standalone = snapshot.find_by_cidr("198.18.2.0/24")
        self.assertIsNone(standalone.shared_network)
        self.assertEqual(standalone.configuration.pools[0].start, ipaddress.ip_address("198.18.2.64"))
        self.assertEqual(standalone.configuration.pools[0].end, ipaddress.ip_address("198.18.2.127"))
        self.assertEqual(standalone.configuration.options[0].name, "domain-name-servers")
        self.assertEqual(standalone.configuration.settings.valid_lifetime, 3600)
        self.assertEqual(standalone.configuration.settings.ddns_qualifying_suffix, "example.invalid")

    def test_display_reconciles_typed_dhcpv6_configuration(self):
        identities = _identity(
            6,
            [
                {"id": 2, "subnet": "2001:db8:2::/64", "shared-network-name": None},
                {"id": 1, "subnet": "2001:db8:1::/64", "shared-network-name": "access"},
            ],
        )
        configuration = _config(
            6,
            [
                {
                    "id": 2,
                    "subnet": "2001:db8:2::/64",
                    "pools": [{"pool": "2001:db8:2::/80"}],
                    "option-data": [{"name": "dns-servers", "code": 23, "data": "2001:db8::53"}],
                    "preferred-lifetime": 1800,
                    "min-preferred-lifetime": 900,
                    "max-preferred-lifetime": 2700,
                    "pd-allocator": "iterative",
                    "interface-id": "eth0-v6",
                    "relay": {"ip-addresses": ["2001:db8::1"]},
                }
            ],
            shared_networks=[
                {
                    "name": "access",
                    "subnet6": [
                        {
                            "id": 1,
                            "subnet": "2001:db8:1::/64",
                            "pools": [{"pool": "2001:db8:1::10 - 2001:db8:1::20"}],
                            "option-data": [],
                        }
                    ],
                }
            ],
        )

        with stub_kea({"subnet6-list": identities, "config-get": configuration}) as kea:
            snapshot = display(self.server, 6)

        self.assertIsInstance(snapshot, CompleteCatalogueSnapshot)
        self.assertEqual(kea.commands(), ["subnet6-list", "config-get"])
        self.assertEqual(snapshot.subnet_choices, (("2001:db8:1::/64", 1), ("2001:db8:2::/64", 2)))

        shared = snapshot.find_by_id(1)
        self.assertEqual(shared.shared_network.name, "access")
        self.assertEqual(shared.configuration.pools[0].start, ipaddress.ip_address("2001:db8:1::10"))
        self.assertEqual(shared.configuration.pools[0].end, ipaddress.ip_address("2001:db8:1::20"))

        standalone = snapshot.find_by_cidr("2001:db8:2::/64")
        self.assertIsNone(standalone.shared_network)
        self.assertEqual(standalone.configuration.pools[0].start, ipaddress.ip_address("2001:db8:2::"))
        self.assertEqual(
            standalone.configuration.pools[0].end,
            ipaddress.ip_address("2001:db8:2::ffff:ffff:ffff"),
        )
        self.assertEqual(standalone.configuration.options[0].name, "dns-servers")
        settings = standalone.configuration.settings
        self.assertEqual(settings.preferred_lifetime, 1800)
        self.assertEqual(settings.min_preferred_lifetime, 900)
        self.assertEqual(settings.max_preferred_lifetime, 2700)
        self.assertEqual(settings.pd_allocator, "iterative")
        self.assertEqual(settings.interface_id, "eth0-v6")
        self.assertEqual(settings.relay_addresses, (ipaddress.ip_address("2001:db8::1"),))

    def _dhcpv6_relay_snapshot(self, relay_addresses):
        identities = _identity(6, [{"id": 1, "subnet": "2001:db8:1::/64", "shared-network-name": None}])
        configuration = _config(
            6,
            [
                {
                    "id": 1,
                    "subnet": "2001:db8:1::/64",
                    # Valid IPv6 pool: the relay must be the only invalid input, so the
                    # snapshot class below can only be explained by the relay.
                    "pools": [{"pool": "2001:db8:1::/80"}],
                    "option-data": [],
                    "relay": {"ip-addresses": relay_addresses},
                }
            ],
        )
        with stub_kea({"subnet6-list": identities, "config-get": configuration}):
            return display(self.server, 6)

    def test_dhcpv6_relay_drops_addresses_from_the_wrong_family(self):
        snapshot = self._dhcpv6_relay_snapshot(["198.18.1.1", "2001:db8::1"])

        # An omitted invalid setting must not leave the catalogue authoritative, or
        # synchronization would treat the surviving configuration as complete.
        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertIn("invalid-setting", {diagnostic.code for diagnostic in snapshot.diagnostics})
        subnet = snapshot.find_by_id(1)
        self.assertEqual(subnet.configuration.settings.relay_addresses, (ipaddress.ip_address("2001:db8::1"),))
        # The valid pool survives, so the diagnostic above is the relay's alone.
        self.assertEqual(subnet.configuration.pools[0].start, ipaddress.ip_address("2001:db8:1::"))

    def test_dhcpv6_relay_of_the_right_family_keeps_the_catalogue_complete(self):
        snapshot = self._dhcpv6_relay_snapshot(["2001:db8::1"])

        self.assertIsInstance(snapshot, CompleteCatalogueSnapshot)
        self.assertEqual(snapshot.diagnostics, ())
        self.assertEqual(
            snapshot.find_by_id(1).configuration.settings.relay_addresses,
            (ipaddress.ip_address("2001:db8::1"),),
        )

    def test_dhcpv4_relay_rejects_non_string_address(self):
        # Only DHCPv4 reaches this bug: ``1`` parses as an IPv4 address, so the
        # wrong-family check already rejects it for DHCPv6.
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24", "shared-network-name": None}])
        configuration = _config(
            4,
            [
                {
                    "id": 1,
                    "subnet": "198.18.1.0/24",
                    "pools": [{"pool": "198.18.1.0/26"}],
                    "option-data": [],
                    "relay": {"ip-addresses": [1]},
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertEqual(snapshot.find_by_id(1).configuration.settings.relay_addresses, ())
        self.assertIn(
            ("invalid-setting", "subnet4[0].relay.ip-addresses"),
            {(diagnostic.code, diagnostic.path) for diagnostic in snapshot.diagnostics},
        )

    def test_public_operations_reject_invalid_scope(self):
        with self.assertRaisesMessage(ValueError, "family must be 4 or 6"):
            display(self.server, 5)
        with self.assertRaisesMessage(ValueError, "requires a persisted Server"):
            display(Server(), 4)
        with self.assertRaisesMessage(ValueError, "requires a persisted Server"):
            for_synchronization(Server(), 4)

    def test_display_reports_malformed_source_envelopes(self):
        cases = (
            (
                {"subnet4-list": {"result": 1, "text": "failed"}, "config-get": _config(4, [])},
                {"identity-unavailable"},
            ),
            (
                {"subnet4-list": [], "config-get": []},
                {"malformed-identity-response", "malformed-configuration-response"},
            ),
            (
                {
                    "subnet4-list": {"result": 0, "arguments": {"subnets": "not-a-list"}},
                    "config-get": {"result": 0, "arguments": {"Other": {}}},
                },
                {"malformed-identity-response", "malformed-configuration-response"},
            ),
        )

        for responses, expected_codes in cases:
            with self.subTest(expected_codes=expected_codes), stub_kea(responses):
                snapshot = display(self.server, 4)
                self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
                self.assertTrue(expected_codes.issubset({diagnostic.code for diagnostic in snapshot.diagnostics}))

    def test_display_omits_malformed_identities_but_keeps_valid_subnet(self):
        identities = _identity(
            4,
            [
                "not-an-object",
                {"id": True, "subnet": "198.18.2.0/24"},
                {"id": 2, "subnet": "198.18.2.1/24"},
                {"id": 1, "subnet": "198.18.1.0/24", "shared-network-name": []},
            ],
        )
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertEqual(snapshot.subnet_choices, (("198.18.1.0/24", 1),))
        self.assertIsNone(snapshot.subnets[0].shared_network)
        self.assertTrue(
            {
                "invalid-subnet",
                "invalid-subnet-id",
                "invalid-subnet-cidr",
                "invalid-shared-network-membership",
            }.issubset({diagnostic.code for diagnostic in snapshot.diagnostics})
        )

    def test_display_omits_invalid_nested_configuration_facts(self):
        identities = _identity(
            4,
            [
                {"id": 1, "subnet": "198.18.1.0/24"},
                {"id": 2, "subnet": "198.18.2.0/24"},
            ],
        )
        configuration = _config(
            4,
            [
                {
                    "id": 1,
                    "subnet": "198.18.1.0/24",
                    "pools": [
                        {},
                        {"pool": "198.18.3.0/24"},
                        {"pool": "2001:db8::1-2001:db8::2"},
                        {"pool": "198.18.2.1-198.18.2.2"},
                        {"pool": "198.18.1.10-198.18.1.20"},
                    ],
                    "option-data": [
                        "not-an-object",
                        {"code": True, "data": "invalid"},
                        {"name": "", "data": "invalid"},
                        {},
                        {"name": "routers", "space": "", "data": "invalid"},
                        {"name": "routers", "data": []},
                        {"name": "routers", "data": "198.18.1.1", "csv-format": "yes"},
                        {"name": "routers", "data": "198.18.1.1"},
                    ],
                    "valid-lifetime": -1,
                    "allocator": "",
                    "relay": {"ip-addresses": ["invalid", "2001:db8::1", "198.18.1.1"]},
                    "client-classes": "not-a-list",
                    "require-client-classes": ["known-client"],
                },
                {
                    "id": 2,
                    "subnet": "198.18.2.0/24",
                    "pools": "not-a-list",
                    "option-data": "not-a-list",
                    "relay": [],
                },
                "not-a-subnet",
            ],
            shared_networks=[
                "not-a-network",
                {"name": "broken", "subnet4": "not-a-list"},
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        first = snapshot.find_by_id(1)
        self.assertEqual(first.configuration.pools[0].range, "198.18.1.10-198.18.1.20")
        self.assertEqual(first.configuration.options[0].name, "routers")
        self.assertIsNone(first.configuration.settings.valid_lifetime)
        self.assertIsNone(first.configuration.settings.allocator)
        self.assertEqual(first.configuration.settings.relay_addresses, (ipaddress.ip_address("198.18.1.1"),))
        self.assertEqual(first.configuration.settings.require_client_classes, ("known-client",))
        self.assertEqual(snapshot.find_by_id(2).configuration.pools, ())
        self.assertTrue(
            {
                "invalid-pool",
                "invalid-pool-collection",
                "invalid-option",
                "invalid-option-collection",
                "invalid-setting",
                "invalid-subnet",
                "invalid-shared-network",
                "invalid-subnet-collection",
            }.issubset({diagnostic.code for diagnostic in snapshot.diagnostics})
        )

    def test_display_reports_invalid_top_level_configuration_collections(self):
        configuration = {
            "result": 0,
            "arguments": {"Dhcp4": {"subnet4": {}, "shared-networks": {}}},
        }

        with stub_kea(
            {
                "subnet4-list": {"result": 3, "text": "no subnets"},
                "config-get": configuration,
            }
        ):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertFalse(snapshot.subnets)
        self.assertFalse(snapshot.unavailable)
        self.assertEqual(
            {diagnostic.code for diagnostic in snapshot.diagnostics},
            {"invalid-subnet-collection", "invalid-shared-network-collection"},
        )

    def test_display_returns_identity_only_snapshot_when_config_fails(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])

        with stub_kea({"subnet4-list": identities, "config-get": requests.ConnectionError("down")}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IdentityOnlyCatalogueSnapshot)
        self.assertEqual(snapshot.subnet_choices, (("198.18.1.0/24", 1),))
        self.assertIsNone(snapshot.subnets[0].configuration)
        self.assertIn("configuration-unavailable", {diagnostic.code for diagnostic in snapshot.diagnostics})

    def test_current_additional_classes_key_is_read(self):
        """Read `evaluate-additional-classes`, the name Kea 3.0 renamed the old key to.

        Kea 3.2 answers a configuration that still uses `require-client-classes` with
        "The parameter 'require-client-classes' is deprecated. Use
        'evaluate-additional-classes' instead", and refuses a configuration that sets
        both, so `config-get` returns exactly one of the two names.
        """
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(
            4,
            [
                {
                    "id": 1,
                    "subnet": "198.18.1.0/24",
                    "pools": [],
                    "evaluate-additional-classes": ["additional-client"],
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertEqual(
            snapshot.find_by_id(1).configuration.settings.require_client_classes,
            ("additional-client",),
        )

    def test_legacy_client_class_is_read(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(
            4,
            [
                {
                    "id": 1,
                    "subnet": "198.18.1.0/24",
                    "pools": [],
                    "client-class": "legacy-client",
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, CompleteCatalogueSnapshot)
        self.assertEqual(snapshot.find_by_id(1).configuration.settings.client_classes, ("legacy-client",))

    def test_current_client_classes_take_precedence_over_legacy_client_class(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(
            4,
            [
                {
                    "id": 1,
                    "subnet": "198.18.1.0/24",
                    "pools": [],
                    "client-classes": ["current-client"],
                    "client-class": "legacy-client",
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertEqual(snapshot.find_by_id(1).configuration.settings.client_classes, ("current-client",))

    def test_invalid_legacy_client_class_makes_snapshot_incomplete(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(
            4,
            [
                {
                    "id": 1,
                    "subnet": "198.18.1.0/24",
                    "pools": [],
                    "client-class": ["not-a-string"],
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertEqual(snapshot.find_by_id(1).configuration.settings.client_classes, ())
        self.assertEqual(
            [(diagnostic.code, diagnostic.path) for diagnostic in snapshot.diagnostics],
            [("invalid-setting", "subnet4[0].client-class")],
        )

    def test_failed_configuration_read_keeps_the_kea_hint(self):
        """Name the Kea result in the diagnostic, so the operator can act on it."""
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])

        with stub_kea({"subnet4-list": identities, "config-get": {"result": 2, "text": "unknown command"}}):
            snapshot = display(self.server, 4)

        unavailable = [
            diagnostic for diagnostic in snapshot.diagnostics if diagnostic.code == "configuration-unavailable"
        ]
        self.assertEqual(len(unavailable), 1, snapshot.diagnostics)
        self.assertIn("hook library", unavailable[0].message)

    def test_complete_empty_identity_observation_confirms_safe_absence(self):
        with stub_kea(
            {
                "subnet4-list": {"result": 3, "text": "no subnets"},
                "config-get": requests.ConnectionError("down"),
            }
        ):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IdentityOnlyCatalogueSnapshot)
        self.assertFalse(snapshot.unavailable)
        self.assertFalse(snapshot.subnets)

    def test_display_returns_configuration_only_snapshot_without_subnet_commands(self):
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": _identity(4, [], result=2), "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, ConfigurationOnlyCatalogueSnapshot)
        self.assertFalse(snapshot.subnets)
        self.assertEqual(snapshot.configured_subnets[0].candidate_identity.subnet_id, 1)
        self.assertIn("identity-command-unavailable", {diagnostic.code for diagnostic in snapshot.diagnostics})

    def test_display_retries_both_sources_after_a_disagreement(self):
        first_identity = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        first_config = _config(4, [{"id": 1, "subnet": "198.18.2.0/24"}], config_hash="hash-a")
        second_identity = _identity(4, [{"id": 1, "subnet": "198.18.2.0/24"}])
        second_config = _config(4, [{"id": 1, "subnet": "198.18.2.0/24"}], config_hash="hash-b")

        with stub_kea(
            {
                "subnet4-list": queued(first_identity, second_identity),
                "config-get": queued(first_config, second_config),
            }
        ) as kea:
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, CompleteCatalogueSnapshot)
        self.assertEqual(snapshot.subnet_choices, (("198.18.2.0/24", 1),))
        self.assertEqual(kea.commands(), ["subnet4-list", "config-get", "subnet4-list", "config-get"])

    def test_display_reports_configuration_change_during_retry(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        first_config = _config(4, [{"id": 1, "subnet": "198.18.2.0/24"}], config_hash="hash-a")
        second_config = _config(4, [{"id": 1, "subnet": "198.18.2.0/24"}], config_hash="hash-b")

        with stub_kea(
            {
                "subnet4-list": identities,
                "config-get": queued(first_config, second_config),
            }
        ):
            snapshot = display(self.server, 4)

        self.assertFalse(snapshot.consistent)
        self.assertIn("configuration-changed-during-retry", {item.code for item in snapshot.diagnostics})

    def test_shared_network_membership_disagreement_quarantines_subnet(self):
        identities = _identity(
            4,
            [{"id": 1, "subnet": "198.18.1.0/24", "shared-network-name": "access-a"}],
        )
        configuration = _config(
            4,
            [],
            shared_networks=[
                {
                    "name": "access-b",
                    "subnet4": [{"id": 1, "subnet": "198.18.1.0/24"}],
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertFalse(snapshot.subnets)
        self.assertFalse(snapshot.consistent)
        self.assertIn("identity-configuration-disagreement", {item.code for item in snapshot.diagnostics})

    def test_persistent_disagreement_is_incomplete_and_not_authoritative(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.2.0/24"}], config_hash="stable")

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertFalse(snapshot.subnets)
        self.assertIn("identity-configuration-disagreement", {diagnostic.code for diagnostic in snapshot.diagnostics})

    def test_identity_missing_from_complete_configuration_remains_visible(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [], config_hash="stable")

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertEqual(snapshot.subnet_choices, (("198.18.1.0/24", 1),))
        self.assertIsNone(snapshot.subnets[0].configuration)
        self.assertIn("identity-configuration-disagreement", {diagnostic.code for diagnostic in snapshot.diagnostics})

    def test_identity_collision_quarantines_participants_but_keeps_unrelated_subnet(self):
        identities = _identity(
            4,
            [
                {"id": 1, "subnet": "198.18.1.0/24"},
                {"id": 1, "subnet": "198.18.2.0/24"},
                {"id": 2, "subnet": "198.18.3.0/24"},
            ],
        )
        configuration = _config(
            4,
            [
                {"id": 1, "subnet": "198.18.1.0/24", "pools": []},
                {"id": 2, "subnet": "198.18.3.0/24", "pools": []},
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertEqual(snapshot.subnet_choices, (("198.18.3.0/24", 2),))
        diagnostic_codes = {diagnostic.code for diagnostic in snapshot.diagnostics}
        self.assertIn("identity-collision", diagnostic_codes)
        self.assertIn("catalogue-identity-collision", diagnostic_codes)
        self.assertNotIn("identity-configuration-disagreement", diagnostic_codes)

    def test_invalid_pool_is_omitted_without_discarding_subnet(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(
            4,
            [
                {
                    "id": 1,
                    "subnet": "198.18.1.0/24",
                    "pools": [
                        {"pool": "198.18.1.10-198.18.1.20"},
                        {"pool": "not-a-pool"},
                    ],
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertEqual(len(snapshot.subnets), 1)
        self.assertEqual(len(snapshot.subnets[0].configuration.pools), 1)
        self.assertIn("invalid-pool", {diagnostic.code for diagnostic in snapshot.diagnostics})

    def test_invalid_shared_network_fact_does_not_discard_member_subnet(self):
        identities = _identity(
            4,
            [{"id": 1, "subnet": "198.18.1.0/24", "shared-network-name": "access"}],
        )
        configuration = _config(
            4,
            [],
            shared_networks=[
                {
                    "name": "",
                    "subnet4": [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}],
                }
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertEqual(snapshot.subnet_choices, (("198.18.1.0/24", 1),))
        self.assertEqual(snapshot.subnets[0].shared_network.name, "access")
        self.assertIn("invalid-shared-network", {diagnostic.code for diagnostic in snapshot.diagnostics})

    def test_no_false_empty_snapshot_when_both_sources_fail(self):
        with stub_kea(
            {
                "subnet4-list": requests.ConnectionError("identity down"),
                "config-get": requests.ConnectionError("config down"),
            }
        ):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertTrue(snapshot.unavailable)
        self.assertFalse(snapshot.subnets)
        self.assertGreaterEqual(len(snapshot.diagnostics), 2)

    def test_for_synchronization_requires_a_complete_live_snapshot(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])

        with stub_kea({"subnet4-list": identities, "config-get": requests.ConnectionError("down")}):
            with self.assertRaises(CatalogueUnavailable):
                for_synchronization(self.server, 4)

    def test_for_synchronization_returns_complete_live_snapshot(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = for_synchronization(self.server, 4)

        self.assertIsInstance(snapshot, CompleteCatalogueSnapshot)
        self.assertEqual(snapshot.subnet_choices, (("198.18.1.0/24", 1),))

    def test_display_reports_client_construction_failure(self):
        self.server.client_cert_path = "/tmp/netbox-kea-client.crt"
        self.server.client_key_path = ""

        with stub_kea({}) as kea:
            snapshot = display(self.server, 4)

        self.assertEqual(kea.commands(), [])
        self.assertTrue(snapshot.unavailable)
        self.assertEqual(
            {item.code for item in snapshot.diagnostics},
            {"identity-unavailable", "configuration-unavailable"},
        )

    def test_mutation_scope_resolves_exact_identity_and_prepares_next_id(self):
        identities = _identity(
            4,
            [
                {"id": 1, "subnet": "198.18.1.0/24"},
                {"id": 9, "subnet": "198.18.9.0/24"},
            ],
        )
        configuration = _config(
            4,
            [
                {"id": 1, "subnet": "198.18.1.0/24", "pools": []},
                {"id": 9, "subnet": "198.18.9.0/24", "pools": []},
            ],
        )

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            with mutation(self.server, 4) as scope:
                self.assertEqual(scope.find_by_id(1).cidr, "198.18.1.0/24")
                self.assertEqual(scope.find_by_cidr("198.18.9.0/24").subnet_id, 9)
                self.assertIsNone(scope.find_by_id(3))
                prepared = scope.prepare_creation("198.18.10.0/24")

        self.assertEqual(prepared.subnet_id, 10)
        self.assertEqual(prepared.cidr, "198.18.10.0/24")

    def test_mutation_scope_validates_lookup_and_explicit_creation_identity(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])
        unopened_scope = mutation(self.server, 4)
        with self.assertRaisesMessage(RuntimeError, "must be entered"):
            unopened_scope.find_by_id(1)

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            with mutation(self.server, 4) as scope:
                self.assertIsNone(scope.find_by_cidr("198.18.2.0/24"))
                with self.assertRaisesMessage(ValueError, "non-empty string"):
                    scope.prepare_creation("")
                with self.assertRaisesMessage(ValueError, "must be an integer"):
                    scope.prepare_creation("198.18.2.0/24", True)
                with self.assertRaisesMessage(ValueError, "must be between"):
                    scope.prepare_creation("198.18.2.0/24", 0)
                with self.assertRaisesMessage(SubnetIdentityConflict, "Subnet ID 1"):
                    scope.prepare_creation("198.18.2.0/24", 1)

    def test_incomplete_mutation_scope_cannot_confirm_absence_or_prepare_creation(self):
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea(
            {
                "subnet4-list": {"result": 1, "text": "failed"},
                "config-get": configuration,
            }
        ):
            with mutation(self.server, 4) as scope:
                with self.assertRaises(CatalogueUnavailable):
                    scope.find_by_id(99)
                with self.assertRaises(CatalogueUnavailable):
                    scope.prepare_creation("198.18.2.0/24")

    def test_mutation_creation_rejects_existing_identity(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            with mutation(self.server, 4) as scope:
                with self.assertRaises(SubnetIdentityConflict):
                    scope.prepare_creation("198.18.1.0/24")

    def test_mutation_creation_reuses_a_free_id_when_the_range_above_is_full(self):
        subnets = [{"id": 1, "subnet": "198.18.1.0/24"}, {"id": MAX_SUBNET_ID, "subnet": "198.18.9.0/24"}]
        identities = _identity(4, subnets)
        configuration = _config(4, [{**subnet, "pools": []} for subnet in subnets])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            with mutation(self.server, 4) as scope:
                prepared = scope.prepare_creation("198.18.2.0/24")

        self.assertEqual(prepared.subnet_id, 2)

    def test_mutation_creation_fails_when_subnet_id_range_is_exhausted(self):
        subnets = [{"id": 1, "subnet": "198.18.1.0/24"}, {"id": 2, "subnet": "198.18.2.0/24"}]
        identities = _identity(4, subnets)
        configuration = _config(4, [{**subnet, "pools": []} for subnet in subnets])

        # mock-ok: the real range is 4 billion IDs wide, so narrowing it is the only way
        # to hold every ID and reach the guard.
        with (
            patch("netbox_kea.subnet_catalogue.MAX_SUBNET_ID", 2),
            stub_kea({"subnet4-list": identities, "config-get": configuration}),
        ):
            with mutation(self.server, 4) as scope:
                with self.assertRaises(SubnetIdExhausted):
                    scope.prepare_creation("198.18.3.0/24")

    def test_mutation_scope_rejects_use_after_it_exits(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            with mutation(self.server, 4) as scope:
                self.assertEqual(scope.find_by_id(1).cidr, "198.18.1.0/24")

            for use in (
                lambda: scope.find_by_id(1),
                lambda: scope.find_by_cidr("198.18.1.0/24"),
                lambda: scope.prepare_creation("198.18.2.0/24"),
            ):
                with self.assertRaisesMessage(RuntimeError, "must be entered"):
                    use()

    def test_mutation_scope_invalidates_display_cache_on_entry_and_exit(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}) as kea:
            display(self.server, 4)
            display(self.server, 4)
            with mutation(self.server, 4):
                # Entry invalidation only shows here: the scope reads live without caching,
                # so without it this call would serve the pre-mutation snapshot.
                display(self.server, 4)
            display(self.server, 4)

        self.assertEqual(kea.commands().count("subnet4-list"), 4)
        self.assertEqual(kea.commands().count("config-get"), 4)
