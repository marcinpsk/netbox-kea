import json
from ipaddress import ip_address, ip_network

from django.test import SimpleTestCase

from netbox_kea.dhcp_options import DHCPOption
from netbox_kea.reservation_transfer import (
    ReservationTransferError,
    export_reservation_document,
    parse_reservation_document,
    resolve_import_proposal,
)
from netbox_kea.reservations import (
    GlobalReservationScope,
    InSubnetReservationScope,
    IPv4Reservation,
    IPv6Reservation,
    ReservationIdentity,
)
from netbox_kea.subnet_catalogue import SubnetIdentity


class TestReservationTransfer(SimpleTestCase):
    def setUp(self):
        self.records = (
            IPv4Reservation(
                scope=InSubnetReservationScope(SubnetIdentity(20, ip_network("198.18.0.0/24"))),
                identity=ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:ff"),
                addresses=(ip_address("198.18.0.20"),),
                hostname="host.example.invalid",
                options=(
                    DHCPOption(
                        code=6,
                        name="domain-name-servers",
                        space="dhcp4",
                        data="198.18.0.53",
                        csv_format=True,
                        always_send=False,
                        never_send=False,
                    ),
                ),
            ),
            IPv6Reservation(
                scope=GlobalReservationScope(),
                identity=ReservationIdentity("duid", "00:01:02:03"),
                addresses=(ip_address("2001:db8::20"), ip_address("2001:db8::21")),
                delegated_prefixes=(ip_network("2001:db8:100::/56"),),
            ),
        )

    def test_yaml_and_json_export_round_trip_supported_in_subnet_records(self):
        for format_name in ("yaml", "json"):
            with self.subTest(format=format_name):
                document = export_reservation_document(self.records[:1], format_name)
                parsed = parse_reservation_document(document, format_name)

                self.assertEqual(len(parsed.proposals), 1)
                self.assertEqual(parsed.proposals[0].family, 4)
                self.assertEqual(parsed.proposals[0].subnet_cidr, "198.18.0.0/24")
                self.assertEqual(parsed.proposals[0].identity, ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:ff"))
                self.assertEqual(parsed.proposals[0].addresses, (ip_address("198.18.0.20"),))
                self.assertEqual(parsed.diagnostics, ())

    def test_global_schema_is_recognized_but_creation_is_rejected(self):
        document = export_reservation_document(self.records[1:], "yaml")

        parsed = parse_reservation_document(document, "yaml")

        self.assertEqual(parsed.proposals, ())
        self.assertEqual(parsed.diagnostics[0].code, "unsupported-scope")
        self.assertEqual(parsed.diagnostics[0].source_position, "reservations[0].scope.type")

    def test_reports_all_field_errors_and_duplicates_before_execution(self):
        document = """version: 1
reservations:
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: hw-address, value: not-a-mac}
    addresses: [2001:db8::1]
    delegated_prefixes: []
    hostname: 42
    options: []
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: flex-id, value: duplicate-key}
    addresses: []
    delegated_prefixes: []
    hostname: ""
    options: []
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: flex-id, value: duplicate-key}
    addresses: []
    delegated_prefixes: []
    hostname: ""
    options: []
"""

        parsed = parse_reservation_document(document, "yaml")

        positions = {diagnostic.source_position for diagnostic in parsed.diagnostics}
        self.assertIn("reservations[0].identity.value", positions)
        self.assertIn("reservations[0].addresses[0]", positions)
        self.assertIn("reservations[0].hostname", positions)
        self.assertIn("reservations[2].identity", positions)
        self.assertEqual(parsed.proposals, ())

    def test_reports_an_out_of_subnet_address_before_returning_any_proposal(self):
        document = """version: 1
reservations:
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: hw-address, value: "aa:bb:cc:dd:ee:01"}
    addresses: [198.18.0.20]
    delegated_prefixes: []
    hostname: first.example.invalid
    options: []
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: hw-address, value: "aa:bb:cc:dd:ee:02"}
    addresses: [198.18.1.20]
    delegated_prefixes: []
    hostname: second.example.invalid
    options: []
"""

        parsed = parse_reservation_document(document, "yaml")

        self.assertEqual(parsed.proposals, ())
        self.assertEqual(len(parsed.diagnostics), 1)
        self.assertEqual(parsed.diagnostics[0].code, "out-of-subnet-address")
        self.assertEqual(parsed.diagnostics[0].source_position, "reservations[1].addresses[0]")

    def test_rejects_an_unknown_or_ambiguous_format(self):
        with self.assertRaises(ReservationTransferError):
            parse_reservation_document("{}", "auto")

    def test_rejects_invalid_export_format_and_document_syntax(self):
        with self.assertRaisesRegex(ReservationTransferError, "YAML or JSON"):
            export_reservation_document(self.records, "csv")
        for document, format_name in (("{", "json"), ("[unterminated", "yaml")):
            with self.subTest(format_name=format_name):
                with self.assertRaisesRegex(ReservationTransferError, "not valid syntax"):
                    parse_reservation_document(document, format_name)

    def test_rejects_invalid_document_envelopes(self):
        root = parse_reservation_document("[]", "json")
        self.assertEqual(root.diagnostics[0].code, "invalid-document")

        envelope = parse_reservation_document('{"version": 2, "reservations": {}}', "json")
        self.assertEqual(
            {diagnostic.code for diagnostic in envelope.diagnostics},
            {"invalid-version", "invalid-reservations"},
        )

    def test_reports_invalid_record_field_shapes_together(self):
        document = {
            "version": 1,
            "reservations": [
                "not-an-object",
                {
                    "family": True,
                    "scope": [],
                    "identity": [],
                    "addresses": "198.18.0.20",
                    "delegated_prefixes": "2001:db8:100::/56",
                    "hostname": "host.example.invalid",
                    "options": {},
                },
            ],
        }

        result = parse_reservation_document(json.dumps(document), "json")

        self.assertEqual(
            {diagnostic.code for diagnostic in result.diagnostics},
            {
                "invalid-record",
                "invalid-family",
                "invalid-scope",
                "invalid-identity",
                "invalid-addresses",
                "invalid-prefixes",
                "invalid-options",
            },
        )

    def test_reports_scope_and_identity_semantic_errors(self):
        base = {
            "family": 4,
            "identity": {"type": "hw-address", "value": "aa:bb:cc:dd:ee:ff"},
            "addresses": [],
            "delegated_prefixes": [],
            "hostname": "",
            "options": [],
        }
        records = [
            {**base, "scope": {"type": "other"}},
            {**base, "scope": {"type": "in-subnet", "subnet": {"cidr": "invalid"}}},
            {**base, "scope": {"type": "in-subnet", "subnet": {"cidr": "2001:db8::/64"}}},
            {
                **base,
                "family": 6,
                "scope": {"type": "in-subnet", "subnet": {"cidr": "2001:db8::/64"}},
                "identity": {"type": "client-id", "value": "01:02"},
            },
        ]

        result = parse_reservation_document(json.dumps({"version": 1, "reservations": records}), "json")

        positions = {diagnostic.source_position for diagnostic in result.diagnostics}
        self.assertIn("reservations[0].scope.type", positions)
        self.assertIn("reservations[1].scope.subnet.cidr", positions)
        self.assertIn("reservations[2].scope.subnet.cidr", positions)
        self.assertIn("reservations[3].identity.type", positions)

    def test_reports_address_prefix_and_option_semantic_errors(self):
        scope4 = {"type": "in-subnet", "subnet": {"cidr": "198.18.0.0/24"}}
        scope6 = {"type": "in-subnet", "subnet": {"cidr": "2001:db8::/64"}}
        records = [
            {
                "family": 4,
                "scope": scope4,
                "identity": {"type": "hw-address", "value": "aa:bb:cc:dd:ee:01"},
                "addresses": ["invalid", "2001:db8::1", "198.18.0.20", "198.18.0.20", "198.18.0.21"],
                "delegated_prefixes": ["2001:db8:100::/56"],
                "hostname": "",
                "options": [42],
            },
            {
                "family": 6,
                "scope": scope6,
                "identity": {"type": "duid", "value": "00:01:02:03"},
                "addresses": [],
                "delegated_prefixes": ["invalid", "2001:db8:100::/56", "2001:db8:100::/56"],
                "hostname": "",
                "options": [],
            },
        ]

        result = parse_reservation_document(json.dumps({"version": 1, "reservations": records}), "json")

        codes = {diagnostic.code for diagnostic in result.diagnostics}
        self.assertTrue(
            {
                "invalid-address",
                "duplicate-address",
                "invalid-addresses",
                "invalid-prefixes",
                "invalid-option",
                "invalid-prefix",
                "duplicate-prefix",
            }.issubset(codes)
        )

    def test_resolves_a_valid_ipv6_proposal_and_rejects_a_different_subnet(self):
        document = export_reservation_document(
            (
                IPv6Reservation(
                    scope=InSubnetReservationScope(SubnetIdentity(10, ip_network("2001:db8::/64"))),
                    identity=ReservationIdentity("duid", "00:01:02:03"),
                    addresses=(ip_address("2001:db8::20"),),
                    delegated_prefixes=(ip_network("2001:db8:100::/56"),),
                ),
            ),
            "json",
        )
        proposal = parse_reservation_document(document, "json").proposals[0]

        reservation = resolve_import_proposal(proposal, SubnetIdentity(10, ip_network("2001:db8::/64")))

        self.assertIsInstance(reservation, IPv6Reservation)
        self.assertEqual(reservation.delegated_prefixes, (ip_network("2001:db8:100::/56"),))
        with self.assertRaisesRegex(ValueError, "does not match"):
            resolve_import_proposal(proposal, SubnetIdentity(11, ip_network("2001:db8:1::/64")))
