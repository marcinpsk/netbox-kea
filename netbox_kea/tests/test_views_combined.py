# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for netbox_kea plugin.

Also contains pure-Python unit tests for helper functions defined in views.py
(e.g. ``_extract_identifier``), which do not require a database but live here
because they are tightly coupled to view logic.

These tests drive the **real** ``KeaClient``; only the HTTP boundary is stubbed
via ``kea_stub.stub_kea``, so the combined multi-server fetch helpers exercise the
real request/response path.
"""

import requests
from django.test import override_settings
from django.urls import reverse

from .kea_stub import _catalogue_responses, _res_page, stub_kea
from .utils import _PLUGINS_CONFIG, _ViewTestBase


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchSharedNetworksFromServer(_ViewTestBase):
    """_fetch_shared_networks_from_server with null config-get arguments raises RuntimeError."""

    def test_null_config_raises_runtime_error(self):
        from netbox_kea.views import _fetch_shared_networks_from_server

        with stub_kea({"config-get": [{"result": 0, "arguments": None}]}):
            with self.assertRaises(RuntimeError):
                _fetch_shared_networks_from_server(self.server, version=4)


# ---------------------------------------------------------------------------
# Combined fetch helpers — response-shape guards
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedResponseShapeGuards(_ViewTestBase):
    """An empty Kea response list must raise RuntimeError before indexing ``resp[0]``.

    ``KeaClient.command`` only guarantees a *list*; ``check_response`` iterating an
    empty list raises nothing, so a helper that indexes ``resp[0]["result"]`` would
    blow up with ``IndexError``. These combined helpers guard with
    ``_require_first_entry`` and raise ``RuntimeError`` instead (CLAUDE.md: "Validate
    Kea response shape before indexing … raise RuntimeError").

    A *non-dict* first entry (e.g. ``["not-a-dict"]``) is not exercised here: with the
    real client it never reaches the helper's guard because ``check_response`` (which
    every one of these commands runs with ``check=(0, 3)``) indexes ``entry["result"]``
    first and raises on the non-subscriptable entry. The empty-list case below covers
    the ``_require_first_entry`` guard through a response the real client can produce.
    """

    def test_leases_empty_response_raises_runtime_error(self):
        from netbox_kea import constants
        from netbox_kea.views import _fetch_leases_from_server

        with stub_kea({"lease4-get": []}):
            with self.assertRaises(RuntimeError):
                _fetch_leases_from_server(self.server, "10.0.0.1", constants.BY_IP, 4)

    def test_all_leases_empty_response_raises_runtime_error(self):
        from netbox_kea.views import _fetch_all_leases_from_server

        with stub_kea({"lease4-get-page": []}):
            with self.assertRaises(RuntimeError):
                _fetch_all_leases_from_server(self.server, 4)

    def test_shared_networks_empty_response_raises_runtime_error(self):
        from netbox_kea.views import _fetch_shared_networks_from_server

        with stub_kea({"config-get": []}):
            with self.assertRaises(RuntimeError):
                _fetch_shared_networks_from_server(self.server, 4)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedSubnetDiagnostics(_ViewTestBase):
    def test_complete_catalogue_is_reused_for_unchanged_requests(self):
        url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.server.pk}"
        identity = {"result": 0, "arguments": {"subnets": [{"id": 1, "subnet": "198.18.1.0/24"}]}}
        configuration = {
            "result": 0,
            "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "198.18.1.0/24", "pools": []}]}},
        }

        with stub_kea(
            {
                "subnet4-list": identity,
                "config-get": configuration,
                "stat-lease4-get": {"result": 2, "text": "unknown command"},
            }
        ) as kea:
            first_response = self.client.get(url)
            second_response = self.client.get(url)

        self.assertContains(first_response, "198.18.1.0/24")
        self.assertContains(second_response, "198.18.1.0/24")
        self.assertEqual(kea.commands().count("subnet4-list"), 1)
        self.assertEqual(kea.commands().count("config-get"), 1)

    def test_unavailable_catalogue_preserves_specific_diagnostics(self):
        url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.server.pk}"

        with stub_kea(
            {
                "subnet4-list": requests.ConnectionError("identity unavailable"),
                "config-get": requests.ConnectionError("configuration unavailable"),
            }
        ):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea subnet identity facts are unavailable.")
        self.assertContains(response, "Kea Subnet configuration facts are unavailable.")
        self.assertNotContains(response, "Failed to query server")

    def test_empty_config_response_preserves_confirmed_empty_identity(self):
        from netbox_kea.views import _fetch_subnets_from_server

        with stub_kea(
            {
                "subnet4-list": {"result": 3, "text": "no subnets"},
                "config-get": [],
                "stat-lease4-get": {"result": 2, "text": "unknown command"},
            }
        ):
            subnets, diagnostics = _fetch_subnets_from_server(self.server, 4)

        self.assertEqual(subnets, [])
        # Assert the diagnostic itself: assertTrue(diagnostics) also passed when the
        # catalogue reported unavailable instead of a confirmed-empty identity, or
        # when a different code was emitted.
        self.assertIn("malformed-configuration-response", [diagnostic.code for diagnostic in diagnostics])

    def test_incomplete_catalogue_explains_omitted_facts(self):
        responses = {
            "subnet4-list": {
                "result": 0,
                "arguments": {"subnets": [{"id": 1, "subnet": "198.18.0.0/24"}]},
            },
            "config-get": {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [{"id": 1, "subnet": "198.18.0.0/24", "pools": "not-a-list"}],
                    }
                },
            },
            "stat-lease4-get": {"result": 2, "text": "unknown command"},
        }
        url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.server.pk}"

        with stub_kea(responses):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "198.18.0.0/24")
        self.assertContains(response, "Kea returned a non-list Pool collection.")

    def test_repeated_catalogue_diagnostics_render_once(self):
        responses = {
            "subnet4-list": {
                "result": 0,
                "arguments": {"subnets": [{"id": 1, "subnet": "198.18.0.0/24"}]},
            },
            "config-get": {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [
                            {
                                "id": 1,
                                "subnet": "198.18.0.0/24",
                                "pools": ["invalid-one", "invalid-two"],
                            }
                        ],
                    }
                },
            },
            "stat-lease4-get": {"result": 2, "text": "unknown command"},
        }
        url = reverse("plugins:netbox_kea:combined_subnets4") + f"?server={self.server.pk}"

        with stub_kea(responses):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea returned an invalid Pool.", count=1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservationsWithoutAddress(_ViewTestBase):
    """The global reservations tab hits the same address-less crash as the per-server tab (#110)."""

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:combined_reservations{version}") + f"?server={self.server.pk}"

    def test_v4_identifier_only_reservation_renders(self):
        page = _res_page([{"subnet-id": 3742, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "printer-1"}])
        with stub_kea(
            {
                **_catalogue_responses(4, 3742, "198.18.0.0/24"),
                "reservation-get-page": page,
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        ):
            response = self.client.get(self._url(4))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"printer-1", response.content)

    def test_v6_prefix_only_reservation_renders(self):
        page = _res_page([{"subnet-id": 12, "duid": "00:01:00:01:12:34", "prefixes": ["2001:db8:1::/64"]}])
        with stub_kea(
            {
                **_catalogue_responses(6, 12, "2001:db8::/48"),
                "reservation-get-page": page,
                "lease6-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        ):
            response = self.client.get(self._url(6))
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservationsShowWhatIsReserved(_ViewTestBase):
    """The global tab shows the identifier and prefixes an address-less host reserves.

    Rendering 200 only proves the crash is gone; these assert the row actually says
    what the reservation is, which is the point of showing it at all.
    """

    def _url(self, version=4, query=""):
        base = reverse(f"plugins:netbox_kea:combined_reservations{version}") + f"?server={self.server.pk}"
        return base + query

    def _stub(self, hosts, version=4):
        cidr = "198.18.0.0/24" if version == 4 else "2001:db8::/48"
        return {
            **_catalogue_responses(version, hosts[0]["subnet-id"], cidr),
            "reservation-get-page": _res_page(hosts),
            f"lease{version}-get-by-state": {"result": 0, "arguments": {"leases": []}},
        }

    def test_v4_flex_id_row_shows_the_identifier_and_its_type(self):
        hosts = [{"subnet-id": 1, "flex-id": "vendor-42", "hostname": "kiosk"}]
        with stub_kea(self._stub(hosts)):
            response = self.client.get(self._url(4))
        body = response.content.decode()
        self.assertIn("vendor-42", body)
        self.assertIn("flex-id", body)

    def test_v6_prefix_only_row_shows_its_prefixes_and_no_address(self):
        hosts = [{"subnet-id": 12, "duid": "00:01:00:01:12:34", "prefixes": ["2001:db8:1::/64"]}]
        with stub_kea(self._stub(hosts, version=6)):
            response = self.client.get(self._url(6))
        body = response.content.decode()
        self.assertIn("2001:db8:1::/64", body)
        self.assertIn("No address", body)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservationSyncControl(_ViewTestBase):
    """The combined tab must offer the same Reservation synchronization as one server.

    The table cell renders the Sync all button only from ``sync_url``, which the
    enrichment sets only for a caller that passes ``can_sync``. The combined view left
    it out, so the button never rendered there.
    """

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:combined_reservations{version}") + f"?server={self.server.pk}"

    def test_unsynchronized_row_offers_the_sync_control(self):
        hosts = [{"subnet-id": 1, "hw-address": "aa:bb:cc:00:00:01", "ip-address": "198.18.0.10"}]
        stub = {
            **_catalogue_responses(4, 1, "198.18.0.0/24"),
            "reservation-get-page": _res_page(hosts),
            "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
        }

        with stub_kea(stub):
            response = self.client.get(self._url(4))

        body = response.content.decode()
        sync_url = reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk, 1])
        self.assertIn("Not Synchronized", body)
        self.assertIn(sync_url, body)
        self.assertIn("Sync all", body)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservationCursorPagination(_ViewTestBase):
    """The Next page link must carry the encoded per-server cursor, not just exist."""

    def _url(self, version=4, query=""):
        return reverse(f"plugins:netbox_kea:combined_reservations{version}") + f"?server={self.server.pk}{query}"

    #: The view asks for 100 records; only a full page can carry a next cursor.
    PAGE_SIZE = 100

    def _stub(self, *, next_from=0, next_source=0, hosts=None):
        if hosts is None:
            hosts = [
                {
                    "subnet-id": 1,
                    "hw-address": f"aa:bb:cc:00:{index // 256:02x}:{index % 256:02x}",
                    "ip-address": f"198.18.{index // 256}.{index % 256}",
                }
                for index in range(self.PAGE_SIZE)
            ]
        return {
            **_catalogue_responses(4, 1, "198.18.0.0/16"),
            "reservation-get-page": _res_page(hosts, next_from=next_from, next_source=next_source),
            "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
        }

    def test_next_page_url_carries_the_encoded_cursor(self):
        # Kea's two-part cursor (source-index 1, from 5) as one opaque base64url token.
        with stub_kea(self._stub(next_from=5, next_source=1)) as kea:
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(kea.bodies("reservation-get-page")), 1)
        next_page_url = response.context["next_page_url"]
        self.assertIsNotNone(next_page_url)
        self.assertIn(f"reservation_cursor_{self.server.pk}=WzEsNV0", next_page_url)
        self.assertContains(response, "Next page")

    def test_the_cursor_round_trips_to_the_next_reservation_page_request(self):
        with stub_kea(self._stub(next_from=5, next_source=1)):
            first = self.client.get(self._url())
        next_page_url = first.context["next_page_url"]

        # A short terminal page ends the traversal on the second request.
        terminal = [{"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.9.9"}]
        with stub_kea(self._stub(hosts=terminal)) as kea:
            second = self.client.get(next_page_url)

        self.assertEqual(second.status_code, 200)
        body = kea.bodies("reservation-get-page")[0]
        # The decoded cursor must reach Kea as its native from/source-index pair.
        self.assertEqual(body["arguments"]["from"], 5)
        self.assertEqual(body["arguments"]["source-index"], 1)

    def test_an_exhausted_source_offers_no_next_page(self):
        terminal = [{"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.9.9"}]
        with stub_kea(self._stub(hosts=terminal)):
            response = self.client.get(self._url())

        self.assertIsNone(response.context["next_page_url"])
        self.assertNotContains(response, "Next page")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationIdentifierColumns(_ViewTestBase):
    """A typed identifier column must stay empty for every other identifier type.

    The reservation row carries one ``identifier`` plus its ``identifier_type``, so a
    column bound straight to ``identifier`` showed a client-id under "Hardware
    Address" and a hw-address under "DUID".
    """

    def _stub(self, hosts, version=4):
        cidr = "198.18.0.0/24" if version == 4 else "2001:db8::/48"
        return {
            **_catalogue_responses(version, 1, cidr),
            "reservation-get-page": _res_page(hosts),
            f"lease{version}-get-by-state": {"result": 0, "arguments": {"leases": []}},
        }

    def _table(self, version, hosts):
        url = reverse(f"plugins:netbox_kea:combined_reservations{version}") + f"?server={self.server.pk}"
        with stub_kea(self._stub(hosts, version=version)):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.context["table"]

    def _cell(self, table, column_name):
        row = next(iter(table.rows))
        return str(row.get_cell(column_name))

    def test_v4_hw_address_column_is_empty_for_a_client_id_reservation(self):
        table = self._table(4, [{"subnet-id": 1, "client-id": "01aabbccddeeff", "hostname": "kiosk"}])
        # The domain value normalizes a client-id to colon-separated octets.
        normalized = "01:aa:bb:cc:dd:ee:ff"

        self.assertNotIn(normalized, self._cell(table, "hw_address"))
        self.assertIn(normalized, self._cell(table, "identifier"))
        self.assertIn("client-id", self._cell(table, "identifier"))

    def test_v4_hw_address_column_shows_a_hw_address_reservation(self):
        table = self._table(4, [{"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "printer"}])

        self.assertIn("aa:bb:cc:dd:ee:ff", self._cell(table, "hw_address"))

    def test_v6_duid_column_is_empty_for_a_hw_address_reservation(self):
        table = self._table(6, [{"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "sensor"}])

        self.assertNotIn("aa:bb:cc:dd:ee:ff", self._cell(table, "duid"))
        self.assertIn("aa:bb:cc:dd:ee:ff", self._cell(table, "identifier"))

    def test_v6_duid_column_shows_a_duid_reservation(self):
        table = self._table(6, [{"subnet-id": 1, "duid": "00:01:00:01:12:34", "hostname": "laptop"}])

        self.assertIn("00:01:00:01:12:34", self._cell(table, "duid"))
