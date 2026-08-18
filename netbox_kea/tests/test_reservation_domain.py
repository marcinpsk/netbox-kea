from datetime import UTC, datetime
from ipaddress import ip_address, ip_network

from django.test import SimpleTestCase

from netbox_kea.dhcp_options import DHCPOption, parse_dhcp_options
from netbox_kea.kea import KeaClient
from netbox_kea.reservations import (
    ClearValue,
    GlobalReservationScope,
    InSubnetReservationScope,
    IPv4Reservation,
    IPv6Reservation,
    MalformedReservation,
    ReservationCapabilities,
    ReservationChange,
    ReservationConflict,
    ReservationIdentity,
    ReservationSnapshot,
    ReservationSynchronizationState,
    SetValue,
    reservation_fingerprint,
)
from netbox_kea.subnet_catalogue import (
    CatalogueSnapshot,
    IdentityOnlyCatalogueSnapshot,
    SubnetIdentity,
    VerifiedSubnet,
)

from .kea_stub import _res_get, _res_page, queued, stub_kea


def _catalogue(family: int, subnet_id: int, cidr: str) -> CatalogueSnapshot:
    subnet = VerifiedSubnet(
        identity=SubnetIdentity(subnet_id=subnet_id, network=ip_network(cidr)),
        configuration=None,
        shared_network=None,
    )
    # Identity-only: the fixture carries no configuration, which is all a Reservation
    # Scope needs verified.
    return IdentityOnlyCatalogueSnapshot(
        server_id=1,
        family=family,
        observed_at=datetime.now(UTC),
        subnets=(subnet,),
        configured_subnets=(),
        diagnostics=(),
        identity_complete=True,
        configuration_complete=False,
        consistent=True,
        configuration_hash=None,
    )


def _persistence_responses(version: int) -> dict:
    return {
        "config-get": {"result": 0, "arguments": {f"Dhcp{version}": {}, "hash": "reservation-test"}},
        "config-test": {"result": 0},
        "config-write": {"result": 0},
    }


class TestReservationIdentity(SimpleTestCase):
    def test_normalizes_hex_identifiers_to_lowercase_colon_notation(self):
        self.assertEqual(ReservationIdentity("hw-address", "AA-BB-CC-DD-EE-FF").value, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(ReservationIdentity("duid", "00010203").value, "00:01:02:03")
        self.assertEqual(ReservationIdentity("client-id", "01.AA.BB").value, "01:aa:bb")

    def test_preserves_opaque_identifiers_exactly(self):
        self.assertEqual(ReservationIdentity("circuit-id", " Circuit/A ").value, " Circuit/A ")
        self.assertEqual(ReservationIdentity("flex-id", "Relay:Class-A").value, "Relay:Class-A")

    def test_rejects_oversized_opaque_identifiers(self):
        for identifier_type in ("circuit-id", "flex-id"):
            with self.subTest(identifier_type=identifier_type):
                with self.assertRaisesRegex(ValueError, "255"):
                    ReservationIdentity(identifier_type, "x" * 256)

    def test_enforces_hex_identifier_octet_bounds(self):
        max_duid = ":".join(["ab"] * 128)
        self.assertEqual(ReservationIdentity("duid", max_duid).value, max_duid)
        with self.assertRaises(ValueError):
            ReservationIdentity("duid", ":".join(["ab"] * 129))
        with self.assertRaises(ValueError):
            ReservationIdentity("client-id", "ab")

    def test_rejects_unsupported_and_empty_identifiers(self):
        for identifier_type, value in (("remote-id", "relay"), ("flex-id", ""), ("duid", "not-hex")):
            with self.subTest(identifier_type=identifier_type, value=value):
                with self.assertRaises(ValueError):
                    ReservationIdentity(identifier_type, value)


class TestReservationValues(SimpleTestCase):
    def setUp(self):
        self.scope4 = InSubnetReservationScope(SubnetIdentity(20, ip_network("198.18.0.0/24")))
        self.scope6 = InSubnetReservationScope(SubnetIdentity(10, ip_network("2001:db8::/64")))
        self.identity4 = ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:ff")
        self.identity6 = ReservationIdentity("duid", "00:01:02:03")

    def test_rejects_invalid_reservation_invariants(self):
        cases = (
            lambda: IPv6Reservation(
                scope=self.scope6,
                identity=ReservationIdentity("client-id", "01:02"),
                addresses=(),
                delegated_prefixes=(),
            ),
            lambda: IPv4Reservation(scope=self.scope6, identity=self.identity4, addresses=()),
            lambda: IPv4Reservation(scope=self.scope4, identity=self.identity4, addresses=(), hostname=1),
            lambda: IPv4Reservation(
                scope=self.scope4,
                identity=self.identity4,
                addresses=(ip_address("198.18.0.20"), ip_address("198.18.0.20")),
            ),
            lambda: IPv4Reservation(
                scope=self.scope4,
                identity=self.identity4,
                addresses=(ip_address("2001:db8::20"),),
            ),
            lambda: IPv4Reservation(
                scope=self.scope4,
                identity=self.identity4,
                addresses=(ip_address("198.18.1.20"),),
            ),
            lambda: IPv4Reservation(
                scope=self.scope4,
                identity=self.identity4,
                addresses=(ip_address("198.18.0.20"), ip_address("198.18.0.21")),
            ),
            lambda: IPv4Reservation(
                scope=self.scope4,
                identity=self.identity4,
                addresses=(),
                delegated_prefixes=(ip_network("2001:db8:100::/56"),),
            ),
            lambda: IPv6Reservation(
                scope=self.scope6,
                identity=self.identity6,
                addresses=(),
                delegated_prefixes=(ip_network("2001:db8:100::/56"), ip_network("2001:db8:100::/56")),
            ),
            lambda: IPv6Reservation(
                scope=self.scope6,
                identity=self.identity6,
                addresses=(),
                delegated_prefixes=(ip_network("198.18.0.0/24"),),
            ),
            lambda: IPv4Reservation(scope=self.scope4, identity=self.identity4, addresses=(), options=(object(),)),
        )
        for index, build in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    build()

    def test_synchronization_state_factories_cover_all_aggregate_states(self):
        self.assertEqual(ReservationSynchronizationState.from_counts(0, 2).label, "Not Synchronized")
        self.assertEqual(ReservationSynchronizationState.from_counts(1, 2).label, "Partially Synchronized")
        self.assertEqual(ReservationSynchronizationState.from_counts(2, 2).label, "Synchronized")
        self.assertEqual(ReservationSynchronizationState.not_applicable("Global").reason, "Global")
        self.assertEqual(ReservationSynchronizationState.unknown(2, "lookup failed").label, "Unknown")
        with self.assertRaises(ValueError):
            ReservationSynchronizationState.from_counts(3, 2)

    def test_dhcp_option_collection_must_be_a_list_at_the_adapter_boundary(self):
        with self.assertRaisesRegex(ValueError, "must be a list"):
            parse_dhcp_options({})


class TestReservationPage(SimpleTestCase):
    def setUp(self):
        self.client = KeaClient(url="http://kea.example.invalid", send_service=False)

    def test_returns_typed_snapshot_and_quarantines_one_malformed_record(self):
        page = _res_page(
            [
                {
                    "subnet-id": 10,
                    "duid": "00-01-02-03",
                    "ip-addresses": ["2001:db8::20", "2001:db8::10"],
                    "prefixes": ["2001:db8:100::/56"],
                    "hostname": "host.example.invalid",
                    "option-data": [
                        {
                            "code": 23,
                            "space": "dhcp6",
                            "data": "2001:db8::53",
                            "csv-format": True,
                            "always-send": False,
                            "never-send": False,
                        }
                    ],
                },
                {
                    "subnet-id": 10,
                    "remote-id": "relay-value",
                    "ip-addresses": ["2001:db8::30"],
                },
            ],
            next_from=2,
            next_source=1,
        )

        with stub_kea({"reservation-get-page": page}) as kea:
            snapshot = self.client.reservation_page(6, _catalogue(6, 10, "2001:db8::/64"), limit=2)

        self.assertIsInstance(snapshot, ReservationSnapshot)
        self.assertFalse(snapshot.complete)
        self.assertEqual(len(snapshot.records), 1)
        reservation = snapshot.records[0]
        self.assertEqual(reservation.identity, ReservationIdentity("duid", "00:01:02:03"))
        self.assertEqual(
            reservation.scope,
            InSubnetReservationScope(SubnetIdentity(10, ip_network("2001:db8::/64"))),
        )
        self.assertEqual(
            reservation.addresses,
            (ip_address("2001:db8::20"), ip_address("2001:db8::10")),
        )
        self.assertEqual(reservation.delegated_prefixes, (ip_network("2001:db8:100::/56"),))
        self.assertEqual(reservation.hostname, "host.example.invalid")
        self.assertEqual(reservation.options[0].code, 23)
        self.assertEqual(snapshot.diagnostics[0].code, "unsupported-identifier")
        self.assertEqual(snapshot.diagnostics[0].source_position, "hosts[1].remote-id")
        self.assertNotIn("relay-value", snapshot.diagnostics[0].message)
        self.assertIsInstance(snapshot.next_cursor, str)
        self.assertNotEqual(snapshot.next_cursor, "1:2")
        self.assertEqual(
            kea.bodies("reservation-get-page")[0]["arguments"],
            {"source-index": 0, "from": 0, "limit": 2},
        )

    def test_uses_the_opaque_cursor_for_the_next_bounded_page(self):
        hosts = [{"subnet-id": 10, "hw-address": f"aa:bb:cc:dd:ee:{index:02x}"} for index in range(25)]
        first_page = _res_page(hosts, next_from=17, next_source=2)

        with stub_kea({"reservation-get-page": first_page}):
            first = self.client.reservation_page(4, _catalogue(4, 10, "198.18.0.0/24"), limit=25)

        with stub_kea({"reservation-get-page": _res_page([])}) as kea:
            self.client.reservation_page(
                4,
                _catalogue(4, 10, "198.18.0.0/24"),
                cursor=first.next_cursor,
                limit=25,
            )

        self.assertEqual(
            kea.bodies("reservation-get-page")[0]["arguments"],
            {"source-index": 2, "from": 17, "limit": 25},
        )

    def test_fills_one_bounded_page_across_short_backend_source_pages(self):
        configured = [{"subnet-id": 10, "hw-address": f"aa:bb:cc:dd:ee:{index:02x}"} for index in range(6)]
        database = [{"subnet-id": 10, "hw-address": "aa:bb:cc:dd:ef:01"}]
        responses = queued(
            _res_page(configured, next_from=6, next_source=0),
            _res_page(database, next_from=10, next_source=1),
            {"result": 3},
        )

        with stub_kea({"reservation-get-page": responses}) as kea:
            snapshot = self.client.reservation_page(4, _catalogue(4, 10, "198.18.0.0/24"), limit=10)

        self.assertEqual(len(snapshot.records), 7)
        self.assertIsNone(snapshot.next_cursor)
        self.assertEqual(
            [body["arguments"] for body in kea.bodies("reservation-get-page")],
            [
                {"source-index": 0, "from": 0, "limit": 10},
                {"source-index": 0, "from": 6, "limit": 4},
                {"source-index": 1, "from": 10, "limit": 3},
            ],
        )

    def test_rejects_a_malformed_response_envelope(self):
        with stub_kea({"reservation-get-page": []}):
            with self.assertRaisesRegex(RuntimeError, "malformed response"):
                self.client.reservation_page(4, _catalogue(4, 10, "198.18.0.0/24"))

    def test_rejects_a_malformed_page_cursor(self):
        page = {
            "result": 0,
            "arguments": {
                "hosts": [],
                "next": {"source-index": "source", "from": 1},
            },
        }
        with stub_kea({"reservation-get-page": page}):
            with self.assertRaisesRegex(RuntimeError, "malformed next cursor"):
                self.client.reservation_page(4, _catalogue(4, 10, "198.18.0.0/24"))

    def test_preserves_a_global_addressless_reservation(self):
        page = _res_page([{"subnet-id": 0, "circuit-id": "opaque value"}])

        with stub_kea({"reservation-get-page": page}):
            snapshot = self.client.reservation_page(4, _catalogue(4, 10, "198.18.0.0/24"))

        reservation = snapshot.records[0]
        self.assertEqual(reservation.scope, GlobalReservationScope())
        self.assertEqual(reservation.identity, ReservationIdentity("circuit-id", "opaque value"))
        self.assertEqual(reservation.addresses, ())
        self.assertEqual(reservation.delegated_prefixes, ())

    def test_quarantines_malformed_ipv6_records_independently(self):
        valid_identity = {"subnet-id": 10, "duid": "00:01:02:03"}
        hosts = [
            None,
            {"duid": "00:01:02:03"},
            {"subnet-id": 11, "duid": "00:01:02:03"},
            {"subnet-id": 10, "flex-id": 42},
            {**valid_identity, "ip-address": "2001:db8::20"},
            {**valid_identity, "ip-addresses": {}},
            {**valid_identity, "ip-addresses": [1]},
            {**valid_identity, "ip-addresses": ["invalid"]},
            {**valid_identity, "ip-addresses": ["2001:db8::20", "2001:db8::20"]},
            {**valid_identity, "prefixes": {}},
            {**valid_identity, "prefixes": ["invalid"]},
            {**valid_identity, "prefixes": ["2001:db8:100::/56", "2001:db8:100::/56"]},
            {**valid_identity, "hostname": 42},
            {**valid_identity, "option-data": [42]},
        ]

        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            snapshot = self.client.reservation_page(6, _catalogue(6, 10, "2001:db8::/64"))

        self.assertEqual(snapshot.records, ())
        self.assertEqual(len(snapshot.diagnostics), len(hosts))
        self.assertIn("unverified-scope", {diagnostic.code for diagnostic in snapshot.diagnostics})

    def test_quarantines_an_identifier_from_the_other_family(self):
        hosts = [
            {
                "subnet-id": 10,
                "duid": "00:01:02:03",
                "ip-addresses": ["2001:db8::20"],
            },
            {
                "subnet-id": 10,
                "duid": "00:01:02:04",
                "client-id": "01:02",
                "ip-addresses": ["2001:db8::21"],
            },
        ]

        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            snapshot = self.client.reservation_page(6, _catalogue(6, 10, "2001:db8::/64"))

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.records[0].identity, ReservationIdentity("duid", "00:01:02:03"))
        self.assertEqual(snapshot.diagnostics[0].code, "invalid-family-identifier")
        self.assertEqual(snapshot.diagnostics[0].source_position, "hosts[1].client-id")

    def test_quarantines_an_oversized_opaque_identifier(self):
        hosts = [
            {"subnet-id": 10, "duid": "00:01:02:03"},
            {"subnet-id": 10, "flex-id": "x" * 256},
        ]

        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            snapshot = self.client.reservation_page(6, _catalogue(6, 10, "2001:db8::/64"))

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.records[0].identity, ReservationIdentity("duid", "00:01:02:03"))
        self.assertEqual(snapshot.diagnostics[0].code, "invalid-identifier")
        self.assertEqual(snapshot.diagnostics[0].source_position, "hosts[1].flex-id")

    def test_quarantines_non_string_delegated_prefixes(self):
        hosts = [
            {"subnet-id": 10, "duid": "00:01:02:03", "prefixes": ["2001:db8:100::/56"]},
            {"subnet-id": 10, "duid": "00:01:02:04", "prefixes": [42]},
            {"subnet-id": 10, "duid": "00:01:02:05", "prefixes": [True]},
        ]

        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            snapshot = self.client.reservation_page(6, _catalogue(6, 10, "2001:db8::/64"))

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.records[0].identity, ReservationIdentity("duid", "00:01:02:03"))
        self.assertEqual(
            [(diagnostic.code, diagnostic.source_position) for diagnostic in snapshot.diagnostics],
            [
                ("invalid-prefix", "hosts[1].prefixes[0]"),
                ("invalid-prefix", "hosts[2].prefixes[0]"),
            ],
        )

    def test_quarantines_ipv4_collection_and_prefix_fields(self):
        hosts = [
            {"subnet-id": 10, "hw-address": "aa:bb:cc:dd:ee:01", "ip-addresses": []},
            {
                "subnet-id": 10,
                "hw-address": "aa:bb:cc:dd:ee:02",
                "prefixes": ["2001:db8:100::/56"],
            },
        ]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            snapshot = self.client.reservation_page(4, _catalogue(4, 10, "198.18.0.0/24"))

        self.assertEqual(snapshot.records, ())
        self.assertEqual({item.code for item in snapshot.diagnostics}, {"invalid-addresses", "invalid-prefixes"})

    def test_quarantines_an_address_outside_the_verified_scope(self):
        hosts = [
            {
                "subnet-id": 10,
                "hw-address": "aa:bb:cc:dd:ee:01",
                "ip-address": "198.18.0.20",
            },
            {
                "subnet-id": 10,
                "hw-address": "aa:bb:cc:dd:ee:02",
                "ip-address": "198.18.1.20",
            },
        ]

        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            snapshot = self.client.reservation_page(4, _catalogue(4, 10, "198.18.0.0/24"))

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.records[0].identity, ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:01"))
        self.assertEqual(snapshot.diagnostics[0].code, "invalid-address")
        self.assertEqual(snapshot.diagnostics[0].source_position, "hosts[1].ip-address")


class TestReservationExactIdentity(SimpleTestCase):
    def setUp(self):
        self.client = KeaClient(url="http://kea.example.invalid", send_service=False)
        self.catalogue = _catalogue(4, 20, "198.18.0.0/24")
        self.scope = InSubnetReservationScope(SubnetIdentity(20, ip_network("198.18.0.0/24")))

    def test_returns_the_one_typed_target_for_normalized_identity(self):
        identity = ReservationIdentity("client-id", "01:aa:bb:cc:dd:ee:ff")
        response = _res_get(
            {
                "subnet-id": 20,
                "client-id": "01-AA-BB-CC-DD-EE-FF",
                "ip-address": "198.18.0.20",
            }
        )

        with stub_kea({"reservation-get": response}) as kea:
            reservation = self.client.reservation_by_identity(4, self.catalogue, self.scope, identity)

        self.assertEqual(reservation.identity, identity)
        self.assertEqual(reservation.addresses, (ip_address("198.18.0.20"),))
        self.assertEqual(
            kea.bodies("reservation-get")[0]["arguments"],
            {
                "subnet-id": 20,
                "identifier-type": "client-id",
                "identifier": "01:aa:bb:cc:dd:ee:ff",
            },
        )

    def test_fails_closed_when_the_target_has_multiple_identifiers(self):
        response = _res_get(
            {
                "subnet-id": 20,
                "client-id": "01:aa:bb:cc:dd:ee:ff",
                "hw-address": "aa:bb:cc:dd:ee:ff",
            }
        )

        with stub_kea({"reservation-get": response}):
            with self.assertRaisesRegex(MalformedReservation, "exactly one"):
                self.client.reservation_by_identity(
                    4,
                    self.catalogue,
                    self.scope,
                    ReservationIdentity("client-id", "01:aa:bb:cc:dd:ee:ff"),
                )


class TestReservationScopedAddress(SimpleTestCase):
    def setUp(self):
        self.client = KeaClient(url="http://kea.example.invalid", send_service=False)
        self.catalogue = _catalogue(4, 20, "198.18.0.0/24")
        self.scope = InSubnetReservationScope(SubnetIdentity(20, ip_network("198.18.0.0/24")))

    def test_resolves_an_address_to_the_canonical_typed_identity(self):
        response = _res_get(
            {
                "subnet-id": 20,
                "hw-address": "AA-BB-CC-DD-EE-FF",
                "ip-address": "198.18.0.20",
            }
        )

        with stub_kea({"reservation-get": response}) as kea:
            reservation = self.client.reservation_by_address(
                4,
                self.catalogue,
                self.scope,
                "198.18.0.20",
            )

        self.assertEqual(reservation.identity, ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:ff"))
        self.assertEqual(
            kea.bodies("reservation-get")[0]["arguments"],
            {"subnet-id": 20, "ip-address": "198.18.0.20"},
        )

    def test_rejects_address_discovery_in_global_scope_without_a_request(self):
        with stub_kea({}) as kea:
            with self.assertRaisesRegex(ValueError, "In-Subnet"):
                self.client.reservation_by_address(
                    4,
                    self.catalogue,
                    GlobalReservationScope(),
                    "198.18.0.20",
                )

        self.assertEqual(kea.commands(), [])


class TestReservationHostname(SimpleTestCase):
    def test_returns_all_valid_matches_and_quarantines_a_malformed_match(self):
        client = KeaClient(url="http://kea.example.invalid", send_service=False)
        response = {
            "result": 0,
            "arguments": {
                "hosts": [
                    {
                        "subnet-id": 10,
                        "duid": "00:01:02:03",
                        "ip-addresses": ["2001:db8::10"],
                        "hostname": "host.example.invalid",
                    },
                    {
                        "subnet-id": 10,
                        "duid": "00:01:02:04",
                        "hw-address": "aa:bb:cc:dd:ee:ff",
                        "hostname": "host.example.invalid",
                    },
                ]
            },
        }

        with stub_kea({"reservation-get-by-hostname": response}) as kea:
            snapshot = client.reservations_by_hostname(
                6,
                _catalogue(6, 10, "2001:db8::/64"),
                "host.example.invalid",
            )

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.records[0].identity, ReservationIdentity("duid", "00:01:02:03"))
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.diagnostics[0].code, "ambiguous-identifier")
        self.assertIsNone(snapshot.next_cursor)
        self.assertEqual(
            kea.bodies("reservation-get-by-hostname")[0]["arguments"],
            {"hostname": "host.example.invalid"},
        )

    def test_quarantines_results_that_do_not_match_the_requested_hostname(self):
        client = KeaClient(url="http://kea.example.invalid", send_service=False)
        response = {
            "result": 0,
            "arguments": {
                "hosts": [
                    {
                        "subnet-id": 10,
                        "duid": "00:01:02:03",
                        "hostname": "host.example.invalid",
                    },
                    {
                        "subnet-id": 10,
                        "duid": "00:01:02:04",
                        "hostname": "different.example.invalid",
                    },
                    {"subnet-id": 10, "duid": "00:01:02:05"},
                ]
            },
        }

        with stub_kea({"reservation-get-by-hostname": response}):
            snapshot = client.reservations_by_hostname(
                6,
                _catalogue(6, 10, "2001:db8::/64"),
                "host.example.invalid",
            )

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.records[0].identity, ReservationIdentity("duid", "00:01:02:03"))
        self.assertEqual(
            [(diagnostic.code, diagnostic.source_position) for diagnostic in snapshot.diagnostics],
            [
                ("target-mismatch", "hosts[1].hostname"),
                ("target-mismatch", "hosts[2].hostname"),
            ],
        )
        self.assertFalse(snapshot.complete)


class TestReservationIteration(SimpleTestCase):
    def test_rejects_an_unsupported_snapshot_family(self):
        client = KeaClient(url="http://kea.example.invalid", send_service=False)

        with self.assertRaisesRegex(ValueError, "version must be 4 or 6"):
            client.reservation_snapshot(5, _catalogue(4, 20, "198.18.0.0/24"))

    def test_combines_bounded_pages_and_preserves_incomplete_diagnostics(self):
        first = _res_page(
            [
                {"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:01", "ip-address": "198.18.0.20"},
                {"subnet-id": 20, "remote-id": "relay-value"},
            ],
            next_from=2,
            next_source=1,
        )
        final = _res_page([{"subnet-id": 20, "client-id": "01:aa:bb:cc:dd:ee:02", "ip-address": "198.18.0.21"}])
        client = KeaClient(url="http://kea.example.invalid", send_service=False)

        with stub_kea({"reservation-get-page": queued(first, final)}) as kea:
            snapshot = client.reservation_snapshot(4, _catalogue(4, 20, "198.18.0.0/24"), page_size=2)

        self.assertEqual(len(snapshot.records), 2)
        self.assertEqual(len(snapshot.diagnostics), 1)
        self.assertFalse(snapshot.complete)
        self.assertIsNone(snapshot.next_cursor)
        self.assertEqual(len(kea.bodies("reservation-get-page")), 2)

    def test_rejects_a_cursor_that_does_not_advance(self):
        stalled = _res_page([], next_from=2, next_source=1)
        client = KeaClient(url="http://kea.example.invalid", send_service=False)

        with stub_kea({"reservation-get-page": stalled}):
            with self.assertRaisesRegex(RuntimeError, "did not advance"):
                client.reservation_snapshot(4, _catalogue(4, 20, "198.18.0.0/24"), page_size=2)

    def test_bounds_empty_pages_whose_cursor_keeps_advancing(self):
        """Stop a backend that answers empty pages forever with a new cursor each time.

        Only a non-empty page moves the traversal towards its limit, and the repeated
        cursor check never fires while the cursor keeps changing, so the request loop
        needs its own bound.
        """
        advancing = queued(*(_res_page([], next_from=index, next_source=1) for index in range(2, 12)))
        client = KeaClient(url="http://kea.example.invalid", send_service=False)

        with stub_kea({"reservation-get-page": advancing}) as kea:
            with self.assertRaisesRegex(RuntimeError, "only empty pages"):
                client.reservation_page(4, _catalogue(4, 20, "198.18.0.0/24"), limit=2)

        self.assertEqual(len(kea.bodies("reservation-get-page")), 9)

    def test_preserves_records_and_reports_a_cursor_cycle_across_pages(self):
        pages = queued(
            _res_page([{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:01"}], next_from=1, next_source=1),
            _res_page([{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:02"}], next_from=2, next_source=1),
            _res_page([{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:03"}], next_from=1, next_source=1),
        )
        client = KeaClient(url="http://kea.example.invalid", send_service=False)

        with stub_kea({"reservation-get-page": pages}):
            snapshot = client.reservation_snapshot(4, _catalogue(4, 20, "198.18.0.0/24"), page_size=1)

        self.assertEqual(len(snapshot.records), 3)
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.diagnostics[-1].code, "pagination-stalled")
        self.assertEqual(snapshot.diagnostics[-1].source_position, "pages[2].next")


class TestReservationMutation(SimpleTestCase):
    def setUp(self):
        self.client = KeaClient(url="http://kea.example.invalid", send_service=False)
        self.catalogue = _catalogue(4, 20, "198.18.0.0/24")
        self.scope = InSubnetReservationScope(SubnetIdentity(20, ip_network("198.18.0.0/24")))
        self.reservation = IPv4Reservation(
            scope=self.scope,
            identity=ReservationIdentity("hw-address", "aa:bb:cc:dd:ee:ff"),
            addresses=(ip_address("198.18.0.20"),),
            hostname="old.example.invalid",
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
        )

    def test_create_sends_managed_facts_and_returns_a_verified_typed_result(self):
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
            "hostname": "old.example.invalid",
            "option-data": [
                {
                    "code": 6,
                    "name": "domain-name-servers",
                    "space": "dhcp4",
                    "data": "198.18.0.53",
                    "csv-format": True,
                    "always-send": False,
                    "never-send": False,
                }
            ],
        }
        with stub_kea(
            {
                **_persistence_responses(4),
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(raw),
            }
        ) as kea:
            result = self.client.reservation_create(self.reservation, self.catalogue)

        self.assertIsNone(result.previous)
        self.assertEqual(result.intended, self.reservation)
        self.assertEqual(result.application, "applied")
        self.assertEqual(result.persistence, "persisted")
        self.assertEqual(result.verification, "verified")
        self.assertEqual(kea.bodies("reservation-add")[0]["arguments"]["reservation"], raw)
        self.assertEqual(kea.commands().count("config-write"), 1)

    def test_update_preserves_unknown_fields_and_applies_explicit_clear_and_set(self):
        current_raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
            "hostname": "old.example.invalid",
            "option-data": [
                {
                    "code": 6,
                    "name": "domain-name-servers",
                    "space": "dhcp4",
                    "data": "198.18.0.53",
                    "csv-format": True,
                    "always-send": False,
                    "never-send": False,
                }
            ],
            "user-context": {"owner": "external-system"},
            "client-classes": ["external-class"],
        }
        intended_raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.21",
            "user-context": {"owner": "external-system"},
            "client-classes": ["external-class"],
        }
        change = ReservationChange(
            addresses=SetValue((ip_address("198.18.0.21"),)),
            hostname=ClearValue(),
            options=ClearValue(),
        )

        with stub_kea(
            {
                **_persistence_responses(4),
                "reservation-get": queued(_res_get(current_raw), _res_get(intended_raw)),
                "reservation-update": {"result": 0},
            }
        ) as kea:
            result = self.client.reservation_change(
                self.reservation,
                reservation_fingerprint(self.reservation),
                change,
                self.catalogue,
            )

        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(sent["user-context"], {"owner": "external-system"})
        self.assertEqual(sent["client-classes"], ["external-class"])
        self.assertEqual(sent["ip-address"], "198.18.0.21")
        self.assertNotIn("hostname", sent)
        self.assertNotIn("option-data", sent)
        self.assertEqual(result.previous, self.reservation)
        self.assertEqual(result.intended.addresses, (ip_address("198.18.0.21"),))
        self.assertEqual(result.verification, "verified")

    def test_create_reports_failed_persistence_without_losing_applied_state(self):
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
            "hostname": "old.example.invalid",
            "option-data": [
                {
                    "code": 6,
                    "name": "domain-name-servers",
                    "space": "dhcp4",
                    "data": "198.18.0.53",
                    "csv-format": True,
                    "always-send": False,
                    "never-send": False,
                }
            ],
        }
        with stub_kea(
            {
                **_persistence_responses(4),
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(raw),
                "config-write": {"result": 1, "text": "write failed"},
            }
        ):
            result = self.client.reservation_create(self.reservation, self.catalogue)

        self.assertEqual(result.application, "applied")
        self.assertEqual(result.persistence, "failed")
        self.assertEqual(result.verification, "verified")

    def test_create_reports_verification_failure_without_losing_applied_state(self):
        with stub_kea(
            {
                **_persistence_responses(4),
                "reservation-add": {"result": 0},
                "reservation-get": {"result": 3},
            }
        ):
            result = self.client.reservation_create(self.reservation, self.catalogue)

        self.assertEqual(result.application, "applied")
        self.assertEqual(result.persistence, "persisted")
        self.assertEqual(result.verification, "failed")

    def test_update_rejects_a_stale_managed_fingerprint_before_writing(self):
        changed_raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
            "hostname": "changed.example.invalid",
        }
        with stub_kea({"reservation-get": _res_get(changed_raw)}) as kea:
            with self.assertRaises(ReservationConflict):
                self.client.reservation_change(
                    self.reservation,
                    reservation_fingerprint(self.reservation),
                    ReservationChange(hostname=SetValue("new.example.invalid")),
                    self.catalogue,
                )

        self.assertNotIn("reservation-update", kea.commands())

    def test_delete_uses_identity_and_verifies_absence(self):
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
            "hostname": "old.example.invalid",
            "option-data": [
                {
                    "code": 6,
                    "name": "domain-name-servers",
                    "space": "dhcp4",
                    "data": "198.18.0.53",
                    "csv-format": True,
                    "always-send": False,
                    "never-send": False,
                }
            ],
        }
        with stub_kea(
            {
                **_persistence_responses(4),
                "reservation-get": queued(_res_get(raw), {"result": 3}),
                "reservation-del": {"result": 0},
            }
        ) as kea:
            result = self.client.reservation_delete(self.reservation, self.catalogue)

        self.assertEqual(result.previous, self.reservation)
        self.assertIsNone(result.intended)
        self.assertEqual(result.verification, "verified")
        self.assertEqual(
            kea.bodies("reservation-del")[0]["arguments"],
            {
                "subnet-id": 20,
                "identifier-type": "hw-address",
                "identifier": "aa:bb:cc:dd:ee:ff",
            },
        )

    def test_create_reports_when_persistence_is_not_requested(self):
        client = KeaClient(url="http://kea.example.invalid", send_service=False, persist_config=False)
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
            "hostname": "old.example.invalid",
            "option-data": [
                {
                    "code": 6,
                    "name": "domain-name-servers",
                    "space": "dhcp4",
                    "data": "198.18.0.53",
                    "csv-format": True,
                    "always-send": False,
                    "never-send": False,
                }
            ],
        }
        with stub_kea({"reservation-add": {"result": 0}, "reservation-get": _res_get(raw)}) as kea:
            result = client.reservation_create(self.reservation, self.catalogue)

        self.assertEqual(result.persistence, "not-requested")
        self.assertNotIn("config-write", kea.commands())


class TestReservationCapabilities(SimpleTestCase):
    def test_uses_live_family_configuration_and_flex_id_hook(self):
        client = KeaClient(url="http://kea.example.invalid", send_service=False)
        commands = ["reservation-get", "reservation-add", "reservation-update", "reservation-del"]
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "host-reservation-identifiers": ["hw-address", "flex-id", "remote-id"],
                    "hooks-libraries": [{"library": "/usr/lib/kea/hooks/libdhcp_flex_id.so"}],
                }
            },
        }

        with stub_kea({"list-commands": {"result": 0, "arguments": commands}, "config-get": config}):
            capabilities = client.reservation_capabilities(4)

        self.assertIsInstance(capabilities, ReservationCapabilities)
        self.assertEqual(capabilities.family, 4)
        self.assertEqual(capabilities.identifiers, ("hw-address", "flex-id"))
        self.assertTrue(capabilities.mutation_available)
        self.assertEqual(capabilities.explanation, "")
        self.assertIn(
            ("duid", "This identifier is not enabled in host-reservation-identifiers."),
            capabilities.unavailable_identifiers,
        )

    def test_missing_host_command_disables_mutation(self):
        client = KeaClient(url="http://kea.example.invalid", send_service=False)
        config = {"result": 0, "arguments": {"Dhcp6": {"host-reservation-identifiers": ["duid"]}}}

        with stub_kea(
            {
                "list-commands": {"result": 0, "arguments": ["reservation-get"]},
                "config-get": config,
            }
        ):
            capabilities = client.reservation_capabilities(6)

        self.assertFalse(capabilities.mutation_available)
        self.assertEqual(capabilities.identifiers, ("duid",))
        self.assertIn("host_cmds", capabilities.explanation)
