# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Direct unit tests for api/views.py — no database required.

Covers all paths in _lease_search, _fetch_leases, _reservation_search, and
_fetch_reservations.  The TestCase-based api test files cover the same happy
paths via a real DB + HTTP stack; these SimpleTestCase tests provide fast,
DB-free coverage for every branch.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import requests as rq
from django.test import SimpleTestCase, override_settings
from rest_framework import status

from netbox_kea.api.views import ServerViewSet
from netbox_kea.kea import KeaException

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

# Minimal KeaResponse dict accepted by KeaException.__init__
_KEA_ERR_RESP = {"result": 1, "text": "command failed"}


def _make_view():
    """Return a ServerViewSet with get_object() mocked to return a mock Server."""
    view = ServerViewSet()
    view.kwargs = {}
    view.format_kwarg = None
    mock_server = MagicMock()
    mock_server.name = "test-server"
    view.get_object = MagicMock(return_value=mock_server)
    view.check_permissions = MagicMock()
    view.check_object_permissions = MagicMock()
    return view, mock_server


def _make_request(query_params: dict):
    """Return a minimal mock request with the given query_params dict."""
    req = MagicMock()
    req.query_params = query_params
    return req


def _view_with_command(command_return):
    """Return (view, mock_client) with client.command pre-configured."""
    view, server = _make_view()
    mock_client = MagicMock()
    mock_client.command.return_value = command_return
    server.get_client.return_value = mock_client
    return view, mock_client


# ─────────────────────────────────────────────────────────────────────────────
# leases4 / leases6 action dispatch
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseActionDispatch(SimpleTestCase):
    """leases4() and leases6() dispatch to _lease_search with the right version."""

    def test_leases4_dispatches_with_version_4(self):
        view, server = _make_view()
        mock_client = MagicMock()
        mock_client.command.return_value = [{"result": 0, "arguments": None}]
        server.get_client.return_value = mock_client
        response = view.leases4(_make_request({"ip_address": "10.0.0.1"}), pk=1)
        server.get_client.assert_called_once_with(version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_leases6_dispatches_with_version_6(self):
        view, server = _make_view()
        mock_client = MagicMock()
        mock_client.command.return_value = [{"result": 0, "arguments": None}]
        server.get_client.return_value = mock_client
        response = view.leases6(_make_request({"ip_address": "2001:db8::1"}), pk=1)
        server.get_client.assert_called_once_with(version=6)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
# _lease_search — parameter validation and error handling
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchValidation(SimpleTestCase):
    """Parameter validation paths in _lease_search."""

    def test_no_params_returns_400(self):
        view, _ = _make_view()
        response = view._lease_search(_make_request({}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("required", response.data["detail"])

    def test_no_params_v6_includes_duid_in_message(self):
        view, _ = _make_view()
        response = view._lease_search(_make_request({}), version=6)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("duid", response.data["detail"])

    def test_invalid_subnet_id_returns_400(self):
        view, _ = _make_view()
        response = view._lease_search(_make_request({"subnet_id": "not-a-number"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subnet_id", response.data["detail"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchErrors(SimpleTestCase):
    """Error handling paths in _lease_search."""

    def test_connection_error_returns_502(self):
        view, server = _make_view()
        server.get_client.side_effect = rq.ConnectionError("refused")
        response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_kea_exception_returns_502(self):
        view, server = _make_view()
        server.get_client.side_effect = KeaException(_KEA_ERR_RESP)
        response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_value_error_returns_500(self):
        view, server = _make_view()
        server.get_client.side_effect = ValueError("missing DHCPv4 URL")
        response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("configuration error", response.data["detail"].lower())

    def test_generic_exception_returns_500(self):
        view, server = _make_view()
        server.get_client.side_effect = RuntimeError("unexpected internal error")
        response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("internal error", response.data["detail"].lower())


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_leases — all dispatch branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesIpAddress(SimpleTestCase):
    """ip_address branch in _fetch_leases (lines 112-122)."""

    def test_result3_returns_empty(self):
        view, _ = _view_with_command([{"result": 3, "text": "not found"}])
        response = view._lease_search(_make_request({"ip_address": "10.0.0.99"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_null_arguments_returns_empty(self):
        view, _ = _view_with_command([{"result": 0, "arguments": None}])
        response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_lease_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "subnet-id": 1}
        view, _ = _view_with_command([{"result": 0, "arguments": lease}])
        response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesHwAddress(SimpleTestCase):
    """hw_address branch in _fetch_leases (lines 124-133)."""

    def test_result3_returns_empty(self):
        view, _ = _view_with_command([{"result": 3}])
        response = view._lease_search(_make_request({"hw_address": "aa:bb:cc:dd:ee:ff"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_leases_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1}
        view, _ = _view_with_command([{"result": 0, "arguments": {"leases": [lease]}}])
        response = view._lease_search(_make_request({"hw_address": "aa:bb:cc:dd:ee:ff"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesDuid(SimpleTestCase):
    """duid branch in _fetch_leases (lines 135-144)."""

    def test_result3_returns_empty(self):
        view, _ = _view_with_command([{"result": 3}])
        response = view._lease_search(_make_request({"duid": "00:01:02:03"}), version=6)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_leases_returned_in_results(self):
        lease = {"ip-address": "2001:db8::1", "duid": "00:01:02:03", "subnet-id": 10}
        view, _ = _view_with_command([{"result": 0, "arguments": {"leases": [lease]}}])
        response = view._lease_search(_make_request({"duid": "00:01:02:03"}), version=6)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_duid_on_v4_falls_through_to_empty(self):
        """duid provided on v4 skips the duid branch; no other param matches → empty (line 168)."""
        view, server = _make_view()
        mock_client = MagicMock()
        server.get_client.return_value = mock_client
        response = view._lease_search(_make_request({"duid": "00:01:02:03"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)
        mock_client.command.assert_not_called()


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesHostname(SimpleTestCase):
    """hostname branch in _fetch_leases (lines 146-155)."""

    def test_result3_returns_empty(self):
        view, _ = _view_with_command([{"result": 3}])
        response = view._lease_search(_make_request({"hostname": "host1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_leases_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "hostname": "host1", "subnet-id": 1}
        view, _ = _view_with_command([{"result": 0, "arguments": {"leases": [lease]}}])
        response = view._lease_search(_make_request({"hostname": "host1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesSubnetId(SimpleTestCase):
    """subnet_id branch in _fetch_leases (lines 157-166)."""

    def test_result3_returns_empty(self):
        view, _ = _view_with_command([{"result": 3}])
        response = view._lease_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_leases_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "subnet-id": 1}
        view, _ = _view_with_command([{"result": 0, "arguments": {"leases": [lease]}}])
        response = view._lease_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# reservations4 / reservations6 action dispatch
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationActionDispatch(SimpleTestCase):
    """reservations4() and reservations6() dispatch with correct version."""

    def test_reservations4_dispatches_with_version_4(self):
        view, server = _make_view()
        mock_client = MagicMock()
        mock_client.reservation_get.return_value = {"ip-address": "10.0.0.50", "subnet-id": 1}
        server.get_client.return_value = mock_client
        response = view.reservations4(_make_request({"ip_address": "10.0.0.50", "subnet_id": "1"}), pk=1)
        server.get_client.assert_called_once_with(version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_reservations6_dispatches_with_version_6(self):
        view, server = _make_view()
        mock_client = MagicMock()
        mock_client.reservation_get.return_value = {"ip-addresses": ["2001:db8::50"], "subnet-id": 10}
        server.get_client.return_value = mock_client
        response = view.reservations6(_make_request({"ip_address": "2001:db8::50", "subnet_id": "10"}), pk=1)
        server.get_client.assert_called_once_with(version=6)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
# _reservation_search — parameter validation and error handling
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSearchValidation(SimpleTestCase):
    """Parameter validation paths in _reservation_search."""

    def test_no_params_returns_400(self):
        view, _ = _make_view()
        response = view._reservation_search(_make_request({}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("required", response.data["detail"])

    def test_no_params_v6_includes_duid_in_message(self):
        view, _ = _make_view()
        response = view._reservation_search(_make_request({}), version=6)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("duid", response.data["detail"])

    def test_invalid_subnet_id_returns_400(self):
        view, _ = _make_view()
        response = view._reservation_search(_make_request({"subnet_id": "not-a-number"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subnet_id", response.data["detail"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSearchErrors(SimpleTestCase):
    """Error handling paths in _reservation_search."""

    def test_connection_error_returns_502(self):
        view, server = _make_view()
        server.get_client.side_effect = rq.ConnectionError("refused")
        response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_kea_exception_returns_502(self):
        view, server = _make_view()
        server.get_client.side_effect = KeaException(_KEA_ERR_RESP)
        response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_value_error_returns_500(self):
        view, server = _make_view()
        server.get_client.side_effect = ValueError("missing DHCPv4 URL")
        response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("configuration error", response.data["detail"].lower())

    def test_generic_exception_returns_500(self):
        view, server = _make_view()
        server.get_client.side_effect = RuntimeError("unexpected")
        response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("internal error", response.data["detail"].lower())


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_reservations — all dispatch branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsIpAndSubnet(SimpleTestCase):
    """ip_address + subnet_id branch in _fetch_reservations (lines 253-255)."""

    def test_found_returns_one_reservation(self):
        view, server = _make_view()
        mock_client = MagicMock()
        mock_client.reservation_get.return_value = {"ip-address": "10.0.0.50", "subnet-id": 1}
        server.get_client.return_value = mock_client
        response = view._reservation_search(_make_request({"ip_address": "10.0.0.50", "subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_not_found_returns_empty(self):
        view, server = _make_view()
        mock_client = MagicMock()
        mock_client.reservation_get.return_value = None
        server.get_client.return_value = mock_client
        response = view._reservation_search(_make_request({"ip_address": "10.0.0.99", "subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsHwAndSubnet(SimpleTestCase):
    """hw_address + subnet_id branch in _fetch_reservations (lines 257-259)."""

    def test_found_returns_one_reservation(self):
        view, server = _make_view()
        mock_client = MagicMock()
        mock_client.reservation_get.return_value = {"ip-address": "10.0.0.50", "hw-address": "aa:bb:cc:dd:ee:ff"}
        server.get_client.return_value = mock_client
        response = view._reservation_search(
            _make_request({"hw_address": "aa:bb:cc:dd:ee:ff", "subnet_id": "1"}), version=4
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsDuidAndSubnet(SimpleTestCase):
    """duid + subnet_id + version=6 branch in _fetch_reservations (lines 261-263)."""

    def test_found_returns_one_reservation(self):
        view, server = _make_view()
        mock_client = MagicMock()
        mock_client.reservation_get.return_value = {"ip-addresses": ["2001:db8::50"], "duid": "00:01:02:03"}
        server.get_client.return_value = mock_client
        response = view._reservation_search(_make_request({"duid": "00:01:02:03", "subnet_id": "10"}), version=6)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsSubnetOnly(SimpleTestCase):
    """subnet_id-only pagination branch in _fetch_reservations (lines 265-277)."""

    def test_single_page_returns_all_hosts(self):
        view, server = _make_view()
        mock_client = MagicMock()
        host = {"ip-address": "10.0.0.50", "subnet-id": 1}
        mock_client.reservation_get_page.return_value = ([host], 0, 0)
        server.get_client.return_value = mock_client
        response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_multi_page_collects_all_hosts(self):
        view, server = _make_view()
        mock_client = MagicMock()
        host1 = {"ip-address": "10.0.0.1", "subnet-id": 1}
        host2 = {"ip-address": "10.0.0.2", "subnet-id": 1}
        mock_client.reservation_get_page.side_effect = [
            ([host1], 1, 1),  # first page: next_from=1 → continue
            ([host2], 0, 0),  # second page: exhausted
        ]
        server.get_client.return_value = mock_client
        response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_filters_by_subnet_id(self):
        view, server = _make_view()
        mock_client = MagicMock()
        host_in = {"ip-address": "10.0.0.1", "subnet-id": 1}
        host_out = {"ip-address": "10.0.0.2", "subnet-id": 2}
        mock_client.reservation_get_page.return_value = ([host_in, host_out], 0, 0)
        server.get_client.return_value = mock_client
        response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsNoMatch(SimpleTestCase):
    """Fallthrough return [] when no branch matches (line 279)."""

    def test_duid_without_subnet_on_v4_returns_empty(self):
        """duid only on v4 (no subnet_id) — none of the 4 branches fires → empty list."""
        view, server = _make_view()
        mock_client = MagicMock()
        server.get_client.return_value = mock_client
        response = view._reservation_search(_make_request({"duid": "00:01:02:03"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)
        mock_client.reservation_get.assert_not_called()
