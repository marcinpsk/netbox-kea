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

from .kea_stub import queued, stub_kea
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

    def test_subnets_empty_response_raises_runtime_error(self):
        from netbox_kea.views import _fetch_subnets_from_server

        with stub_kea({"config-get": []}):
            with self.assertRaises(RuntimeError):
                _fetch_subnets_from_server(self.server, 4)

    def test_shared_networks_empty_response_raises_runtime_error(self):
        from netbox_kea.views import _fetch_shared_networks_from_server

        with stub_kea({"config-get": []}):
            with self.assertRaises(RuntimeError):
                _fetch_shared_networks_from_server(self.server, 4)


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
            # active-lease badge enrichment queries lease4-get-all per unique subnet
            "lease4-get-all": {"result": 0, "arguments": {"leases": []}},
        }
        with stub_kea(stub) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(kea.bodies("reservation-get-page")), 2)
        self.assertIn(b"10.0.0.1", response.content)
        self.assertIn(b"10.0.0.2", response.content)
