# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Direct unit tests for api/views.py: no database required.

Covers all paths in _lease_search. The TestCase-based API tests cover the
normalized Reservation API through its real request surface.

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

from .kea_stub import stub_kea

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30, "lease_query_max_unpaged_leases": 0}}
_GUARDED_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30, "lease_query_max_unpaged_leases": 100}}

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
        with stub_kea({"lease4-get": [{"result": 3, "text": "not found"}]}) as kea:
            response = view.leases4(_make_request({"ip_address": "10.0.0.1"}), pk=1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(kea.bodies("lease4-get")[0]["service"], ["dhcp4"])

    def test_leases6_dispatches_with_version_6(self):
        view, _ = _make_view()
        with stub_kea({"lease6-get": [{"result": 3, "text": "not found"}]}) as kea:
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
# _lease_search client dispatch branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesIpAddress(SimpleTestCase):
    """ip_address branch in _lease_search."""

    def test_result3_returns_empty(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get": [{"result": 3, "text": "not found"}]}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.99"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_null_arguments_returns_internal_error(self):
        view, _ = _make_view()
        with stub_kea({"lease4-get": [{"result": 0, "arguments": None}]}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("internal error", response.data["detail"].lower())

    def test_lease_returned_in_results(self):
        lease = {"ip-address": "10.0.0.1", "subnet-id": 1}
        view, _ = _make_view()
        with stub_kea({"lease4-get": [{"result": 0, "arguments": lease}]}):
            response = view._lease_search(_make_request({"ip_address": "10.0.0.1"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesHwAddress(SimpleTestCase):
    """hw_address branch in _lease_search."""

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
    """duid branch in _lease_search."""

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

    def test_duid_on_v4_returns_bad_request(self):
        """DHCPv4 rejects the DHCPv6-only selector without a Kea request."""
        view, _ = _make_view()
        with stub_kea({}) as kea:
            response = view._lease_search(_make_request({"duid": "00:01:02:03"}), version=4)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("DHCPv6", response.data["detail"])
        self.assertEqual(kea.commands(), [])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesHostname(SimpleTestCase):
    """hostname branch in _lease_search."""

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
    """subnet_id branch in _lease_search."""

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

    @override_settings(PLUGINS_CONFIG=_GUARDED_PLUGINS_CONFIG)
    def test_declined_state_uses_guarded_subnet_state_query(self):
        lease = {"ip-address": "198.18.0.1", "subnet-id": 1, "state": 1}
        stats = {
            "result": 0,
            "arguments": {
                "result-set": {
                    "columns": ["subnet-id", "assigned-addresses", "declined-addresses"],
                    "rows": [[1, 501, 1]],
                }
            },
        }
        view, _ = _make_view()
        with stub_kea(
            {
                "stat-lease4-get": stats,
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": [lease]}},
            }
        ) as kea:
            response = view._lease_search(_make_request({"subnet_id": "1", "state": "1"}), version=4)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            kea.bodies("lease4-get-by-state")[0]["arguments"],
            {"subnet-id": 1, "state": 1},
        )

    @override_settings(PLUGINS_CONFIG=_GUARDED_PLUGINS_CONFIG)
    def test_large_unqualified_subnet_query_is_rejected(self):
        stats = {
            "result": 0,
            "arguments": {
                "result-set": {
                    "columns": ["subnet-id", "assigned-addresses", "declined-addresses"],
                    "rows": [[1, 101, 0]],
                }
            },
        }
        view, _ = _make_view()
        with stub_kea({"stat-lease4-get": stats}) as kea:
            response = view._lease_search(_make_request({"subnet_id": "1"}), version=4)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Active or Declined", response.data["detail"])
        self.assertEqual(kea.commands(), ["stat-lease4-get"])

    def test_state_requires_subnet_id(self):
        view, _ = _make_view()
        with stub_kea({}) as kea:
            response = view._lease_search(_make_request({"hostname": "host1", "state": "1"}), version=4)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subnet_id", response.data["detail"])
        self.assertEqual(kea.commands(), [])

    def test_state_rejects_an_earlier_selected_filter(self):
        view, _ = _make_view()
        with stub_kea({}) as kea:
            response = view._lease_search(
                _make_request({"ip_address": "198.18.0.1", "subnet_id": "1", "state": "1"}),
                version=4,
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("selected filter", response.data["detail"])
        self.assertEqual(kea.commands(), [])

    def test_expired_state_is_not_safe_for_subnet_query(self):
        view, _ = _make_view()
        with stub_kea({}) as kea:
            response = view._lease_search(_make_request({"subnet_id": "1", "state": "2"}), version=4)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Active or Declined", response.data["detail"])
        self.assertEqual(kea.commands(), [])
