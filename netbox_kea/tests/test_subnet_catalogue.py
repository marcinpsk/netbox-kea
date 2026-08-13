import ipaddress

import requests
from django.test import TestCase, override_settings

from netbox_kea.subnet_catalogue import (
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
from netbox_kea.tests.kea_stub import queued, stub_kea
from netbox_kea.tests.utils import _PLUGINS_CONFIG, _drop_subnet_choices_cache, _make_db_server


def _identity(version, subnets, *, result=0):
    if result != 0:
        return {"result": result, "text": "subnet command unavailable"}
    return {"result": 0, "arguments": {"subnets": list(subnets)}}


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

    def test_display_returns_identity_only_snapshot_when_config_fails(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])

        with stub_kea({"subnet4-list": identities, "config-get": requests.ConnectionError("down")}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IdentityOnlyCatalogueSnapshot)
        self.assertEqual(snapshot.subnet_choices, (("198.18.1.0/24", 1),))
        self.assertIsNone(snapshot.subnets[0].configuration)
        self.assertIn("configuration-unavailable", {diagnostic.code for diagnostic in snapshot.diagnostics})

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

    def test_persistent_disagreement_is_incomplete_and_not_authoritative(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.2.0/24"}], config_hash="stable")

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            snapshot = display(self.server, 4)

        self.assertIsInstance(snapshot, IncompleteCatalogueSnapshot)
        self.assertFalse(snapshot.subnets)
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
        self.assertIn("identity-collision", {diagnostic.code for diagnostic in snapshot.diagnostics})

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

    def test_mutation_creation_rejects_existing_identity(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            with mutation(self.server, 4) as scope:
                with self.assertRaises(SubnetIdentityConflict):
                    scope.prepare_creation("198.18.1.0/24")

    def test_mutation_creation_fails_when_subnet_id_range_is_exhausted(self):
        identities = _identity(4, [{"id": 4_294_967_294, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 4_294_967_294, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}):
            with mutation(self.server, 4) as scope:
                with self.assertRaises(SubnetIdExhausted):
                    scope.prepare_creation("198.18.2.0/24")

    def test_mutation_scope_invalidates_display_cache_on_entry_and_exit(self):
        identities = _identity(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        configuration = _config(4, [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}])

        with stub_kea({"subnet4-list": identities, "config-get": configuration}) as kea:
            display(self.server, 4)
            display(self.server, 4)
            with mutation(self.server, 4):
                pass
            display(self.server, 4)

        self.assertEqual(kea.commands().count("subnet4-list"), 3)
        self.assertEqual(kea.commands().count("config-get"), 3)
