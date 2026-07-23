# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Direct unit tests for api/views.py — no database required.

Covers all paths in _lease_search, _fetch_leases, _reservation_search, and
_fetch_reservations.  The TestCase-based api test files cover the same happy
paths via a real DB + HTTP stack; these SimpleTestCase tests provide fast,
DB-free coverage for every branch.

The view methods are called directly (DRF's get_object/permission hooks are the
only mocks — they are framework plumbing, not Kea). ``get_object`` returns a
**real, unsaved** ``Server`` (instantiating a model touches no DB), so
``server.get_client()`` builds a **real** ``KeaClient`` and the HTTP boundary is
stubbed via ``kea_stub.stub_kea`` — the actual ``command`` payloads (command name
+ ``service``) are exercised, and error paths run through the real client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import requests as rq
from django.test import SimpleTestCase, override_settings
from rest_framework import status

from netbox_kea.api.views import ServerViewSet
from netbox_kea.models import Server

from .kea_stub import _res_page, queued, stub_kea

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

# A Kea error response (result 1) → the real KeaClient turns this into a KeaException.
_KEA_ERR_RESP = {"result": 1, "text": "command failed"}


def _make_view(**server_kwargs):
    """Return ``(view, server)`` where *server* is a real (unsaved) Server.

    Only DRF plumbing is stubbed: ``get_object`` returns the real Server and the
    permission hooks are no-ops so the action can be invoked directly. The Server
    is a genuine model instance (no ``.save()`` → no DB), so ``get_client()`` builds
    a real ``KeaClient``.
    """
    view = ServerViewSet()
    view.kwargs = {}
    view.format_kwarg = None
    defaults = {
        "name": "test-server",
        "ca_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(server_kwargs)
    server = Server(**defaults)
    view.get_object = MagicMock(return_value=server)  # mock-ok: DRF get_object → real unsaved Server
    view.check_permissions = MagicMock()  # mock-ok: stub DRF permission hook for direct view call
    view.check_object_permissions = MagicMock()  # mock-ok: stub DRF object-permission hook
    return view, server


def _make_request(query_params: dict):
    """Return a minimal mock request with the given query_params dict."""
    req = MagicMock()  # mock-ok: minimal DRF request (query_params only)
    req.query_params = query_params
    return req


# ─────────────────────────────────────────────────────────────────────────────
# leases4 / leases6 action dispatch
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseActionDispatch(SimpleTestCase):
    """leases4() and leases6() dispatch to _lease_search with the right version."""

    def test_leases4_dispatches_with_version_4(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get": [{"result": 0, "arguments": None}]}) as kea:
            response = view.leases4(_make_request({"ip_address": "10.0.0.1"}), pk=1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(kea.bodies("lease4-get")[0]["service"], ["dhcp4"])

    def test_leases6_dispatches_with_version_6(self):
        view, _ = _make_view()
        with stub_kea({"lease6-get": [{"result": 0, "arguments": None}]}) as kea:
            response = view.leases6(_make_request({"ip_address": "2001:db8::1"}), pk=1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(kea.bodies("lease6-get")[0]["service"], ["dhcp6"])


# ─────────────────────────────────────────────────────────────────────────────
# _lease_search — parameter validation and error handling
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchValidation(SimpleTestCase):
    """Parameter validation paths in _lease_search (no Kea traffic)."""

    def test_no_params_returns_400(self):
        view, _ = _make_view()
        with stub_kea({}) as kea:
            response = view._lease_search(_make_request({}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("required", response.data["detail"])
        self.assertEqual(kea.commands(), [])

    def test_no_params_v6_includes_duid_in_message(self):
        view, _ = _make_view()
        with stub_kea({}):
            response = view._lease_search(_make_request({}), version=6)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("duid", response.data["detail"])

    def test_invalid_subnet_id_returns_400(self):
        view, _ = _make_view()
        with stub_kea({}):
            response = view._lease_search(_make_request({"subnet_id": "not-a-number"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subnet_id", response.data["detail"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchErrors(SimpleTestCase):
    """Error handling paths in _lease_search (real client, boundary-injected errors)."""

    def test_connection_error_returns_502(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get": rq.ConnectionError("refused")}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_kea_exception_returns_502(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get": _KEA_ERR_RESP}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_value_error_returns_500(self):
        # cert-without-key makes the real get_client() raise ValueError (configuration error).
        view, _ = _make_view(client_cert_path="/nonexistent-cert.pem")
        with stub_kea({}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("configuration error", response.data["detail"].lower())

    def test_generic_exception_returns_500(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get": RuntimeError("unexpected internal error")}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("internal error", response.data["detail"].lower())


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_leases — all dispatch branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesIpAddress(SimpleTestCase):
    """ip_address branch in _fetch_leases (lease{v}-get)."""

    def test_result3_returns_empty(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get": [{"result": 3, "text": "not found"}]}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.99"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_null_arguments_returns_empty(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get": [{"result": 0, "arguments": None}]}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_lease_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "subnet-id": 1}
        view, _ = _make_view()
        with stub_kea({"lease4-get": [{"result": 0, "arguments": lease}]}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesHwAddress(SimpleTestCase):
    """hw_address branch in _fetch_leases (lease{v}-get-by-hw-address)."""

    def test_result3_returns_empty(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get-by-hw-address": [{"result": 3}]}):
            response = view._lease_search(_make_request({"hw_address": "aa:bb:cc:dd:ee:ff"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_leases_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1}
        view, _ = _make_view()
        with stub_kea({"lease4-get-by-hw-address": [{"result": 0, "arguments": {"leases": [lease]}}]}):
            response = view._lease_search(_make_request({"hw_address": "aa:bb:cc:dd:ee:ff"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesDuid(SimpleTestCase):
    """duid branch in _fetch_leases (lease6-get-by-duid)."""

    def test_result3_returns_empty(self):
        view, _ = _make_view()
        with stub_kea({"lease6-get-by-duid": [{"result": 3}]}):
            response = view._lease_search(_make_request({"duid": "00:01:02:03"}), version=6)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_leases_returned_in_results(self):
        lease = {"ip-address": "2001:db8::1", "duid": "00:01:02:03", "subnet-id": 10}
        view, _ = _make_view()
        with stub_kea({"lease6-get-by-duid": [{"result": 0, "arguments": {"leases": [lease]}}]}):
            response = view._lease_search(_make_request({"duid": "00:01:02:03"}), version=6)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_duid_on_v4_falls_through_to_empty(self):
        """duid provided on v4 skips the duid branch; no other param matches → empty (no Kea call)."""
        view, _ = _make_view()
        with stub_kea({}) as kea:
            response = view._lease_search(_make_request({"duid": "00:01:02:03"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(kea.commands(), [])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesHostname(SimpleTestCase):
    """hostname branch in _fetch_leases (lease{v}-get-by-hostname)."""

    def test_result3_returns_empty(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get-by-hostname": [{"result": 3}]}):
            response = view._lease_search(_make_request({"hostname": "host1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_leases_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "hostname": "host1", "subnet-id": 1}
        view, _ = _make_view()
        with stub_kea({"lease4-get-by-hostname": [{"result": 0, "arguments": {"leases": [lease]}}]}):
            response = view._lease_search(_make_request({"hostname": "host1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesSubnetId(SimpleTestCase):
    """subnet_id branch in _fetch_leases (lease{v}-get-all)."""

    def test_result3_returns_empty(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get-all": [{"result": 3}]}):
            response = view._lease_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_leases_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "subnet-id": 1}
        view, _ = _make_view()
        with stub_kea({"lease4-get-all": [{"result": 0, "arguments": {"leases": [lease]}}]}):
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
        view, _ = _make_view()
        reservation = {"result": 0, "arguments": {"ip-address": "10.0.0.50", "subnet-id": 1}}
        with stub_kea({"reservation-get": reservation}) as kea:
            response = view.reservations4(_make_request({"ip_address": "10.0.0.50", "subnet_id": "1"}), pk=1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(kea.bodies("reservation-get")[0]["service"], ["dhcp4"])

    def test_reservations6_dispatches_with_version_6(self):
        view, _ = _make_view()
        reservation = {"result": 0, "arguments": {"ip-addresses": ["2001:db8::50"], "subnet-id": 10}}
        with stub_kea({"reservation-get": reservation}) as kea:
            response = view.reservations6(_make_request({"ip_address": "2001:db8::50", "subnet_id": "10"}), pk=1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(kea.bodies("reservation-get")[0]["service"], ["dhcp6"])


# ─────────────────────────────────────────────────────────────────────────────
# _reservation_search — parameter validation and error handling
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSearchValidation(SimpleTestCase):
    """Parameter validation paths in _reservation_search (no Kea traffic)."""

    def test_no_params_returns_400(self):
        view, _ = _make_view()
        with stub_kea({}) as kea:
            response = view._reservation_search(_make_request({}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("required", response.data["detail"])
        self.assertEqual(kea.commands(), [])

    def test_no_params_v6_includes_duid_in_message(self):
        view, _ = _make_view()
        with stub_kea({}):
            response = view._reservation_search(_make_request({}), version=6)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("duid", response.data["detail"])

    def test_invalid_subnet_id_returns_400(self):
        view, _ = _make_view()
        with stub_kea({}):
            response = view._reservation_search(_make_request({"subnet_id": "not-a-number"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subnet_id", response.data["detail"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSearchErrors(SimpleTestCase):
    """Error handling paths in _reservation_search (real client, boundary-injected errors)."""

    def test_connection_error_returns_502(self):
        view, _ = _make_view()
        with stub_kea({"reservation-get-page": rq.ConnectionError("refused")}):
            response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_kea_exception_returns_502(self):
        view, _ = _make_view()
        with stub_kea({"reservation-get-page": _KEA_ERR_RESP}):
            response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_value_error_returns_500(self):
        view, _ = _make_view(client_cert_path="/nonexistent-cert.pem")
        with stub_kea({}):
            response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("configuration error", response.data["detail"].lower())

    def test_generic_exception_returns_500(self):
        view, _ = _make_view()
        with stub_kea({"reservation-get-page": RuntimeError("unexpected")}):
            response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("internal error", response.data["detail"].lower())


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_reservations — all dispatch branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsIpAndSubnet(SimpleTestCase):
    """ip_address + subnet_id branch in _fetch_reservations (reservation-get)."""

    def test_found_returns_one_reservation(self):
        view, _ = _make_view()
        reservation = {"result": 0, "arguments": {"ip-address": "10.0.0.50", "subnet-id": 1}}
        with stub_kea({"reservation-get": reservation}):
            response = view._reservation_search(_make_request({"ip_address": "10.0.0.50", "subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_not_found_returns_empty(self):
        view, _ = _make_view()
        with stub_kea({"reservation-get": {"result": 3}}):
            response = view._reservation_search(_make_request({"ip_address": "10.0.0.99", "subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsHwAndSubnet(SimpleTestCase):
    """hw_address + subnet_id branch in _fetch_reservations (reservation-get)."""

    def test_found_returns_one_reservation(self):
        view, _ = _make_view()
        reservation = {"result": 0, "arguments": {"ip-address": "10.0.0.50", "hw-address": "aa:bb:cc:dd:ee:ff"}}
        with stub_kea({"reservation-get": reservation}):
            response = view._reservation_search(
                _make_request({"hw_address": "aa:bb:cc:dd:ee:ff", "subnet_id": "1"}), version=4
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsDuidAndSubnet(SimpleTestCase):
    """duid + subnet_id + version=6 branch in _fetch_reservations (reservation-get)."""

    def test_found_returns_one_reservation(self):
        view, _ = _make_view()
        reservation = {"result": 0, "arguments": {"ip-addresses": ["2001:db8::50"], "duid": "00:01:02:03"}}
        with stub_kea({"reservation-get": reservation}):
            response = view._reservation_search(_make_request({"duid": "00:01:02:03", "subnet_id": "10"}), version=6)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsSubnetOnly(SimpleTestCase):
    """subnet_id-only pagination branch in _fetch_reservations (reservation-get-page)."""

    def test_single_page_returns_all_hosts(self):
        view, _ = _make_view()
        host = {"ip-address": "10.0.0.50", "subnet-id": 1}
        with stub_kea({"reservation-get-page": _res_page([host])}):
            response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_multi_page_collects_all_hosts(self):
        view, _ = _make_view()
        host1 = {"ip-address": "10.0.0.1", "subnet-id": 1}
        host2 = {"ip-address": "10.0.0.2", "subnet-id": 1}
        pages = queued(
            _res_page([host1], next_from=1, next_source=1),  # not exhausted
            _res_page([host2]),  # exhausted
        )
        with stub_kea({"reservation-get-page": pages}):
            response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_filters_by_subnet_id(self):
        view, _ = _make_view()
        host_in = {"ip-address": "10.0.0.1", "subnet-id": 1}
        host_out = {"ip-address": "10.0.0.2", "subnet-id": 2}
        with stub_kea({"reservation-get-page": _res_page([host_in, host_out])}):
            response = view._reservation_search(_make_request({"subnet_id": "1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchReservationsNoMatch(SimpleTestCase):
    """Fallthrough return [] when no branch matches."""

    def test_duid_without_subnet_on_v4_returns_empty(self):
        """duid only on v4 (no subnet_id) — none of the 4 branches fires → empty list, no Kea call."""
        view, _ = _make_view()
        with stub_kea({}) as kea:
            response = view._reservation_search(_make_request({"duid": "00:01:02:03"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(kea.commands(), [])
