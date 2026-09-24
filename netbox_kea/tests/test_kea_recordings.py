"""Parse configuration replies recorded from a real Kea, not hand-written stubs.

The recordings come from scripts/record_kea_config_get.py, which runs the coverage
configurations in kea_recordings/ on the Compose harness Kea version.
"""

import ipaddress
import json
import re
from pathlib import Path

from django.test import TestCase, override_settings

from netbox_kea import server_configuration
from netbox_kea.subnet_catalogue import CompleteCatalogueSnapshot, for_synchronization
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.utils import _PLUGINS_CONFIG, _make_db_server

_RECORDINGS = Path(__file__).with_name("kea_recordings")
_COMPOSE_OVERRIDE = Path(__file__).resolve().parents[2] / "tests" / "docker" / "docker-compose.override.yml"


def _recording(family: int) -> dict:
    return json.loads((_RECORDINGS / f"dhcp{family}.json").read_text())


def test_recordings_come_from_the_harness_kea_version():
    harness = re.findall(r"kea-dhcp[46]:\$\{KEA_VERSION:-([^}]+)\}", _COMPOSE_OVERRIDE.read_text())
    assert len(harness) == 2, harness
    assert {_recording(4)["kea-version"], _recording(6)["kea-version"]} == set(harness)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestRecordedKeaConfiguration(TestCase):
    def setUp(self):
        self.server = _make_db_server()

    def _snapshot(self, family: int) -> server_configuration.ServerConfigurationSnapshot:
        with stub_kea({"config-get": _recording(family)["config-get"]}):
            return server_configuration.for_verification(self.server, family)

    def test_every_recorded_fact_parses_without_a_diagnostic(self):
        for family in (4, 6):
            with self.subTest(family=family):
                snapshot = self._snapshot(family)
                self.assertEqual(snapshot.diagnostics, ())
                self.assertTrue(snapshot.available)
                self.assertTrue(snapshot.complete)
                self.assertTrue(snapshot.global_options_complete)
                self.assertTrue(snapshot.shared_networks_complete)
                self.assertTrue(all(subnet.complete for subnet in snapshot.subnets))
                self.assertTrue(all(network.complete for network in snapshot.shared_networks))

    def test_recorded_facts_keep_their_values(self):
        cases = {
            4: ("192.0.2.0/24", "198.51.100.1", 9, ("uint8", "string")),
            6: ("2001:db8:1::/64", "2001:db8:ffff::1", 9, ("uint16", "string")),
        }
        for family, (cidr, relay, global_options, record_types) in cases.items():
            with self.subTest(family=family):
                snapshot = self._snapshot(family)
                self.assertEqual(
                    [(subnet.declared_subnet_id, subnet.shared_network_name) for subnet in snapshot.subnets],
                    [(10, None), (20, "office"), (21, "office")],
                )
                standalone = snapshot.subnets[0]
                self.assertEqual(standalone.declared_cidr, cidr)
                self.assertEqual(len(standalone.configuration.pools), 2)
                settings = standalone.configuration.settings
                self.assertEqual(settings.relay_addresses, (ipaddress.ip_address(relay),))
                self.assertEqual(settings.client_classes, ("voip",))
                self.assertEqual(settings.require_client_classes, ("printers",))
                self.assertEqual(settings.allocator, "random")
                self.assertEqual(settings.ddns_qualifying_suffix, "")
                self.assertEqual(
                    [(network.name, len(network.member_cidrs)) for network in snapshot.shared_networks],
                    [("empty-network", 0), ("office", 2)],
                )
                self.assertEqual(len(snapshot.global_options), global_options)
                self.assertIn(record_types, [definition.record_types for definition in snapshot.option_definitions])

    def test_the_recorded_catalogue_is_complete_for_synchronization(self):
        for family in (4, 6):
            with self.subTest(family=family):
                recording = _recording(family)
                with stub_kea(
                    {"config-get": recording["config-get"], f"subnet{family}-list": recording[f"subnet{family}-list"]}
                ):
                    snapshot = for_synchronization(self.server, family)
                self.assertIsInstance(snapshot, CompleteCatalogueSnapshot)
                self.assertEqual([subnet.identity.subnet_id for subnet in snapshot.subnets], [10, 20, 21])
