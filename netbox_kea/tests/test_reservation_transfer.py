import json
from ipaddress import IPv6Address, ip_address, ip_network
from unittest.mock import patch

from django.test import SimpleTestCase

from netbox_kea.dhcp_options import DHCPOption
from netbox_kea.reservation_transfer import (
    MAX_DOCUMENT_BYTES,
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

    def test_reports_a_duplicate_when_the_same_record_has_another_field_error(self):
        document = """version: 1
reservations:
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
    hostname: 42
    options: []
"""

        parsed = parse_reservation_document(document, "yaml")

        self.assertEqual(
            {(diagnostic.code, diagnostic.source_position) for diagnostic in parsed.diagnostics},
            {
                ("invalid-hostname", "reservations[1].hostname"),
                ("duplicate-reservation", "reservations[1].identity"),
            },
        )
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

    def test_rejects_a_document_larger_than_the_transfer_limit(self):
        """The parser bounds every caller, not only the upload the import form accepts."""
        oversized = '{"version": 1, "reservations": []}' + " " * MAX_DOCUMENT_BYTES

        with self.assertRaisesRegex(ReservationTransferError, "must not exceed"):
            parse_reservation_document(oversized, "json")

    def test_rejects_invalid_export_format_and_document_syntax(self):
        with self.assertRaisesRegex(ReservationTransferError, "YAML or JSON"):
            export_reservation_document(self.records, "csv")
        for document, format_name in (("{", "json"), ("[unterminated", "yaml")):
            with self.subTest(format_name=format_name):
                with self.assertRaisesRegex(ReservationTransferError, "not valid syntax"):
                    parse_reservation_document(document, format_name)

    def test_rejects_a_yaml_document_that_exceeds_the_parser_recursion_limit(self):
        document = "[" * 10_000 + "]" * 10_000

        with self.assertRaisesRegex(ReservationTransferError, "not valid syntax"):
            parse_reservation_document(document, "yaml")

    def test_reports_a_json_recursion_error_as_a_parse_error(self):
        """The JSON scanner probes the real C stack, so no document depth is portable."""

        def raise_recursion_error(*_args, **_kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        with (
            patch("netbox_kea.reservation_transfer.json.loads", raise_recursion_error),
            self.assertRaisesRegex(ReservationTransferError, "not valid syntax"),
        ):
            parse_reservation_document("[]", "json")

    def test_reports_an_oversized_json_integer_as_a_transfer_error(self):
        """`json.loads` raises a plain ValueError here, not JSONDecodeError.

        The import view handles only ReservationTransferError, so the bare ValueError
        left the view as an unhandled 500 instead of a form error.
        """
        document = '{"version": ' + "9" * 5000 + "}"

        with self.assertRaises(ReservationTransferError):
            parse_reservation_document(document, "json")

    def test_rejects_invalid_document_envelopes(self):
        root = parse_reservation_document("[]", "json")
        self.assertEqual(root.diagnostics[0].code, "invalid-document")

        envelope = parse_reservation_document('{"version": 2, "reservations": {}}', "json")
        self.assertEqual(
            {diagnostic.code for diagnostic in envelope.diagnostics},
            {"invalid-version", "invalid-reservations"},
        )

    def test_rejects_a_boolean_document_version(self):
        result = parse_reservation_document('{"version": true, "reservations": []}', "json")

        self.assertEqual(
            [(diagnostic.code, diagnostic.source_position) for diagnostic in result.diagnostics],
            [("invalid-version", "version")],
        )

    def _valid_record(self, **overrides):
        """One complete in-subnet IPv4 record, so a probe changes exactly one field."""
        record = {
            "family": 4,
            "scope": {"type": "in-subnet", "subnet": {"cidr": "198.18.0.0/24"}},
            "identity": {"type": "hw-address", "value": "aa:bb:cc:dd:ee:ff"},
            "addresses": ["198.18.0.20"],
            "delegated_prefixes": [],
            "hostname": "host.example.invalid",
            "options": [],
        }
        record.update(overrides)
        return record

    def test_rejects_a_float_family(self):
        """``4.0 == 4``, so an equality test alone lets a float into a ``Family`` field."""
        document = {"version": 1, "reservations": [self._valid_record(family=4.0)]}

        result = parse_reservation_document(json.dumps(document), "json")

        self.assertEqual(
            [(diagnostic.code, diagnostic.source_position) for diagnostic in result.diagnostics],
            [("invalid-family", "reservations[0].family")],
        )

    def test_rejects_a_float_document_version(self):
        """``1.0 != 1`` is False, so the version gate needs a type check of its own."""
        document = {"version": 1.0, "reservations": [self._valid_record()]}

        result = parse_reservation_document(json.dumps(document), "json")

        self.assertEqual(
            [(diagnostic.code, diagnostic.source_position) for diagnostic in result.diagnostics],
            [("invalid-version", "version")],
        )

    def test_normalizes_a_non_canonical_subnet_cidr(self):
        """The catalogue match compares canonical strings, so the parser normalizes here.

        ``2001:0db8::/64`` names the same network as ``2001:db8::/64``. Rejecting the
        spelling would fail a valid document; ``strict=True`` still rejects host bits.
        """
        record = self._valid_record(
            family=6,
            scope={"type": "in-subnet", "subnet": {"cidr": "2001:0db8::/64"}},
            identity={"type": "duid", "value": "00:01:02:03"},
            addresses=["2001:db8::5"],
        )
        document = {"version": 1, "reservations": [record]}

        result = parse_reservation_document(json.dumps(document), "json", expected_family=6)

        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.proposals[0].subnet_cidr, "2001:db8::/64")

    def test_rejects_a_subnet_cidr_with_host_bits_set(self):
        """The diagnostic exists for an address that is not a network, and still fires."""
        document = {
            "version": 1,
            "reservations": [self._valid_record(scope={"type": "in-subnet", "subnet": {"cidr": "198.18.0.1/24"}})],
        }

        result = parse_reservation_document(json.dumps(document), "json")

        self.assertEqual(
            [(diagnostic.code, diagnostic.source_position) for diagnostic in result.diagnostics],
            [("invalid-subnet", "reservations[0].scope.subnet.cidr")],
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

    def test_reports_unknown_fields_at_each_document_boundary(self):
        document = {
            "version": 1,
            "metadata": {},
            "reservations": [
                {
                    "family": 4,
                    "scope": {
                        "type": "in-subnet",
                        "subnet": {"cidr": "198.18.0.0/24", "id": 20},
                        "subnet_id": 20,
                    },
                    "identity": {
                        "type": "hw-address",
                        "value": "aa:bb:cc:dd:ee:01",
                        "label": "primary",
                    },
                    "addresses": [],
                    "delegated_prefixes": [],
                    "hostname": "",
                    "options": [{"name": "domain-name", "data": "example.invalid", "always_sned": True}],
                    "comment": "ignored",
                }
            ],
        }

        result = parse_reservation_document(json.dumps(document), "json")

        self.assertEqual(
            {(diagnostic.code, diagnostic.source_position) for diagnostic in result.diagnostics},
            {
                ("unknown-field", "metadata"),
                ("unknown-field", "reservations[0].comment"),
                ("unknown-field", "reservations[0].scope.subnet_id"),
                ("unknown-field", "reservations[0].scope.subnet.id"),
                ("unknown-field", "reservations[0].identity.label"),
                ("unknown-field", "reservations[0].options[0].always_sned"),
            },
        )
        self.assertEqual(result.proposals, ())

    def test_reports_duplicate_dhcp_options(self):
        document = {
            "version": 1,
            "reservations": [
                {
                    "family": 4,
                    "scope": {"type": "in-subnet", "subnet": {"cidr": "198.18.0.0/24"}},
                    "identity": {"type": "hw-address", "value": "aa:bb:cc:dd:ee:01"},
                    "addresses": [],
                    "delegated_prefixes": [],
                    "hostname": "",
                    "options": [
                        {"name": "domain-name", "data": "first.example.invalid"},
                        {"name": "domain-name", "data": "second.example.invalid"},
                    ],
                }
            ],
        }

        result = parse_reservation_document(json.dumps(document), "json")

        self.assertEqual(
            [(diagnostic.code, diagnostic.source_position) for diagnostic in result.diagnostics],
            [("duplicate-option", "reservations[0].options[1]")],
        )
        self.assertEqual(result.proposals, ())

    def test_rejects_non_string_network_values_from_json(self):
        records = [
            {
                "family": 4,
                "scope": {"type": "in-subnet", "subnet": {"cidr": 42}},
                "identity": {"type": "hw-address", "value": "aa:bb:cc:dd:ee:01"},
                "addresses": [],
                "delegated_prefixes": [],
                "hostname": "",
                "options": [],
            },
            {
                "family": 4,
                "scope": {"type": "in-subnet", "subnet": {"cidr": "0.0.0.0/24"}},
                "identity": {"type": "hw-address", "value": "aa:bb:cc:dd:ee:02"},
                "addresses": [42],
                "delegated_prefixes": [],
                "hostname": "",
                "options": [],
            },
            {
                "family": 6,
                "scope": {"type": "in-subnet", "subnet": {"cidr": "2001:db8::/64"}},
                "identity": {"type": "duid", "value": "00:01:02:03"},
                "addresses": [],
                "delegated_prefixes": [42],
                "hostname": "",
                "options": [],
            },
        ]

        result = parse_reservation_document(json.dumps({"version": 1, "reservations": records}), "json")

        self.assertEqual(
            {(diagnostic.code, diagnostic.source_position) for diagnostic in result.diagnostics},
            {
                ("invalid-subnet", "reservations[0].scope.subnet.cidr"),
                ("invalid-address", "reservations[1].addresses[0]"),
                ("invalid-prefix", "reservations[2].delegated_prefixes[0]"),
            },
        )
        self.assertEqual(result.proposals, ())

    def test_export_uses_the_complete_normalized_option_shape(self):
        reservation = IPv4Reservation(
            scope=InSubnetReservationScope(SubnetIdentity(20, ip_network("198.18.0.0/24"))),
            identity=ReservationIdentity("flex-id", "option-shape"),
            addresses=(),
            options=(
                DHCPOption(
                    code=None,
                    name="domain-name",
                    space=None,
                    data="example.invalid",
                    csv_format=None,
                    always_send=None,
                    never_send=None,
                ),
            ),
        )

        exported = json.loads(export_reservation_document((reservation,), "json"))

        self.assertEqual(
            exported["reservations"][0]["options"],
            [
                {
                    "code": None,
                    "name": "domain-name",
                    "space": None,
                    "data": "example.invalid",
                    "csv_format": None,
                    "always_send": None,
                    "never_send": None,
                }
            ],
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


class TestReservationTransferDocumentBounds(SimpleTestCase):
    """MAX_DOCUMENT_BYTES must bound parser work, not only the input length."""

    _SUBNET = "2001:db8::/64"

    @classmethod
    def _one_record(cls, addresses: str) -> str:
        return (
            "version: 1\nreservations: [{family: 6, "
            f"scope: {{type: in-subnet, subnet: {{cidr: '{cls._SUBNET}'}}}}, "
            "identity: {type: duid, value: '00:01:02:03'}, "
            f"addresses: [{addresses}], "
            "delegated_prefixes: [], hostname: 'host.example.invalid', options: []}]\n"
        )

    def test_a_yaml_alias_is_rejected(self):
        document = "addrs: &addrs ['2001:db8::1', '2001:db8::2']\n" + self._one_record("*addrs")

        with self.assertRaisesRegex(ReservationTransferError, "must not use YAML aliases"):
            parse_reservation_document(document, "yaml")

    def test_a_yaml_anchor_without_an_alias_still_loads(self):
        document = self._one_record("&first '2001:db8::1'")

        result = parse_reservation_document(document, "yaml")

        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.proposals), 1)

    def test_many_addresses_parse_without_a_diagnostic(self):
        """One record can carry every address that fits inside the byte cap."""
        addresses = ", ".join(f"'2001:db8::{index:x}'" for index in range(1, 20_001))
        document = self._one_record(addresses)
        self.assertLess(len(document.encode()), MAX_DOCUMENT_BYTES)

        result = parse_reservation_document(document, "yaml")

        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.proposals[0].addresses), 20_000)

    def test_duplicate_detection_never_scans_the_addresses_it_has_seen(self):
        """Duplicate detection scanned a list, so one record could hang a worker.

        Count the comparisons instead of the clock: a set membership hashes and
        compares only on a bucket collision, while a list scan compares against every
        earlier address. A loaded runner changes the wall clock, not the comparisons.
        """
        count = 2_000
        document = self._one_record(", ".join(f"'2001:db8::{index:x}'" for index in range(1, count + 1)))
        comparisons = 0
        equal = IPv6Address.__eq__

        def counting_equal(self, other):
            nonlocal comparisons
            comparisons += 1
            return equal(self, other)

        with patch.object(IPv6Address, "__eq__", counting_equal):
            result = parse_reservation_document(document, "yaml")

        self.assertEqual(len(result.proposals[0].addresses), count)
        # A list scan makes about count * count / 2 comparisons: two million here.
        self.assertLess(comparisons, count, f"{comparisons} comparisons for {count} addresses is not linear.")

    def test_a_duplicate_address_is_still_reported(self):
        document = self._one_record("'2001:db8::1', '2001:db8::2', '2001:db8::1'")

        result = parse_reservation_document(document, "yaml")

        self.assertEqual([d.code for d in result.diagnostics], ["duplicate-address"])
        self.assertEqual(result.diagnostics[0].source_position, "reservations[0].addresses[2]")
