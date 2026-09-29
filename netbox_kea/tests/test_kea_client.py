# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.kea — KeaClient, KeaException, check_response.

These tests mock all HTTP calls and require no running services.
"""

import dataclasses
import ipaddress
from dataclasses import replace
from typing import get_args, get_origin, get_type_hints
from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests

from netbox_kea import constants
from netbox_kea.kea import (
    KeaClient,
    KeaException,
    KeaResponse,
    LeaseCollection,
    LeasePage,
    LeaseQueryGuardError,
    LeaseQueryNotMeasurable,
    LeaseQueryPreflightUnavailable,
    LeaseQueryTooBroad,
    SubnetEdit,
    SubnetFields,
    check_response,
    lease_query_guard_message,
)
from netbox_kea.tests.kea_stub import _subnet_stats, queued, stub_kea


def _mock_http_response(json_data, status_code=200):
    """Build a mock requests.Response returning *json_data*."""
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    else:
        mock_resp.raise_for_status.return_value = None
    return mock_resp


class TestKeaResponseContract(TestCase):
    """Tests for the common Kea response type."""

    def test_arguments_accept_command_lists(self):
        """The response contract includes list-valued command responses."""
        arguments_type = get_type_hints(KeaResponse)["arguments"]

        self.assertIn(list, {get_origin(member) for member in get_args(arguments_type)})


class TestKeaClientInit(TestCase):
    """Tests for KeaClient.__init__ validation."""

    def test_basic_init_sets_url(self):
        client = KeaClient(url="http://kea:8000")
        self.assertEqual(client.url, "http://kea:8000")

    def test_default_timeout(self):
        client = KeaClient(url="http://kea:8000")
        self.assertEqual(client.timeout, 30)

    def test_custom_timeout(self):
        client = KeaClient(url="http://kea:8000", timeout=10)
        self.assertEqual(client.timeout, 10)

    def test_cert_without_key_raises(self):
        with self.assertRaises(ValueError):
            KeaClient(url="http://kea:8000", client_cert="/cert.pem")

    def test_key_without_cert_raises(self):
        with self.assertRaises(ValueError):
            KeaClient(url="http://kea:8000", client_key="/key.pem")

    def test_cert_and_key_together_accepted(self):
        client = KeaClient(url="http://kea:8000", client_cert="/cert.pem", client_key="/key.pem")
        self.assertEqual(client._session.cert, ("/cert.pem", "/key.pem"))

    def test_basic_auth_configured(self):
        client = KeaClient(url="http://kea:8000", username="admin", password="secret")
        self.assertIsNotNone(client._session.auth)

    def test_no_auth_when_username_only(self):
        # Partial auth — no password means no auth header set
        client = KeaClient(url="http://kea:8000", username="admin")
        self.assertIsNone(client._session.auth)

    def test_ssl_verify_false(self):
        client = KeaClient(url="http://kea:8000", verify=False)
        self.assertFalse(client._session.verify)

    def test_ssl_verify_path(self):
        client = KeaClient(url="http://kea:8000", verify="/etc/ssl/ca.pem")
        self.assertEqual(client._session.verify, "/etc/ssl/ca.pem")

    def test_no_verify_arg_leaves_session_default(self):
        client = KeaClient(url="http://kea:8000")
        # requests.Session defaults verify to True; we do not override it when verify=None
        self.assertTrue(client._session.verify)

    def test_clone_copies_url_and_timeout(self):
        """clone() produces a new KeaClient with the same url and timeout."""
        client = KeaClient(url="http://kea:8000", timeout=15)
        cloned = client.clone()
        self.assertEqual(cloned.url, "http://kea:8000")
        self.assertEqual(cloned.timeout, 15)

    def test_clone_has_independent_session(self):
        """clone() creates a new requests.Session, not a reference to the original."""
        client = KeaClient(url="http://kea:8000")
        cloned = client.clone()
        self.assertIsNot(cloned._session, client._session)

    def test_clone_copies_session_auth(self):
        """clone() copies auth credentials from the original session."""
        client = KeaClient(url="http://kea:8000", username="admin", password="secret")
        cloned = client.clone()
        self.assertEqual(cloned._session.auth, client._session.auth)

    def test_clone_copies_session_verify(self):
        """clone() copies the SSL verify setting."""
        client = KeaClient(url="http://kea:8000", verify="/etc/ssl/ca.pem")
        cloned = client.clone()
        self.assertEqual(cloned._session.verify, "/etc/ssl/ca.pem")

    def test_clone_copies_session_cert(self):
        """clone() copies the client cert tuple."""
        client = KeaClient(url="http://kea:8000", client_cert="/cert.pem", client_key="/key.pem")
        cloned = client.clone()
        self.assertEqual(cloned._session.cert, client._session.cert)

    def test_clone_preserves_send_service(self):
        """clone() carries send_service so a cloned worker-thread client stays direct."""
        self.assertFalse(KeaClient(url="http://kea:8000", send_service=False).clone().send_service)
        self.assertTrue(KeaClient(url="http://kea:8000").clone().send_service)

    """Tests for KeaClient.command()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _patched_post(self, json_data):
        """Patch session.post to return *json_data*."""
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_command_returns_response_list(self):
        resp = [{"result": 0, "arguments": {"leases": []}, "text": "ok"}]
        with self._patched_post(resp):
            result = self.client.command("lease4-get-all", service=["dhcp4"])
        self.assertEqual(result, resp)

    def test_command_sends_correct_body(self):
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.command("status-get", service=["dhcp4"], arguments={"extra": 1})

        call_kwargs = mock_post.call_args
        sent_json = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        self.assertEqual(sent_json["command"], "status-get")
        self.assertEqual(sent_json["service"], ["dhcp4"])
        self.assertEqual(sent_json["arguments"], {"extra": 1})

    def test_command_omits_service_when_none(self):
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.command("list-commands")
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertNotIn("service", sent_json)

    def test_command_omits_service_when_send_service_false(self):
        """A direct-daemon client (send_service=False) drops a supplied service from the body."""
        client = KeaClient(url="http://kea-daemon:8000", send_service=False)
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            client.command("lease4-get", service=["dhcp4"])
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertNotIn("service", sent_json)

    def test_command_omits_arguments_when_none(self):
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.command("list-commands")
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertNotIn("arguments", sent_json)

    def test_command_raises_kea_exception_on_error_code(self):
        resp = [{"result": 1, "text": "unknown command"}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException):
                self.client.command("bad-command")

    def test_command_raises_kea_exception_with_correct_response(self):
        resp = [{"result": 2, "text": "not found"}]
        with self._patched_post(resp):
            try:
                self.client.command("something")
                self.fail("Expected KeaException")
            except KeaException as exc:
                self.assertEqual(exc.response["result"], 2)

    def test_command_check_none_skips_validation(self):
        resp = [{"result": 1, "text": "error but accepted"}]
        with self._patched_post(resp):
            result = self.client.command("whatever", check=None)
        self.assertEqual(result, resp)

    def test_command_custom_ok_codes(self):
        resp = [{"result": 3, "text": "empty"}]
        with self._patched_post(resp):
            result = self.client.command("lease4-get", service=["dhcp4"], check=(0, 3))
        self.assertEqual(result, resp)

    def test_command_http_error_raises(self):
        mock_resp = _mock_http_response({}, status_code=500)
        with patch.object(self.client._session, "post", return_value=mock_resp):
            with self.assertRaises(requests.HTTPError):
                self.client.command("something")

    def test_command_raises_value_error_on_non_list_json(self):
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response({"result": 0, "text": "ok"}),
        ):
            with self.assertRaises(ValueError):
                self.client.command("something")

    def test_command_uses_timeout(self):
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.command("list-commands")
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs.get("timeout"), 30)

    def test_command_multiple_services(self):
        resp = [{"result": 0, "text": "ok"}, {"result": 0, "text": "ok"}]
        with self._patched_post(resp):
            result = self.client.command("status-get", service=["dhcp4", "dhcp6"])
        self.assertEqual(len(result), 2)

    def test_command_raises_on_second_failed_response(self):
        resp = [{"result": 0, "text": "ok"}, {"result": 1, "text": "failed"}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException) as ctx:
                self.client.command("status-get", service=["dhcp4", "dhcp6"])
        self.assertEqual(ctx.exception.index, 1)


class TestKeaException(TestCase):
    """Tests for KeaException initialisation and message formatting."""

    def test_unsupported_command_flag_matches_result(self):
        for result in (0, 1, 2, 3):
            with self.subTest(result=result):
                exc = KeaException({"result": result, "text": "command outcome"})
                self.assertEqual(exc.unsupported_command, result == 2)

    def test_default_message_includes_result_code(self):
        resp = {"result": 1, "text": "command rejected"}
        exc = KeaException(resp, index=0)
        self.assertIn("command rejected", str(exc))

    def test_custom_message_used(self):
        resp = {"result": 2, "text": "not found"}
        exc = KeaException(resp, msg="Custom failure", index=0)
        self.assertIn("Custom failure", str(exc))
        self.assertIn("not found", str(exc))

    def test_response_stored(self):
        resp = {"result": 3, "text": "empty"}
        exc = KeaException(resp, index=0)
        self.assertIs(exc.response, resp)

    def test_index_stored(self):
        resp = {"result": 1, "text": "err"}
        exc = KeaException(resp, index=2)
        self.assertEqual(exc.index, 2)

    def test_is_exception_subclass(self):
        resp = {"result": 1, "text": "err"}
        exc = KeaException(resp)
        self.assertIsInstance(exc, Exception)


class TestCheckResponse(TestCase):
    """Tests for the check_response() helper."""

    def test_result_zero_passes(self):
        resp = [{"result": 0, "text": "ok"}]
        check_response(resp, (0,))  # must not raise

    def test_result_nonzero_raises(self):
        resp = [{"result": 1, "text": "error"}]
        with self.assertRaises(KeaException):
            check_response(resp, (0,))

    def test_multiple_responses_second_fails(self):
        resp = [{"result": 0, "text": "ok"}, {"result": 1, "text": "err"}]
        with self.assertRaises(KeaException) as ctx:
            check_response(resp, (0,))
        self.assertEqual(ctx.exception.index, 1)

    def test_custom_ok_codes_pass(self):
        resp = [{"result": 3, "text": "empty"}]
        check_response(resp, (0, 3))  # must not raise

    def test_custom_ok_codes_raises_for_unlisted(self):
        resp = [{"result": 2, "text": "conflict"}]
        with self.assertRaises(KeaException):
            check_response(resp, (0, 3))

    def test_empty_response_list_passes(self):
        check_response([], (0,))  # no items to check — passes trivially

    def test_non_dict_entry_raises_runtime_error(self):
        """A non-dict entry must raise RuntimeError, not TypeError, so callers' handlers catch it."""
        with self.assertRaises(RuntimeError):
            check_response(["not-a-dict"], (0,))

    def test_entry_without_result_raises_runtime_error(self):
        """An entry missing 'result' must raise RuntimeError, not KeyError."""
        with self.assertRaises(RuntimeError):
            check_response([{"text": "no result key"}], (0,))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Reservation Management — helper method tests
# These tests will FAIL until the methods are added to KeaClient.
# ─────────────────────────────────────────────────────────────────────────────


class TestGetAvailableCommands(TestCase):
    """Tests for KeaClient.get_available_commands(service) -> set[str]."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _patched_post(self, json_data):
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_returns_set_of_command_names(self):
        resp = [{"result": 0, "arguments": ["reservation-add", "reservation-get-page", "reservation-del"]}]
        with self._patched_post(resp):
            result = self.client.get_available_commands("dhcp4")
        self.assertIsInstance(result, set)
        self.assertIn("reservation-add", result)
        self.assertIn("reservation-get-page", result)
        self.assertIn("reservation-del", result)

    def test_handles_empty_arguments(self):
        resp = [{"result": 0, "arguments": []}]
        with self._patched_post(resp):
            result = self.client.get_available_commands("dhcp4")
        self.assertEqual(result, set())

    def test_sends_list_commands_to_correct_service(self):
        resp = [{"result": 0, "arguments": ["reservation-add"]}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.get_available_commands("dhcp4")
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["command"], "list-commands")
        self.assertEqual(sent_json["service"], ["dhcp4"])

    def test_works_for_dhcp6_service(self):
        resp = [{"result": 0, "arguments": ["reservation-add", "reservation-get-page"]}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            result = self.client.get_available_commands("dhcp6")
        self.assertIsInstance(result, set)
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["service"], ["dhcp6"])


# ---------------------------------------------------------------------------
# Shared replies
# ---------------------------------------------------------------------------

_OK = [{"result": 0, "text": "ok"}]


def _side_effects(*responses):
    import copy

    return [_mock_http_response(copy.deepcopy(r)) for r in responses]


# ---------------------------------------------------------------------------
# TestSubnetAdd
# ---------------------------------------------------------------------------

_NO_FIELDS = SubnetFields(pools=(), gateway="", dns_servers=(), ntp_servers=(), ddns_qualifying_suffix="")


class TestSubnetAdd(TestCase):
    """KeaClient.subnet_add sends one subnet{v}-add and does not persist. config_write covers the payload."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_sends_one_command_without_a_persist_step(self):
        with stub_kea({"subnet4-add": {"result": 0, "text": "IPv4 subnet added"}}) as kea:
            self.client.subnet_add(4, 10, "10.99.0.0/24", _NO_FIELDS)
        self.assertEqual(kea.commands(), ["subnet4-add"])
        self.assertEqual(kea.bodies("subnet4-add")[0]["arguments"], {"subnet4": [{"subnet": "10.99.0.0/24", "id": 10}]})

    def test_a_dhcpv6_subnet_carries_no_gateway(self):
        fields = SubnetFields(
            pools=(), gateway="2001:db8::1", dns_servers=(), ntp_servers=(), ddns_qualifying_suffix=""
        )
        with stub_kea({"subnet6-add": {"result": 0, "text": "IPv6 subnet added"}}) as kea:
            self.client.subnet_add(6, 20, "2001:db8:99::/48", fields)
        self.assertEqual(
            kea.bodies("subnet6-add")[0]["arguments"], {"subnet6": [{"subnet": "2001:db8:99::/48", "id": 20}]}
        )

    def test_a_failure_result_raises_kea_exception(self):
        rejection = {"result": 1, "text": "ID of the new IPv4 subnet '10' is already in use"}
        with stub_kea({"subnet4-add": rejection}) as kea, self.assertRaises(KeaException):
            self.client.subnet_add(4, 10, "10.99.0.0/24", _NO_FIELDS)
        self.assertEqual(kea.commands(), ["subnet4-add"])

    def test_a_malformed_reply_raises_runtime_error(self):
        ok = {"result": 0, "text": "IPv4 subnet added"}
        with stub_kea({"subnet4-add": [ok, ok]}), self.assertRaises(RuntimeError):
            self.client.subnet_add(4, 10, "10.99.0.0/24", _NO_FIELDS)


# ─────────────────────────────────────────────────────────────────────────────
# Feature 3.2: lease_wipe — KeaClient.lease_wipe()
# ─────────────────────────────────────────────────────────────────────────────

_LEASE_WIPE_RESP = [{"result": 0, "text": "204 IPv4 lease(s) wiped."}]


class TestLeaseWipe(TestCase):
    """Tests for KeaClient.lease_wipe()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _cmds(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"])["command"] for c in mock_post.call_args_list]

    def test_lease_wipe_v4_sends_correct_command(self):
        """lease4-wipe is sent for version=4."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE_WIPE_RESP),
        ) as mock_post:
            self.client.lease_wipe(version=4, subnet_id=5)
        self.assertIn("lease4-wipe", self._cmds(mock_post))

    def test_lease_wipe_v6_sends_correct_command(self):
        """lease6-wipe is sent for version=6."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE_WIPE_RESP),
        ) as mock_post:
            self.client.lease_wipe(version=6, subnet_id=7)
        self.assertIn("lease6-wipe", self._cmds(mock_post))
        self.assertNotIn("lease4-wipe", self._cmds(mock_post))

    def test_lease_wipe_sends_correct_subnet_id(self):
        """lease4-wipe payload contains the correct subnet-id."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE_WIPE_RESP),
        ) as mock_post:
            self.client.lease_wipe(version=4, subnet_id=42)
        wipe_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "lease4-wipe"
        )
        self.assertEqual(wipe_call["arguments"]["subnet-id"], 42)

    def test_lease_wipe_does_not_call_config_write(self):
        """lease_wipe must NOT call config-write (leases don't need persistence)."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE_WIPE_RESP),
        ) as mock_post:
            self.client.lease_wipe(version=4, subnet_id=1)
        self.assertNotIn("config-write", self._cmds(mock_post))

    def test_lease_wipe_raises_on_kea_error(self):
        """KeaException is raised when Kea returns result != 0 (e.g. hook not loaded)."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects([{"result": 1, "text": "hook not loaded"}]),
        ):
            with self.assertRaises(KeaException):
                self.client.lease_wipe(version=4, subnet_id=99)

    def test_lease_wipe_returns_none_on_success(self):
        """lease_wipe returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE_WIPE_RESP),
        ):
            result = self.client.lease_wipe(version=4, subnet_id=1)
        self.assertIsNone(result)


_DHCP_DISABLE_RESP = [{"result": 0, "text": "DHCPv4 server disabled."}]
_DHCP_ENABLE_RESP = [{"result": 0, "text": "DHCPv4 server enabled."}]


class TestDHCPDisable(TestCase):
    """Tests for KeaClient.dhcp_disable()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payload(self, mock_post):
        return mock_post.call_args_list[0].kwargs.get("json") or mock_post.call_args_list[0][1]["json"]

    def test_dhcp_disable_sends_correct_command(self):
        """dhcp-disable command is sent to the correct service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ) as mock_post:
            self.client.dhcp_disable("dhcp4")
        payload = self._payload(mock_post)
        self.assertEqual(payload["command"], "dhcp-disable")
        self.assertEqual(payload["service"], ["dhcp4"])

    def test_dhcp_disable_without_max_period_omits_arguments(self):
        """When max_period is not given, the arguments field must be absent."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ) as mock_post:
            self.client.dhcp_disable("dhcp4")
        payload = self._payload(mock_post)
        self.assertNotIn("arguments", payload)

    def test_dhcp_disable_with_max_period_includes_arguments(self):
        """When max_period is given, arguments contains max-period."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ) as mock_post:
            self.client.dhcp_disable("dhcp4", max_period=300)
        payload = self._payload(mock_post)
        self.assertIn("arguments", payload)
        self.assertEqual(payload["arguments"]["max-period"], 300)

    def test_dhcp_disable_raises_on_kea_error(self):
        """KeaException is raised when Kea returns result != 0."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects([{"result": 1, "text": "server busy"}]),
        ):
            with self.assertRaises(KeaException):
                self.client.dhcp_disable("dhcp4")

    def test_dhcp_disable_works_for_dhcp6(self):
        """dhcp-disable can target the dhcp6 service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ) as mock_post:
            self.client.dhcp_disable("dhcp6")
        payload = self._payload(mock_post)
        self.assertEqual(payload["service"], ["dhcp6"])

    def test_dhcp_disable_returns_none_on_success(self):
        """dhcp_disable returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ):
            result = self.client.dhcp_disable("dhcp4")
        self.assertIsNone(result)


class TestDHCPEnable(TestCase):
    """Tests for KeaClient.dhcp_enable()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payload(self, mock_post):
        return mock_post.call_args_list[0].kwargs.get("json") or mock_post.call_args_list[0][1]["json"]

    def test_dhcp_enable_sends_correct_command(self):
        """dhcp-enable command is sent to the correct service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_ENABLE_RESP),
        ) as mock_post:
            self.client.dhcp_enable("dhcp4")
        payload = self._payload(mock_post)
        self.assertEqual(payload["command"], "dhcp-enable")
        self.assertEqual(payload["service"], ["dhcp4"])

    def test_dhcp_enable_has_no_arguments(self):
        """dhcp-enable payload must not contain arguments."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_ENABLE_RESP),
        ) as mock_post:
            self.client.dhcp_enable("dhcp4")
        payload = self._payload(mock_post)
        self.assertNotIn("arguments", payload)

    def test_dhcp_enable_raises_on_kea_error(self):
        """KeaException is raised when Kea returns result != 0."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects([{"result": 1, "text": "already enabled"}]),
        ):
            with self.assertRaises(KeaException):
                self.client.dhcp_enable("dhcp4")

    def test_dhcp_enable_works_for_dhcp6(self):
        """dhcp-enable can target the dhcp6 service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_ENABLE_RESP),
        ) as mock_post:
            self.client.dhcp_enable("dhcp6")
        payload = self._payload(mock_post)
        self.assertEqual(payload["service"], ["dhcp6"])

    def test_dhcp_enable_returns_none_on_success(self):
        """dhcp_enable returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_ENABLE_RESP),
        ):
            result = self.client.dhcp_enable("dhcp4")
        self.assertIsNone(result)


# TestLeaseUpdate
# ---------------------------------------------------------------------------

_LEASE4_GET_RESP = [
    {
        "result": 0,
        "arguments": {
            "ip-address": "10.0.0.100",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "host1.example.com",
            "subnet-id": 1,
            "cltt": 1700000000,
            "valid-lft": 3600,
            "state": 0,
        },
    }
]
_LEASE4_NOT_FOUND = [{"result": 3, "text": "Lease not found."}]
_LEASE_UPDATE_OK = [{"result": 0, "text": "Lease updated."}]


class TestLeaseUpdate(TestCase):
    """Tests for KeaClient.lease_update()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_fetches_then_updates(self):
        """lease_update calls lease4-get then lease4-update in sequence."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE4_GET_RESP, _LEASE_UPDATE_OK),
        ) as mock_post:
            self.client.lease_update(version=4, ip_address="10.0.0.100")
        self.assertEqual(self._cmds(mock_post), ["lease4-get", "lease4-update"])

    def test_merges_hostname(self):
        """hostname kwarg replaces the existing hostname in the update payload."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE4_GET_RESP, _LEASE_UPDATE_OK),
        ) as mock_post:
            self.client.lease_update(version=4, ip_address="10.0.0.100", hostname="new.example.com")
        payloads = self._payloads(mock_post)
        update_payload = next(p for p in payloads if p["command"] == "lease4-update")
        self.assertEqual(update_payload["arguments"]["hostname"], "new.example.com")

    def test_merges_hw_address(self):
        """hw_address kwarg replaces the existing hw-address in the update payload."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE4_GET_RESP, _LEASE_UPDATE_OK),
        ) as mock_post:
            self.client.lease_update(version=4, ip_address="10.0.0.100", hw_address="11:22:33:44:55:66")
        payloads = self._payloads(mock_post)
        update_payload = next(p for p in payloads if p["command"] == "lease4-update")
        self.assertEqual(update_payload["arguments"]["hw-address"], "11:22:33:44:55:66")

    def test_merges_valid_lft(self):
        """valid_lft kwarg replaces the existing valid-lft in the update payload."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE4_GET_RESP, _LEASE_UPDATE_OK),
        ) as mock_post:
            self.client.lease_update(version=4, ip_address="10.0.0.100", valid_lft=7200)
        payloads = self._payloads(mock_post)
        update_payload = next(p for p in payloads if p["command"] == "lease4-update")
        self.assertEqual(update_payload["arguments"]["valid-lft"], 7200)

    def test_raises_kea_exception_when_lease_not_found(self):
        """KeaException raised when lease4-get returns result=3 (not found)."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LEASE4_NOT_FOUND),
        ):
            with self.assertRaises(KeaException):
                self.client.lease_update(version=4, ip_address="10.0.0.100")

    def test_v6_uses_dhcp6_service(self):
        """For version=6, both commands use service=['dhcp6']."""
        lease6_get_resp = [
            {
                "result": 0,
                "arguments": {
                    "ip-address": "2001:db8::100",
                    "duid": "00:01:00:01:aa:bb:cc:dd:ee:ff",
                    "hostname": "v6host.example.com",
                    "subnet-id": 10,
                    "cltt": 1700000000,
                    "valid-lft": 3600,
                    "state": 0,
                },
            }
        ]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(lease6_get_resp, _LEASE_UPDATE_OK),
        ) as mock_post:
            self.client.lease_update(version=6, ip_address="2001:db8::100")
        payloads = self._payloads(mock_post)
        for p in payloads:
            self.assertEqual(p["service"], ["dhcp6"])
        self.assertEqual(self._cmds(mock_post), ["lease6-get", "lease6-update"])

    def test_merges_duid_for_v6_lease(self):
        """lease_update includes duid in the update payload when duid is given."""
        lease6_get_resp = [
            {
                "result": 0,
                "arguments": {
                    "ip-address": "2001:db8::100",
                    "duid": "00:01:00:01:ab:cd:ef:01",
                    "hostname": "host6.example.com",
                    "subnet-id": 2,
                    "cltt": 1700000000,
                    "valid-lft": 3600,
                    "state": 0,
                },
            }
        ]
        new_duid = "00:01:00:01:ff:ee:dd:cc"
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(lease6_get_resp, _LEASE_UPDATE_OK),
        ) as mock_post:
            self.client.lease_update(version=6, ip_address="2001:db8::100", duid=new_duid)
        payloads = self._payloads(mock_post)
        update_payload = next(p for p in payloads if p["command"] == "lease6-update")
        self.assertEqual(update_payload["arguments"]["duid"], new_duid)


# ---------------------------------------------------------------------------
# TestLeaseAdd
# ---------------------------------------------------------------------------

_LEASE_ADD_OK = [{"result": 0, "text": "Lease added."}]
_LEASE_ADD_FAIL = [{"result": 1, "text": "address already in use"}]


class TestLeaseAdd(TestCase):
    """Tests for KeaClient.lease_add(version, lease) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_v4_sends_correct_command_and_payload(self):
        """lease4-add command is sent with the provided lease dict as arguments."""
        lease = {"ip-address": "10.0.0.50", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1}
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_LEASE_ADD_OK)) as mock_post:
            self.client.lease_add(version=4, lease=lease)
        payload = self._payloads(mock_post)[0]
        self.assertEqual(payload["command"], "lease4-add")
        self.assertEqual(payload["service"], ["dhcp4"])
        self.assertEqual(payload["arguments"], lease)

    def test_v6_uses_dhcp6_service(self):
        """For version=6, command is lease6-add and service is dhcp6."""
        lease = {"ip-address": "2001:db8::1", "duid": "00:01:02:03", "iaid": 12345}
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_LEASE_ADD_OK)) as mock_post:
            self.client.lease_add(version=6, lease=lease)
        payload = self._payloads(mock_post)[0]
        self.assertEqual(payload["command"], "lease6-add")
        self.assertEqual(payload["service"], ["dhcp6"])

    def test_returns_none_on_success(self):
        """lease_add returns None on success."""
        lease = {"ip-address": "10.0.0.50"}
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_LEASE_ADD_OK)):
            result = self.client.lease_add(version=4, lease=lease)
        self.assertIsNone(result)

    def test_raises_kea_exception_on_error(self):
        """KeaException raised when Kea returns a non-zero result."""
        lease = {"ip-address": "10.0.0.50"}
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_LEASE_ADD_FAIL)):
            with self.assertRaises(KeaException):
                self.client.lease_add(version=4, lease=lease)


# ---------------------------------------------------------------------------
# TestNetworkSubnetAdd
# ---------------------------------------------------------------------------

_NETWORK_SUBNET_ADD_OK = {"result": 0, "text": "Subnet added to shared network."}
_NETWORK_SUBNET_ADD_FAIL = {"result": 1, "text": "subnet not found"}


class TestNetworkSubnetAdd(TestCase):
    """Tests for KeaClient.network_subnet_add(version, name, subnet_id) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_sends_one_command_with_name_and_id_without_a_persist_step(self):
        """network4-subnet-add is sent with name and id in arguments, and nothing after it."""
        with stub_kea({"network4-subnet-add": _NETWORK_SUBNET_ADD_OK}) as kea:
            self.client.network_subnet_add(version=4, name="prod-net", subnet_id=5)
        self.assertEqual(kea.commands(), ["network4-subnet-add"])
        (body,) = kea.bodies("network4-subnet-add")
        self.assertEqual(body["service"], ["dhcp4"])
        self.assertEqual(body["arguments"], {"name": "prod-net", "id": 5})

    def test_raises_kea_exception_on_failure(self):
        """KeaException is raised when the command returns a non-zero result."""
        with stub_kea({"network4-subnet-add": _NETWORK_SUBNET_ADD_FAIL}) as kea, self.assertRaises(KeaException):
            self.client.network_subnet_add(version=4, name="prod-net", subnet_id=99)
        self.assertEqual(kea.bodies("network4-subnet-add")[0]["arguments"], {"name": "prod-net", "id": 99})


# ---------------------------------------------------------------------------
# TestNetworkSubnetDel
# ---------------------------------------------------------------------------


class TestNetworkSubnetDel(TestCase):
    """KeaClient.network_subnet_del sends one network{v}-subnet-del and does not persist."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_sends_one_command_with_name_and_id_without_a_persist_step(self):
        removed = {"result": 0, "text": "IPv4 subnet 10.0.5.0/24 (id 5) is now removed from shared network 'prod-net'"}
        with stub_kea({"network4-subnet-del": removed}) as kea:
            self.client.network_subnet_del(4, "prod-net", 5)
        self.assertEqual(kea.commands(), ["network4-subnet-del"])
        (body,) = kea.bodies("network4-subnet-del")
        self.assertEqual(body["service"], ["dhcp4"])
        self.assertEqual(body["arguments"], {"name": "prod-net", "id": 5})

    def test_a_failure_result_raises_kea_exception(self):
        outside = {
            "result": 3,
            "text": "The IPv4 subnet with id 99 is not part of the shared network with name 'prod-net' found",
        }
        with stub_kea({"network4-subnet-del": outside}), self.assertRaises(KeaException):
            self.client.network_subnet_del(4, "prod-net", 99)

    def test_a_malformed_reply_raises_runtime_error(self):
        with stub_kea({"network4-subnet-del": [_OK[0], _OK[0]]}), self.assertRaises(RuntimeError):
            self.client.network_subnet_del(4, "prod-net", 5)


# ---------------------------------------------------------------------------
# TestLeaseGetByIp
# ---------------------------------------------------------------------------

_LEASE4_GET_FOUND_RESP = [
    {
        "result": 0,
        "arguments": {
            "ip-address": "192.168.1.10",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "host1.example.com",
            "valid-lft": 3600,
            "state": 0,
        },
    }
]

_LEASE4_GET_NOT_FOUND_RESP = [{"result": 3, "text": "Lease not found."}]
_LEASE6_GET_FOUND_RESP = [
    {
        "result": 0,
        "arguments": {
            "ip-address": "2001:db8::1",
            "duid": "00:01:02:03",
            "valid-lft": 7200,
            "state": 0,
        },
    }
]


class TestLeaseGetByIp(TestCase):
    """Tests for KeaClient.lease_get_by_ip()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payload(self, mock_post):
        return mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]

    def test_rejects_an_invalid_version_or_empty_address(self):
        for version, address in ((5, "192.168.1.10"), (4, "")):
            with self.subTest(version=version, address=address), self.assertRaises(ValueError):
                self.client.lease_get_by_ip(version=version, ip_address=address)

    def test_returns_the_first_lease_from_the_delegated_search(self):
        leases = [{"ip-address": "192.168.1.10"}, {"ip-address": "192.168.1.11"}]

        class SearchClient(KeaClient):
            def lease_search(self, version, selector, value, *, state=None):
                self.search = (version, selector, value, state)
                return leases

        client = SearchClient(url="http://kea:8000")

        result = client.lease_get_by_ip(version=4, ip_address="192.168.1.10")

        self.assertIs(result, leases[0])
        self.assertEqual(client.search, (4, constants.BY_IP, "192.168.1.10", None))

    def test_v4_returns_lease_dict_when_found(self):
        """Returns the arguments dict when lease is found (result=0)."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_LEASE4_GET_FOUND_RESP),
        ):
            result = self.client.lease_get_by_ip(version=4, ip_address="192.168.1.10")
        self.assertIsNotNone(result)
        self.assertEqual(result["ip-address"], "192.168.1.10")

    def test_v4_returns_none_when_not_found(self):
        """Returns None when Kea responds with result=3 (not found)."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_LEASE4_GET_NOT_FOUND_RESP),
        ):
            result = self.client.lease_get_by_ip(version=4, ip_address="192.168.1.99")
        self.assertIsNone(result)

    def test_v6_uses_dhcp6_service(self):
        """Uses dhcp6 service and lease6-get command for version=6."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_LEASE6_GET_FOUND_RESP),
        ) as mock_post:
            self.client.lease_get_by_ip(version=6, ip_address="2001:db8::1")
        payload = self._payload(mock_post)
        self.assertEqual(payload["command"], "lease6-get")
        self.assertEqual(payload["service"], ["dhcp6"])

    def test_v4_uses_dhcp4_service(self):
        """Uses dhcp4 service and lease4-get command for version=4."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_LEASE4_GET_FOUND_RESP),
        ) as mock_post:
            self.client.lease_get_by_ip(version=4, ip_address="192.168.1.10")
        payload = self._payload(mock_post)
        self.assertEqual(payload["command"], "lease4-get")
        self.assertEqual(payload["service"], ["dhcp4"])

    def test_sends_ip_address_in_arguments(self):
        """Sends the IP address in the arguments dict."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_LEASE4_GET_FOUND_RESP),
        ) as mock_post:
            self.client.lease_get_by_ip(version=4, ip_address="10.0.0.5")
        payload = self._payload(mock_post)
        self.assertEqual(payload["arguments"]["ip-address"], "10.0.0.5")

    def test_raises_kea_exception_on_error(self):
        """Raises KeaException when Kea returns a non-0/3 result code."""
        error_resp = [{"result": 1, "text": "Internal server error"}]
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(error_resp),
        ):
            with self.assertRaises(KeaException):
                self.client.lease_get_by_ip(version=4, ip_address="10.0.0.1")

    def test_v6_returns_none_when_not_found(self):
        """Returns None for v6 not-found (result=3)."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response([{"result": 3, "text": "Lease not found."}]),
        ):
            result = self.client.lease_get_by_ip(version=6, ip_address="2001:db8::99")
        self.assertIsNone(result)


class TestLeaseQueryGuardMessage(TestCase):
    """Tests for safe user guidance from rejected lease queries."""

    def test_each_guard_failure_has_actionable_guidance(self):
        cases = (
            (LeaseQueryNotMeasurable(2), 2, "cannot safely measure"),
            (LeaseQueryPreflightUnavailable(), None, "stat_cmds"),
            (LeaseQueryTooBroad(101, 100), None, "Select the Active or Declined state"),
            (LeaseQueryTooBroad(101, 100), 0, "exact IP or client identifier"),
            (LeaseQueryGuardError(), None, "more specific search"),
        )

        for error, state, expected in cases:
            with self.subTest(error=type(error).__name__, state=state):
                self.assertIn(expected, lease_query_guard_message(error, state))


class TestLeaseSearch(TestCase):
    """Tests for KeaClient.lease_search()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    @patch("requests.Session.post")
    def test_rejects_selector_that_the_address_family_does_not_support(self, mock_post):
        with self.assertRaisesRegex(ValueError, "duid.*DHCPv4"):
            self.client.lease_search(version=4, selector="duid", value="01:02:03:04")

        mock_post.assert_not_called()

    def test_rejects_invalid_query_parameters_before_any_request(self):
        cases = (
            (5, "ip", "198.18.0.10", None, "version must be 4 or 6"),
            (4, "subnet", "", None, "non-empty CIDR"),
            (4, "hostname", "host.example.invalid", 0, "state can only"),
            (4, "hostname", "", None, "non-empty string"),
            (4, "subnet_id", True, None, "positive integer"),
            (4, "subnet_id", 1.5, None, "positive integer"),
            (4, "subnet_id", object(), None, "positive integer"),
        )

        for version, selector, value, state, message in cases:
            with self.subTest(version=version, selector=selector, value=repr(value), state=state), stub_kea({}) as kea:
                with self.assertRaisesRegex((ValueError, LeaseQueryNotMeasurable), message):
                    self.client.lease_search(version, selector, value, state=state)
                self.assertEqual(kea.commands(), [])

    def test_hardware_address_search_returns_matching_leases(self):
        lease = {"ip-address": "198.18.0.10", "hw-address": "aa:bb:cc:dd:ee:ff"}
        with stub_kea(
            {
                "lease4-get-by-hw-address": {
                    "result": 0,
                    "arguments": {"leases": [lease]},
                }
            }
        ) as kea:
            result = self.client.lease_search(
                version=4,
                selector="hw",
                value="aa:bb:cc:dd:ee:ff",
            )

        self.assertEqual(result, [lease])
        self.assertEqual(
            kea.bodies("lease4-get-by-hw-address")[0]["arguments"],
            {"hw-address": "aa:bb:cc:dd:ee:ff"},
        )

    def test_supported_selectors_map_to_one_canonical_kea_command(self):
        cases = (
            (4, "ip", "198.18.0.10", "lease4-get", {"ip-address": "198.18.0.10"}, False),
            (
                4,
                "hostname",
                "host.example.invalid",
                "lease4-get-by-hostname",
                {"hostname": "host.example.invalid"},
                True,
            ),
            (4, "client_id", "01:aa:bb", "lease4-get-by-client-id", {"client-id": "01:aa:bb"}, True),
            (4, "subnet_id", 12, "lease4-get-all", {"subnets": [12]}, True),
            (6, "ip", "2001:db8::10", "lease6-get", {"ip-address": "2001:db8::10"}, False),
            (
                6,
                "hostname",
                "host.example.invalid",
                "lease6-get-by-hostname",
                {"hostname": "host.example.invalid"},
                True,
            ),
            (6, "duid", "00:01:02:03", "lease6-get-by-duid", {"duid": "00:01:02:03"}, True),
            (6, "subnet_id", "13", "lease6-get-all", {"subnets": [13]}, True),
        )

        for version, selector, value, command, arguments, multiple in cases:
            lease = {"ip-address": "198.18.0.10" if version == 4 else "2001:db8::10"}
            response_arguments = {"leases": [lease]} if multiple else lease
            responses = {command: {"result": 0, "arguments": response_arguments}}
            if selector == "subnet_id":
                responses[f"stat-lease{version}-get"] = _subnet_stats(version, int(value))
            with (
                self.subTest(version=version, selector=selector),
                stub_kea(responses) as kea,
            ):
                result = self.client.lease_search(version, selector, value)
                self.assertEqual(result, [lease])
                self.assertEqual(kea.bodies(command)[0]["arguments"], arguments)

    def test_not_found_returns_empty_collection(self):
        with stub_kea({"lease6-get-by-hostname": {"result": 3, "text": "not found"}}):
            result = self.client.lease_search(6, "hostname", "missing.example.invalid")

        self.assertEqual(result, [])

    def test_malformed_lease_collection_is_rejected(self):
        with stub_kea(
            {
                "lease4-get-by-hostname": {
                    "result": 0,
                    "arguments": {"leases": "not-a-list"},
                }
            }
        ):
            with self.assertRaisesRegex(RuntimeError, "malformed leases collection"):
                self.client.lease_search(4, "hostname", "host.example.invalid")

    def test_large_subnet_is_rejected_before_get_all(self):
        client = KeaClient(url="http://kea:8000", max_unpaged_leases=100)
        with stub_kea({"stat-lease4-get": _subnet_stats(4, 12, assigned=101)}) as kea:
            with self.assertRaisesRegex(LeaseQueryTooBroad, "101.*100"):
                client.lease_search(4, "subnet_id", 12)

        self.assertEqual(kea.commands(), ["stat-lease4-get"])

    def test_state_qualifier_uses_the_subnet_scoped_state_command(self):
        client = KeaClient(url="http://kea:8000", max_unpaged_leases=100)
        for version in (4, 6):
            lease = {"ip-address": "198.18.0.10" if version == 4 else "2001:db8::10", "state": 1}
            with (
                self.subTest(version=version),
                stub_kea(
                    {
                        f"stat-lease{version}-get": _subnet_stats(version, 12, assigned=201, declined=1),
                        f"lease{version}-get-by-state": {"result": 0, "arguments": {"leases": [lease]}},
                    }
                ) as kea,
            ):
                result = client.lease_search(version, "subnet_id", 12, state=1)

                self.assertEqual(result, [lease])
                self.assertEqual(kea.commands(), [f"stat-lease{version}-get", f"lease{version}-get-by-state"])
                self.assertEqual(
                    kea.bodies(f"lease{version}-get-by-state")[0]["arguments"],
                    {"subnet-id": 12, "state": 1},
                )

    def test_unmeasured_subnet_state_is_rejected_before_any_request(self):
        with stub_kea({}) as kea:
            with self.assertRaisesRegex(LeaseQueryNotMeasurable, "state 2"):
                self.client.lease_search(4, "subnet_id", 12, state=2)

        self.assertEqual(kea.commands(), [])

    def test_missing_statistics_hook_fails_closed(self):
        with stub_kea({"stat-lease4-get": {"result": 2, "text": "unknown command"}}) as kea:
            with self.assertRaises(LeaseQueryPreflightUnavailable):
                self.client.lease_search(4, "subnet_id", 12)

        self.assertEqual(kea.commands(), ["stat-lease4-get"])

    def test_unmeasured_subnet_fails_closed_instead_of_counting_zero(self):
        """Refuse the unpaged query when Kea reports no statistics for the Subnet.

        An empty statistics answer says the Subnet was not measured, not that it holds no
        leases, so reading it as zero would send exactly the unbounded query the guard
        exists to stop.
        """
        columns = ["subnet-id", "assigned-addresses", "declined-addresses"]
        cases = (
            ("empty result", {"result": 3, "text": "no statistics"}),
            (
                "another Subnet only",
                {"result": 0, "arguments": {"result-set": {"columns": columns, "rows": [[13, 1, 0]]}}},
            ),
        )

        for label, response in cases:
            with self.subTest(response=label), stub_kea({"stat-lease4-get": response}) as kea:
                with self.assertRaises(LeaseQueryPreflightUnavailable) as ctx:
                    self.client.lease_search(4, "subnet_id", 12)

                self.assertEqual(ctx.exception.reason, "statistics")
                self.assertIn("stat_cmds", lease_query_guard_message(ctx.exception, None))
                self.assertEqual(kea.commands(), ["stat-lease4-get"])

    def test_missing_state_command_fails_closed_when_unmeasured(self):
        client = KeaClient(url="http://kea:8000", max_unpaged_leases=None)
        with stub_kea(
            {
                "lease4-get-by-state": {"result": 2, "text": "unknown command"},
            }
        ) as kea:
            with self.assertRaises(LeaseQueryPreflightUnavailable) as ctx:
                client.lease_search(4, "subnet_id", 12, state=0)

        self.assertIn("3.1.5", lease_query_guard_message(ctx.exception, 0))
        self.assertEqual(kea.commands(), ["lease4-get-by-state"])

    def test_missing_state_command_does_not_fetch_unmeasured_retained_rows(self):
        for version in (4, 6):
            for limit in (100, None):
                with self.subTest(version=version, limit=limit):
                    client = KeaClient(url="http://kea:8000", max_unpaged_leases=limit)
                    active = {"ip-address": "198.18.0.1" if version == 4 else "2001:db8::1", "state": 0}
                    retained = [
                        {"ip-address": f"198.18.1.{index}" if version == 4 else f"2001:db8:1::{index:x}", "state": 2}
                        for index in range(1, 201)
                    ]
                    commands = [f"stat-lease{version}-get"] if limit is not None else []
                    commands.append(f"lease{version}-get-by-state")
                    with stub_kea(
                        {
                            f"stat-lease{version}-get": _subnet_stats(version, 12, assigned=1),
                            f"lease{version}-get-by-state": {"result": 2, "text": "unknown command"},
                            f"lease{version}-get-all": {"result": 0, "arguments": {"leases": [active, *retained]}},
                        }
                    ) as kea:
                        try:
                            with self.assertRaises(LeaseQueryPreflightUnavailable) as ctx:
                                client.lease_search(version, "subnet_id", 12, state=0)
                            self.assertEqual(ctx.exception.reason, "state-command")
                        finally:
                            self.assertEqual(kea.commands(), commands)

    def test_missing_state_command_fails_closed_when_only_filtered_count_is_bounded(self):
        client = KeaClient(url="http://kea:8000", max_unpaged_leases=100)
        with stub_kea(
            {
                "stat-lease4-get": _subnet_stats(4, 12, assigned=201, declined=1),
                "lease4-get-by-state": {"result": 2, "text": "unknown command"},
            }
        ) as kea:
            with self.assertRaises(LeaseQueryPreflightUnavailable):
                client.lease_search(4, "subnet_id", 12, state=1)
        self.assertEqual(kea.commands(), ["stat-lease4-get", "lease4-get-by-state"])

    def test_non_hook_statistics_error_propagates(self):
        with stub_kea({"stat-lease4-get": {"result": 1, "text": "database failure"}}):
            with self.assertRaises(KeaException):
                self.client.lease_search(4, "subnet_id", 12)

    def test_malformed_statistics_are_rejected(self):
        columns = ["subnet-id", "assigned-addresses", "declined-addresses"]
        cases = (
            ([], "malformed response"),
            ({"result": 0, "arguments": {}}, "malformed statistics"),
            (
                {"result": 0, "arguments": {"result-set": {"columns": ["subnet-id"], "rows": [[12]]}}},
                "omitted required statistics columns",
            ),
            (
                {"result": 0, "arguments": {"result-set": {"columns": columns, "rows": [[]]}}},
                "malformed statistics row",
            ),
            (
                {"result": 0, "arguments": {"result-set": {"columns": columns, "rows": [[12, True, 0]]}}},
                "invalid lease count",
            ),
            (
                {"result": 0, "arguments": {"result-set": {"columns": columns, "rows": [[12, 0, 1]]}}},
                "inconsistent lease counts",
            ),
        )

        for response, message in cases:
            with self.subTest(message=message), stub_kea({"stat-lease4-get": response}):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.client.lease_search(4, "subnet_id", 12)

    def test_v6_delegated_prefixes_contribute_to_the_guard(self):
        client = KeaClient(url="http://kea:8000", max_unpaged_leases=100)
        with stub_kea({"stat-lease6-get": _subnet_stats(6, 12, assigned=0, declined=0, assigned_pds=101)}) as kea:
            with self.assertRaisesRegex(LeaseQueryTooBroad, "101.*100"):
                client.lease_search(6, "subnet_id", 12, state=0)

        self.assertEqual(kea.commands(), ["stat-lease6-get"])

    def test_subnet_cidr_resolves_to_id_before_guarded_query(self):
        lease = {"ip-address": "198.18.0.10", "state": 0}
        with stub_kea(
            {
                "config-get": {
                    "result": 0,
                    "arguments": {"Dhcp4": {"subnet4": [{"id": 12, "subnet": "198.18.0.0/24"}]}},
                },
                "stat-lease4-get": _subnet_stats(4, 12, assigned=1),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": [lease]}},
            }
        ) as kea:
            result = self.client.lease_search(4, "subnet", "198.18.0.0/24", state=0)

        self.assertEqual(result, [lease])
        self.assertEqual(
            kea.commands(),
            ["config-get", "stat-lease4-get", "lease4-get-by-state"],
        )

    def test_subnet_cidr_resolves_a_shared_network_member(self):
        with stub_kea(
            {
                "config-get": {
                    "result": 0,
                    "arguments": {
                        "Dhcp4": {
                            "subnet4": [],
                            "shared-networks": [{"name": "access", "subnet4": [{"id": 12, "subnet": "198.18.0.0/24"}]}],
                        }
                    },
                },
                "stat-lease4-get": _subnet_stats(4, 12, assigned=0),
                "lease4-get-all": {"result": 3},
            }
        ) as kea:
            result = self.client.lease_search(4, "subnet", "198.18.0.0/24")

        self.assertEqual(result, [])
        self.assertEqual(kea.commands(), ["config-get", "stat-lease4-get", "lease4-get-all"])

    def test_subnet_cidr_matches_equivalent_configured_network_text(self):
        lease = {"ip-address": "2001:db8::10", "state": 0}
        with stub_kea(
            {
                "config-get": {
                    "result": 0,
                    "arguments": {
                        "Dhcp6": {"subnet6": [{"id": 21, "subnet": "2001:0db8:0:0::/64"}]},
                    },
                },
                "stat-lease6-get": _subnet_stats(6, 21, assigned=1),
                "lease6-get-all": {"result": 0, "arguments": {"leases": [lease]}},
            }
        ) as kea:
            result = self.client.lease_search(6, "subnet", "2001:db8::/64")

        self.assertEqual(result, [lease])
        self.assertEqual(kea.commands(), ["config-get", "stat-lease6-get", "lease6-get-all"])

    def test_malformed_configured_subnet_network_is_rejected_without_a_match(self):
        cases = (
            ("not-a-subnet-entry", "malformed Subnet entry"),
            ({"id": 21, "subnet": None}, "valid IPv6 CIDR"),
            ({"id": 21, "subnet": "not-a-network"}, "valid IPv6 CIDR"),
            ({"id": 21, "subnet": "198.18.0.0/24"}, "valid IPv6 CIDR"),
        )

        for configured_subnet, message in cases:
            with (
                self.subTest(configured_subnet=configured_subnet),
                stub_kea(
                    {
                        "config-get": {
                            "result": 0,
                            "arguments": {"Dhcp6": {"subnet6": [configured_subnet]}},
                        }
                    }
                ),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                self.client.configured_subnet_id_from_cidr(6, "2001:db8::/64")

    def test_matching_configured_subnet_requires_a_valid_id(self):
        for subnet_id in (None, True, 0, "21"):
            with (
                self.subTest(subnet_id=subnet_id),
                stub_kea(
                    {
                        "config-get": {
                            "result": 0,
                            "arguments": {"Dhcp6": {"subnet6": [{"id": subnet_id, "subnet": "2001:db8::/64"}]}},
                        }
                    }
                ),
                self.assertRaisesRegex(RuntimeError, "without a valid ID"),
            ):
                self.client.configured_subnet_id_from_cidr(6, "2001:db8::/64")

    def test_two_subnets_for_one_network_are_ambiguous(self):
        # Kea 3.2.0 loads both spellings as separate Subnets.
        subnets = [{"id": 21, "subnet": "2001:db8::5/64"}, {"id": 22, "subnet": "2001:db8::/64"}]
        with (
            stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp6": {"subnet6": subnets}}}}),
            self.assertRaisesRegex(RuntimeError, r"more than one Subnet for 2001:db8::/64: IDs \[21, 22\]"),
        ):
            self.client.configured_subnet_id_from_cidr(6, "2001:db8::/64")

    def test_malformed_unrelated_subnet_does_not_hide_matching_network(self):
        with stub_kea(
            {
                "config-get": {
                    "result": 0,
                    "arguments": {
                        "Dhcp6": {
                            "subnet6": [
                                {"id": 20, "subnet": "not-a-network"},
                                {"id": 21, "subnet": "2001:0db8:0:0::/64"},
                            ]
                        }
                    },
                }
            }
        ):
            subnet_id = self.client.configured_subnet_id_from_cidr(6, "2001:db8::/64")

        self.assertEqual(subnet_id, 21)

    def test_explicitly_disabled_guard_skips_statistics(self):
        client = KeaClient(url="http://kea:8000", max_unpaged_leases=None)
        lease = {"ip-address": "198.18.0.10", "state": 0}
        with stub_kea({"lease4-get-all": {"result": 0, "arguments": {"leases": [lease]}}}) as kea:
            result = client.lease_search(4, "subnet_id", 12)

        self.assertEqual(result, [lease])
        self.assertEqual(kea.commands(), ["lease4-get-all"])


# ---------------------------------------------------------------------------
# TestSubnetGet
# ---------------------------------------------------------------------------

_SUBNET4_GET_FULL_RESP = [
    {
        "result": 0,
        "arguments": {
            "subnet4": [
                {
                    "id": 42,
                    "subnet": "10.0.0.0/24",
                    "pools": [{"pool": "10.0.0.100-10.0.0.200"}],
                    "option-data": [{"name": "routers", "data": "10.0.0.1"}],
                    "relay": {"ip-addresses": ["10.0.0.254"]},
                    "valid-lifetime": 3600,
                }
            ]
        },
    }
]


class TestSubnetGet(TestCase):
    """Tests for KeaClient.subnet_get()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_returns_full_subnet_dict(self):
        """subnet_get returns the complete subnet dict including relay and option-data."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_SUBNET4_GET_FULL_RESP),
        ):
            result = self.client.subnet_get(version=4, subnet_id=42)
        self.assertEqual(result["id"], 42)
        self.assertEqual(result["subnet"], "10.0.0.0/24")
        self.assertEqual(result["relay"], {"ip-addresses": ["10.0.0.254"]})
        self.assertEqual(result["option-data"], [{"name": "routers", "data": "10.0.0.1"}])

    def test_sends_correct_command_and_id(self):
        """subnet_get sends subnet4-get with the correct id argument."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_SUBNET4_GET_FULL_RESP),
        ) as mock_post:
            self.client.subnet_get(version=4, subnet_id=42)
        sent = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        self.assertEqual(sent["command"], "subnet4-get")
        self.assertEqual(sent["arguments"]["id"], 42)

    def test_raises_kea_exception_when_not_found(self):
        """subnet_get raises KeaException when the subnet list is empty."""
        resp = [{"result": 0, "arguments": {"subnet4": []}}]
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(resp),
        ):
            with self.assertRaises(KeaException):
                self.client.subnet_get(version=4, subnet_id=99)

    def test_v6_sends_subnet6_get(self):
        """subnet_get sends subnet6-get for version=6."""
        resp = [{"result": 0, "arguments": {"subnet6": [{"id": 7, "subnet": "2001:db8::/48"}]}}]
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(resp),
        ) as mock_post:
            self.client.subnet_get(version=6, subnet_id=7)
        sent = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        self.assertEqual(sent["command"], "subnet6-get")

    def test_returns_independent_top_level_dict(self):
        """subnet_get returns a fresh top-level dict so callers can add/remove keys without affecting subsequent calls."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_SUBNET4_GET_FULL_RESP),
        ):
            r1 = self.client.subnet_get(version=4, subnet_id=42)
            r2 = self.client.subnet_get(version=4, subnet_id=42)
        r1["extra"] = "test"
        self.assertNotIn("extra", r2)


# ---------------------------------------------------------------------------
# subnet_definition and subnet_update
# ---------------------------------------------------------------------------

# Every managed field empty: a form-managed DHCP Option is removed, and every lifetime keeps its live value.
_BLANK = SubnetEdit(
    fields=_NO_FIELDS,
    valid_lifetime=None,
    min_valid_lifetime=None,
    max_valid_lifetime=None,
    renew_timer=None,
    rebind_timer=None,
)
_FORM_FIELDS = frozenset(field.name for field in dataclasses.fields(SubnetFields))


def _edit(**values) -> SubnetEdit:
    """Return the blank edit with *values*, given by the name of a form field, a lifetime or a timer."""
    fields = {name: value for name, value in values.items() if name in _FORM_FIELDS}
    lifetimes = {name: value for name, value in values.items() if name not in _FORM_FIELDS}
    return replace(_BLANK, fields=replace(_BLANK.fields, **fields), **lifetimes)


def _subnet_get_reply(version: int, subnet) -> dict:
    return {"result": 0, "arguments": {f"subnet{version}": [subnet]}}


class TestSubnetDefinition(TestCase):
    """KeaClient.subnet_definition reads one Subnet for an update. Two reads of the same Subnet are equal."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_two_reads_of_the_same_subnet_are_equal_and_a_changed_subnet_is_not(self):
        live = {"id": 42, "subnet": "10.0.0.5/24", "pools": [], "valid-lifetime": 3600}
        reordered = dict(reversed(live.items()))
        changed = {**live, "valid-lifetime": 7200}
        replies = queued(*(_subnet_get_reply(4, subnet) for subnet in (live, reordered, changed)))
        with stub_kea({"subnet4-get": replies}) as kea:
            first, same, other = (self.client.subnet_definition(4, 42) for _ in range(3))
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        self.assertEqual(first.network, ipaddress.ip_network("10.0.0.0/24"))
        self.assertEqual([body["arguments"] for body in kea.bodies("subnet4-get")], [{"id": 42}] * 3)

    def test_a_reply_that_is_not_one_valid_subnet_raises_runtime_error(self):
        for label, entry in (
            ("not an object", "subnet"),
            ("another ID", {"id": 7, "subnet": "10.0.0.0/24"}),
            ("numeric CIDR", {"id": 42, "subnet": 3323068416}),
            ("no CIDR", {"id": 42}),
            ("other family", {"id": 42, "subnet": "2001:db8::/64"}),
            ("option-data not a list", {"id": 42, "subnet": "10.0.0.0/24", "option-data": {}}),
            ("option entry not an object", {"id": 42, "subnet": "10.0.0.0/24", "option-data": ["routers"]}),
        ):
            with self.subTest(label), stub_kea({"subnet4-get": _subnet_get_reply(4, entry)}) as kea:
                with self.assertRaises(RuntimeError):
                    self.client.subnet_definition(4, 42)
                self.assertEqual(kea.commands(), ["subnet4-get"])

    def test_a_missing_subnet_raises_kea_exception(self):
        missing = {"result": 3, "text": "No subnet with id 42 found"}
        with stub_kea({"subnet4-get": missing}), self.assertRaises(KeaException):
            self.client.subnet_definition(4, 42)


class TestSubnetUpdate(TestCase):
    """KeaClient.subnet_update sends the Subnet that it read with the form fields, and does not persist."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _sent(self, live: dict, version: int = 4, **fields) -> dict:
        """Return the Subnet that subnet{v}-update sends for *live* and the form *fields*."""
        responses = {
            f"subnet{version}-get": _subnet_get_reply(version, live),
            f"subnet{version}-update": {"result": 0, "text": f"IPv{version} subnet updated"},
        }
        with stub_kea(responses) as kea:
            definition = self.client.subnet_definition(version, live["id"])
            self.client.subnet_update(definition.family, definition.edited(_edit(**fields)))
        self.assertEqual(kea.commands(), [f"subnet{version}-get", f"subnet{version}-update"])
        (body,) = kea.bodies(f"subnet{version}-update")
        self.assertEqual(body["service"], [f"dhcp{version}"])
        (subnet,) = body["arguments"][f"subnet{version}"]
        return subnet

    def _options(self, existing: list, version: int = 4, **fields) -> list:
        subnet = "10.0.0.0/24" if version == 4 else "2001:db8::/48"
        return self._sent({"id": 42, "subnet": subnet, "option-data": existing}, version, **fields)["option-data"]

    def test_the_update_keeps_every_field_that_the_form_does_not_manage(self):
        live = {
            "id": 42,
            "subnet": "10.0.0.0/24",
            "relay": {"ip-addresses": ["10.0.0.254"]},
            "allocator": "random",
            "client-class": "premium",
            "pools": [{"pool": "10.0.0.50-10.0.0.99", "option-data": []}],
            "option-data": [{"name": "routers", "data": "10.0.0.1"}, {"name": "domain-name", "data": "example.org"}],
            "valid-lifetime": 7200,
            "ddns-qualifying-suffix": "old.example.org.",
            "metadata": {"server-tags": ["all"]},
        }
        sent = self._sent(live, pools=("10.0.0.100-10.0.0.110",), gateway="10.0.0.2", ddns_qualifying_suffix="")
        self.assertEqual(
            sent,
            {
                "id": 42,
                "subnet": "10.0.0.0/24",
                "relay": {"ip-addresses": ["10.0.0.254"]},
                "allocator": "random",
                "client-class": "premium",
                "pools": [{"pool": "10.0.0.100-10.0.0.110"}],
                "option-data": [
                    {"name": "domain-name", "data": "example.org"},
                    {"name": "routers", "data": "10.0.0.2"},
                ],
                "valid-lifetime": 7200,
            },
        )

    def test_a_lifetime_or_timer_is_set_with_the_subnet_parameter_name_and_none_keeps_it(self):
        live = {
            "id": 42,
            "subnet": "10.0.0.0/24",
            "valid-lifetime": 3600,
            "min-valid-lifetime": 1800,
            "max-valid-lifetime": 7200,
            "renew-timer": 900,
            "rebind-timer": 1500,
        }
        kept = self._sent(live)
        self.assertEqual({key: kept[key] for key in live}, live)
        values = {
            "valid_lifetime": 4000,
            "min_valid_lifetime": 2000,
            "max_valid_lifetime": 8000,
            "renew_timer": 600,
            "rebind_timer": 900,
        }
        sent = self._sent(live, **values)
        self.assertEqual(
            {key: sent[key] for key in live},
            {
                "id": 42,
                "subnet": "10.0.0.0/24",
                "valid-lifetime": 4000,
                "min-valid-lifetime": 2000,
                "max-valid-lifetime": 8000,
                "renew-timer": 600,
                "rebind-timer": 900,
            },
        )
        # Kea refuses the lease field valid-lft in a Subnet as a spurious parameter.
        self.assertFalse({"valid-lft", "min-valid-lft", "max-valid-lft"} & set(sent))

    def test_a_ddns_suffix_is_set_or_removed(self):
        live = {"id": 42, "subnet": "10.0.0.0/24", "ddns-qualifying-suffix": "old.example.org."}
        self.assertEqual(
            self._sent(live, ddns_qualifying_suffix="new.example.org.")["ddns-qualifying-suffix"], "new.example.org."
        )
        self.assertNotIn("ddns-qualifying-suffix", self._sent(live))

    def test_the_update_sends_the_cidr_text_that_kea_declares(self):
        live = {"id": 7, "subnet": "2001:0DB8:0000:0000::5/64"}
        self.assertEqual(self._sent(live, version=6)["subnet"], "2001:0DB8:0000:0000::5/64")

    def test_the_values_that_the_form_showed_keep_the_options_and_empty_fields_remove_them(self):
        existing = [
            {"code": 3, "data": "198.18.0.1", "never-send": True},
            {"code": 6, "data": "198.18.0.53", "never-send": True},
            {"code": 42, "data": "198.18.0.123", "never-send": True},
        ]
        shown = {"gateway": "198.18.0.1", "dns_servers": ("198.18.0.53",), "ntp_servers": ("198.18.0.123",)}
        self.assertEqual(self._options(existing, **shown), existing)
        self.assertEqual(self._options(existing), [])

    def test_code_only_and_suppression_options_round_trip(self):
        """A DNS option written by code is replaced, not duplicated; a never-send entry survives an empty field."""
        existing = [
            {"code": 6, "data": "10.0.0.53", "always-send": True},
            {"code": 42, "never-send": True},
            {"name": "domain-name-servers", "space": "vendor-4491", "data": "10.0.0.99"},
        ]
        self.assertEqual(
            self._options(existing, dns_servers=("10.0.0.53",)),
            [
                {"name": "domain-name-servers", "space": "vendor-4491", "data": "10.0.0.99"},
                {"code": 6, "data": "10.0.0.53", "always-send": True},
                {"code": 42, "never-send": True},
            ],
        )

    def test_other_family_option_names_remain_unmanaged(self):
        existing = [{"name": "dns-servers", "data": "192.0.2.53"}, {"name": "sntp-servers", "data": "192.0.2.123"}]
        self.assertEqual(
            self._options(existing, dns_servers=("198.18.0.53",)),
            [*existing, {"name": "domain-name-servers", "data": "198.18.0.53"}],
        )

    def test_equivalent_csv_preserves_encoding_flags(self):
        for data in ("198.18.0.53,198.18.0.54", "198.18.0.53, 198.18.0.54"):
            with self.subTest(data=data):
                existing = [{"code": 6, "data": data, "csv-format": True, "never-send": True}]
                self.assertEqual(self._options(existing, dns_servers=("198.18.0.53", "198.18.0.54")), existing)

    def test_class_tagged_entries_are_not_managed_by_the_form(self):
        """Kea allows one entry per class tag; the form edits only the untagged default."""
        tagged = [
            {"code": 6, "data": "10.0.1.53", "client-classes": ["class-a"]},
            {"code": 6, "data": "10.0.2.53", "client-classes": ["class-b"]},
            {"name": "domain-name-servers", "data": "10.0.0.53"},
        ]
        self.assertEqual(
            self._options(tagged, dns_servers=("10.0.0.54",)),
            [*tagged[:2], {"name": "domain-name-servers", "data": "10.0.0.54"}],
        )

    def test_an_empty_field_keeps_a_value_that_the_form_cannot_show(self):
        """The form cannot show binary data or a router list, so an empty field must not delete them."""
        for existing in (
            [{"code": 6, "data": "0A000035", "csv-format": False}],
            [{"code": 3, "data": "10.0.0.1, 10.0.0.2"}],
        ):
            with self.subTest(existing=existing):
                self.assertEqual(self._options(existing), existing)

    def test_a_changed_value_drops_the_old_encoding_flag(self):
        """Form text is CSV, so csv-format false no longer describes the new value."""
        existing = [{"code": 6, "data": "0A000035", "csv-format": False}]
        self.assertEqual(self._options(existing, dns_servers=("10.0.0.53",)), [{"code": 6, "data": "10.0.0.53"}])

    def test_a_dhcpv6_subnet_replaces_its_dns_option_and_keeps_the_others(self):
        existing = [
            {"name": "dns-servers", "data": "2001:4860:4860::8888"},
            {"name": "domain-search", "data": "example.com"},
        ]
        self.assertEqual(
            self._options(existing, version=6, dns_servers=("2001:4860:4860::8844",)),
            [{"name": "domain-search", "data": "example.com"}, {"name": "dns-servers", "data": "2001:4860:4860::8844"}],
        )


class TestKeaClientContextManager(TestCase):
    """KeaClient supports context manager protocol for resource cleanup."""

    def test_close_closes_session(self):
        client = KeaClient(url="http://kea:8000")
        with patch.object(client._session, "close") as mock_close:
            client.close()
            mock_close.assert_called_once()

    def test_context_manager_calls_close(self):
        client = KeaClient(url="http://kea:8000")
        with patch.object(client, "close") as mock_close:
            with client:
                pass
            mock_close.assert_called_once()

    def test_clone_supports_context_manager(self):
        client = KeaClient(url="http://kea:8000")
        with client.clone() as worker:
            self.assertIsInstance(worker, KeaClient)
            self.assertEqual(worker.url, client.url)


class TestConfigGetShapeGuard(TestCase):
    """Methods that call config-get raise KeaException on malformed arguments."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _null_args_response(self):
        return _mock_http_response([{"result": 0, "arguments": None}])

    @patch("requests.Session.post")
    def test_subnet_get_null_arguments_raises_kea_exception(self, mock_post):
        mock_post.return_value = self._null_args_response()
        with self.assertRaises(KeaException):
            self.client.subnet_get(version=4, subnet_id=1)


class TestLeaseGetPage(TestCase):
    """Tests for KeaClient.lease_get_page()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_explicit_cursor_is_sent_without_assuming_backend_order(self):
        first = {"ip-address": "198.18.1.10"}
        second = {"ip-address": "198.18.2.10"}
        with stub_kea(
            {
                "lease4-get-page": {
                    "result": 0,
                    "arguments": {"leases": [first, second], "count": 2},
                }
            }
        ) as kea:
            result = self.client.lease_get_page(
                version=4,
                limit=10,
                cursor="198.18.0.255",
            )

        self.assertEqual(result, LeasePage(leases=[first, second], next_cursor=None))
        self.assertEqual(kea.bodies("lease4-get-page")[0]["arguments"]["from"], "198.18.0.255")

    def test_full_page_returns_last_address_as_next_cursor(self):
        leases = [{"ip-address": "2001:db8::10"}, {"ip-address": "2001:db8::20"}]
        with stub_kea(
            {
                "lease6-get-page": {
                    "result": 0,
                    "arguments": {"leases": leases, "count": 2},
                }
            }
        ):
            result = self.client.lease_get_page(version=6, limit=2)

        self.assertEqual(result, LeasePage(leases=leases, next_cursor="2001:db8::20"))

    def test_rejects_cursor_from_another_address_family_without_request(self):
        with patch("requests.Session.post") as mock_post:
            with self.assertRaisesRegex(ValueError, "IPv6.*DHCPv4"):
                self.client.lease_get_page(
                    version=4,
                    limit=10,
                    cursor="2001:db8::1",
                )

        mock_post.assert_not_called()

    def test_rejects_invalid_page_parameters_without_request(self):
        cases = (
            ((5,), {"limit": 10}, "version must be 4 or 6"),
            ((4,), {"limit": False}, "positive integer"),
            ((4,), {"limit": 10, "cursor": "not-an-address"}, "Invalid DHCPv4 lease cursor"),
        )

        for args, kwargs, message in cases:
            with self.subTest(args=args, kwargs=kwargs), stub_kea({}) as kea:
                with self.assertRaisesRegex(ValueError, message):
                    self.client.lease_get_page(*args, **kwargs)
                self.assertEqual(kea.commands(), [])

    def test_rejects_invalid_addresses_in_a_partial_page(self):
        cases = (
            ("not-an-address", "invalid ip-address"),
            ("2001:db8::1", "wrong address family"),
        )

        for address, message in cases:
            with (
                self.subTest(address=address),
                stub_kea(
                    {
                        "lease4-get-page": {
                            "result": 0,
                            "arguments": {"leases": [{"ip-address": address}], "count": 1},
                        }
                    }
                ),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                self.client.lease_get_page(version=4, limit=2)

    def test_rejects_invalid_final_address_in_a_full_page(self):
        cases = (
            ("not-an-address", "invalid final ip-address"),
            ("2001:db8::1", "final lease for the wrong address family"),
        )

        for address, message in cases:
            with (
                self.subTest(address=address),
                stub_kea(
                    {
                        "lease4-get-page": {
                            "result": 0,
                            "arguments": {"leases": [{"ip-address": address}], "count": 1},
                        }
                    }
                ),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                self.client.lease_get_page(version=4, limit=1)

    def test_rejects_count_that_does_not_match_the_lease_collection(self):
        """Kea defines count as the number of leases in the returned page."""
        with stub_kea(
            {
                "lease4-get-page": {
                    "result": 0,
                    "arguments": {"leases": [{"ip-address": "198.18.0.10"}], "count": 0},
                }
            }
        ):
            with self.assertRaisesRegex(RuntimeError, "count"):
                self.client.lease_get_page(version=4, limit=10)


class TestLeaseGetAllPagination(TestCase):
    """Tests for KeaClient.lease_get_all() pagination and edge-case handling."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _page_response(self, leases, count=None, result=0):
        args = {"leases": leases}
        if count is not None:
            args["count"] = count
        return _mock_http_response([{"result": result, "arguments": args}])

    def _no_leases_response(self):
        """Kea returns result=3 (no more leases)."""
        return _mock_http_response([{"result": 3}])

    def test_rejects_invalid_addresses_in_a_partial_page(self):
        cases = (
            ("not-an-address", "invalid ip-address"),
            ("2001:db8::1", "wrong address family"),
        )

        for address, message in cases:
            with (
                self.subTest(address=address),
                stub_kea(
                    {
                        "lease4-get-page": {
                            "result": 0,
                            "arguments": {"leases": [{"ip-address": address}], "count": 1},
                        }
                    }
                ),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                self.client.lease_get_all(version=4, per_page=2)

    @patch("requests.Session.post")
    def test_empty_page_breaks_loop(self, mock_post):
        """An empty successful page causes the loop to stop."""
        mock_post.side_effect = [
            self._page_response(leases=[], count=0),
        ]
        leases, truncated = self.client.lease_get_all(version=4)
        self.assertEqual(leases, [])
        self.assertFalse(truncated)
        # Only one HTTP call made (broke on empty page)
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.Session.post")
    def test_empty_page_result3_breaks_loop(self, mock_post):
        """result=3 (no more leases) breaks immediately."""
        mock_post.return_value = self._no_leases_response()
        leases, truncated = self.client.lease_get_all(version=4)
        self.assertEqual(leases, [])
        self.assertFalse(truncated)

    @patch("requests.Session.post")
    def test_malformed_cursor_non_dict_last_item_raises(self, mock_post):
        """Last item in page is not a dict → RuntimeError with 'ip-address' cursor message."""
        # One full page (count==per_page=1) so cursor advancement is attempted
        mock_post.return_value = self._page_response(leases=["not-a-dict"], count=1)
        with self.assertRaises(RuntimeError) as cm:
            self.client.lease_get_all(version=4, per_page=1)
        self.assertIn("ip-address", str(cm.exception))

    @patch("requests.Session.post")
    def test_malformed_cursor_missing_ip_address_raises(self, mock_post):
        """Last item has no 'ip-address' key → RuntimeError."""
        # Page with 1 item missing 'ip-address', count==per_page so loop continues
        mock_post.return_value = self._page_response(leases=[{"hw-address": "aa:bb:cc:dd:ee:ff"}], count=1)
        with self.assertRaises(RuntimeError) as cm:
            self.client.lease_get_all(version=4, per_page=1)
        self.assertIn("ip-address", str(cm.exception))

    @patch("requests.Session.post")
    def test_max_leases_truncates_and_returns_flag(self, mock_post):
        """Exceeding max_leases truncates result and sets truncated=True."""
        leases = [{"ip-address": f"10.0.0.{i}"} for i in range(5)]
        # count < per_page so it's the last page — no cursor advancement needed
        mock_post.return_value = self._page_response(leases=leases, count=5)
        result, truncated = self.client.lease_get_all(version=4, per_page=10, max_leases=3)
        self.assertEqual(len(result), 3)
        self.assertTrue(truncated)

    @patch("requests.Session.post")
    def test_exact_max_leases_on_final_page_is_not_truncated(self, mock_post):
        """An exact result limit is complete when Kea marks the page as final."""
        leases = [{"ip-address": f"10.0.0.{i}"} for i in range(1, 4)]
        mock_post.return_value = self._page_response(leases=leases, count=3)

        collection = self.client.lease_get_all(version=4, per_page=10, max_leases=3)

        self.assertEqual(collection, LeaseCollection(leases=leases, truncated=False))

    @patch("requests.Session.post")
    def test_exact_max_leases_on_full_final_page_is_not_truncated(self, mock_post):
        """An exact full-page limit probes for overflow before reporting truncation."""
        leases = [{"ip-address": "10.0.0.1"}, {"ip-address": "10.0.0.2"}]
        mock_post.side_effect = [
            self._page_response(leases=leases, count=2),
            self._no_leases_response(),
        ]

        collection = self.client.lease_get_all(version=4, per_page=2, max_leases=2)

        self.assertEqual(collection, LeaseCollection(leases=leases, truncated=False))
        self.assertEqual(mock_post.call_count, 2)

    @patch("requests.Session.post")
    def test_exact_max_leases_on_full_page_reports_confirmed_overflow(self, mock_post):
        """A probe that finds another lease confirms the collection is truncated."""
        leases = [{"ip-address": "10.0.0.1"}, {"ip-address": "10.0.0.2"}]
        mock_post.side_effect = [
            self._page_response(leases=leases, count=2),
            self._page_response(leases=[{"ip-address": "10.0.0.3"}], count=1),
        ]

        collection = self.client.lease_get_all(version=4, per_page=2, max_leases=2)

        self.assertEqual(collection, LeaseCollection(leases=leases, truncated=True))
        probe_payload = mock_post.call_args_list[1].kwargs["json"]
        self.assertEqual(probe_payload["arguments"], {"from": "10.0.0.2", "limit": 1})

    @patch("requests.Session.post")
    def test_multi_page_aggregates_leases(self, mock_post):
        """Two pages of leases are combined, and the cursor advances to the last IP on page 1."""
        page1 = [{"ip-address": "10.0.0.1"}, {"ip-address": "10.0.0.2"}]
        page2 = [{"ip-address": "10.0.0.3"}]
        mock_post.side_effect = [
            self._page_response(leases=page1, count=2),  # full page → advance cursor
            self._page_response(leases=page2, count=1),  # partial page → stop
        ]
        leases, truncated = self.client.lease_get_all(version=4, per_page=2)
        self.assertEqual(len(leases), 3)
        self.assertFalse(truncated)
        # Verify the second request used the last IP of page 1 as cursor
        first_payload = mock_post.call_args_list[0].kwargs["json"]
        second_payload = mock_post.call_args_list[1].kwargs["json"]
        self.assertEqual(first_payload["arguments"]["from"], "0.0.0.0")  # noqa: S104 - Kea sentinel value, not a bind address
        self.assertEqual(second_payload["arguments"]["from"], "10.0.0.2")

    def test_rejects_a_cursor_that_does_not_advance(self):
        page = {
            "result": 0,
            "arguments": {"leases": [{"ip-address": "198.18.0.10"}], "count": 1},
        }

        with stub_kea({"lease4-get-page": page}) as kea:
            with self.assertRaisesRegex(RuntimeError, "Lease page cursor did not advance"):
                self.client.lease_get_all(version=4, per_page=1)

        self.assertEqual(len(kea.bodies("lease4-get-page")), 2)

    def test_per_page_zero_raises_value_error(self):
        """per_page < 1 → ValueError before any HTTP call is made."""
        with self.assertRaises(ValueError) as cm:
            self.client.lease_get_all(version=4, per_page=0)
        self.assertIn("per_page", str(cm.exception))

    def test_per_page_negative_raises_value_error(self):
        """per_page < 1 → ValueError before any HTTP call is made."""
        with self.assertRaises(ValueError) as cm:
            self.client.lease_get_all(version=4, per_page=-1)
        self.assertIn("per_page", str(cm.exception))

    def test_max_leases_zero_raises_value_error(self):
        """max_leases=0 → ValueError before any HTTP call is made."""
        with self.assertRaises(ValueError) as cm:
            self.client.lease_get_all(version=4, max_leases=0)
        self.assertIn("max_leases", str(cm.exception))

    def test_max_leases_negative_raises_value_error(self):
        """max_leases < 0 → ValueError before any HTTP call is made."""
        with self.assertRaises(ValueError) as cm:
            self.client.lease_get_all(version=4, max_leases=-1)
        self.assertIn("max_leases", str(cm.exception))

    @patch("requests.Session.post")
    def test_non_int_count_raises_runtime_error(self, mock_post):
        """Non-int count in response → RuntimeError instead of silently stopping early."""
        page = [{"ip-address": "10.0.0.1"}, {"ip-address": "10.0.0.2"}]
        # count is a string instead of int — should raise, not silently break
        mock_post.return_value = self._page_response(leases=page, count="2")
        with self.assertRaises(RuntimeError) as cm:
            self.client.lease_get_all(version=4, per_page=2)
        self.assertIn("count", str(cm.exception))

    @patch("requests.Session.post")
    def test_empty_response_list_raises_runtime_error(self, mock_post):
        """Kea returns [] (empty list) → RuntimeError with useful context, not IndexError."""
        mock_post.return_value = _mock_http_response([])
        with self.assertRaises(RuntimeError) as cm:
            self.client.lease_get_all(version=4)
        self.assertIn("lease4-get-page", str(cm.exception))


class TestPersistConfigFlag(TestCase):
    """Tests that persist_config=False skips the persist step."""

    def test_default_persist_config_is_true(self):
        """KeaClient defaults to persist_config=True."""
        client = KeaClient(url="http://kea:8000")
        self.assertTrue(client.persist_config)

    def test_persist_config_false_stored(self):
        """KeaClient stores persist_config=False when passed."""
        client = KeaClient(url="http://kea:8000", persist_config=False)
        self.assertFalse(client.persist_config)

    def test_clone_propagates_persist_config_false(self):
        """clone() copies persist_config=False to the new instance."""
        client = KeaClient(url="http://kea:8000", persist_config=False)
        cloned = client.clone()
        self.assertFalse(cloned.persist_config)

    def test_clone_propagates_persist_config_true(self):
        """clone() copies persist_config=True (default) to the new instance."""
        client = KeaClient(url="http://kea:8000", persist_config=True)
        cloned = client.clone()
        self.assertTrue(cloned.persist_config)

    def test_persist_config_false_sends_no_persist_step(self):
        """persist() sends nothing when persist_config=False."""
        client = KeaClient(url="http://kea:8000", persist_config=False)
        with stub_kea({}) as kea:
            self.assertEqual(client.persist(4).persistence, "not-requested")
        self.assertEqual(kea.commands(), [])


# ---------------------------------------------------------------------------
# Coverage-gap tests — lines not yet exercised by the suite above
# ---------------------------------------------------------------------------


class TestGetAvailableCommandsMalformed(TestCase):
    """get_available_commands raises RuntimeError on empty / non-dict response."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_empty_list_raises_runtime_error(self):
        """Empty response list hits the 'not resp' branch and raises RuntimeError."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response([])):
            with self.assertRaises(RuntimeError):
                self.client.get_available_commands("dhcp4")


class TestLeaseUpdateGuards(TestCase):
    """lease_update guards on result=3 and non-dict arguments."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_result3_raises_kea_exception(self):
        """command() returning result=3 directly (bypassing check_response) raises KeaException."""
        # check_response would normally raise for result=3; patch command() to bypass it
        # and test the explicit result=3 guard in lease_update.
        with patch.object(
            self.client,
            "command",
            return_value=[{"result": 3, "text": "Lease not found."}],
        ):
            with self.assertRaises(KeaException):
                self.client.lease_update(version=4, ip_address="10.0.0.1")

    def test_non_dict_arguments_raises_value_error(self):
        """result=0 with non-dict arguments raises ValueError."""
        resp = [{"result": 0, "arguments": None}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)):
            with self.assertRaises(ValueError):
                self.client.lease_update(version=4, ip_address="10.0.0.1")


class TestLeaseGetByIpNonDictArguments(TestCase):
    """lease_get_by_ip uses the canonical lease-search response validation."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_non_dict_arguments_raises_runtime_error(self):
        """A result with null arguments is a malformed Kea response."""
        resp = [{"result": 0, "arguments": None}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)):
            with self.assertRaises(RuntimeError):
                self.client.lease_get_by_ip(version=4, ip_address="10.0.0.1")


class TestLeaseGetAllMalformedArguments(TestCase):
    """lease_get_all raises RuntimeError when arguments is not a dict or leases is not a list."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    @patch("requests.Session.post")
    def test_arguments_not_dict_raises_runtime_error(self, mock_post):
        """result=0 with non-dict arguments raises RuntimeError."""
        mock_post.return_value = _mock_http_response([{"result": 0, "arguments": "unexpected"}])
        with self.assertRaises(RuntimeError) as cm:
            self.client.lease_get_all(version=4)
        self.assertIn("arguments", str(cm.exception))

    @patch("requests.Session.post")
    def test_leases_not_list_raises_runtime_error(self, mock_post):
        """result=0, arguments is dict, but leases value is not a list raises RuntimeError."""
        mock_post.return_value = _mock_http_response([{"result": 0, "arguments": {"leases": "bad"}}])
        with self.assertRaises(RuntimeError) as cm:
            self.client.lease_get_all(version=4)
        self.assertIn("leases", str(cm.exception))


class TestConfigPhaseCommand(TestCase):
    def test_config_set_requires_an_explicit_configuration(self):
        with stub_kea({}) as kea, self.assertRaises(ValueError):
            KeaClient(url="http://kea:8000")._config_phase_command("config-set", "dhcp4")
        self.assertEqual(kea.commands(), [])
