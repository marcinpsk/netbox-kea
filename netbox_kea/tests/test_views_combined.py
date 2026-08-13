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

from django.test import override_settings
from django.urls import reverse

from .kea_stub import _res_page, queued, stub_kea
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
        self.assertTrue(diagnostics)

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
class TestCombinedReservationsMultiPage(_ViewTestBase):
    """Combined reservations must follow reservation-get-page across multiple pages."""

    def _url(self):
        return reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.server.pk}"

    def test_multi_page_pagination_followed(self):
        """from/source-index advance across pages until the cursor is exhausted."""
        page1 = {
            "result": 0,
            "arguments": {
                "hosts": [{"subnet-id": 1, "ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:01"}],
                "next": {"from": 1, "source-index": 1},  # not exhausted
            },
        }
        page2 = {
            "result": 0,
            "arguments": {
                "hosts": [{"subnet-id": 1, "ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:dd:ee:02"}],
                "next": {"from": 0, "source-index": 0},  # exhausted
            },
        }
        stub = {
            "reservation-get-page": queued(page1, page2),
            # active-lease badge enrichment queries lease4-get-by-state per unique subnet
            "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
        }
        with stub_kea(stub) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(kea.bodies("reservation-get-page")), 2)
        self.assertIn(b"10.0.0.1", response.content)
        self.assertIn(b"10.0.0.2", response.content)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservationsWithoutAddress(_ViewTestBase):
    """The global reservations tab hits the same address-less crash as the per-server tab (#110)."""

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:combined_reservations{version}") + f"?server={self.server.pk}"

    def test_v4_identifier_only_reservation_renders(self):
        page = _res_page([{"subnet-id": 3742, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "printer-1"}])
        with stub_kea(
            {"reservation-get-page": page, "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}}}
        ):
            response = self.client.get(self._url(4))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"printer-1", response.content)

    def test_v6_prefix_only_reservation_renders(self):
        page = _res_page([{"subnet-id": 12, "duid": "00:01:00:01:12:34", "prefixes": ["2001:db8:1::/64"]}])
        with stub_kea(
            {"reservation-get-page": page, "lease6-get-by-state": {"result": 0, "arguments": {"leases": []}}}
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
        return {
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

    def test_csv_export_renders_every_row(self):
        """Export goes through the real ?export path, so a missing accessor would raise."""
        hosts = [
            {"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:01", "ip-address": "10.0.0.1"},
            {"subnet-id": 1, "flex-id": "vendor-42", "hostname": "kiosk"},
        ]
        with stub_kea(self._stub(hosts)):
            response = self.client.get(self._url(4, "&export"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("10.0.0.1", body)
        self.assertIn("vendor-42", body)
        self.assertEqual(len([line for line in body.splitlines() if line.strip()]), 3)  # header + 2 rows

    def test_v6_csv_export_carries_prefixes(self):
        hosts = [{"subnet-id": 12, "duid": "00:01:00:01:12:34", "prefixes": ["2001:db8:1::/64"]}]
        with stub_kea(self._stub(hosts, version=6)):
            response = self.client.get(self._url(6, "&export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("2001:db8:1::/64", response.content.decode())
