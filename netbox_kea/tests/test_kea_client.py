# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2026 Andrew Backeby <andrew@backeby.eu>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.kea — KeaClient, KeaException, check_response.

These tests mock all HTTP calls and require no running services.
"""

import dataclasses
import ipaddress
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import get_args, get_origin, get_type_hints
from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests

from netbox_kea.constants import MAX_SUBNET_ID
from netbox_kea.kea import (
    KeaClient,
    KeaCommand,
    KeaException,
    KeaResponse,
    KeaTLSFileError,
    LeaseQueryGuardError,
    LeaseQueryNotMeasurable,
    LeaseQueryPreflightUnavailable,
    LeaseQueryTooBroad,
    MalformedConfiguration,
    SubnetEdit,
    SubnetFields,
    check_response,
    lease_query_guard_message,
)
from netbox_kea.leases import (
    AllocationKind,
    DHCPv4LeaseRequest,
    DHCPv6LeaseRequest,
    LeaseAbsent,
    LeaseChanged,
    LeaseConflict,
    LeaseEdit,
    LeaseFound,
    LeaseIdentity,
    LeaseLookupFailed,
    LeaseSnapshot,
    MalformedLeaseResponse,
    _creation_arguments,
    shown_lease,
)
from netbox_kea.tests.kea_stub import (
    LeaseDaemon,
    _http_response,
    _raw_http_response,
    _subnet_stats,
    complete_lease,
    kea_client,
    lease_page,
    lease_pages,
    lease_record,
    queued,
    record_transport,
    stub_kea,
    typed_lease,
)


def _typed(*records):
    return tuple(typed_lease(record) for record in records)


def _identity(address: str, kind: AllocationKind = "address") -> LeaseIdentity:
    parsed = ipaddress.ip_address(address)
    return LeaseIdentity(family=parsed.version, kind=kind, address=parsed)


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
        client = kea_client(url="http://kea:8000")
        self.assertEqual(client.url, "http://kea:8000")

    def test_default_timeout(self):
        client = kea_client(url="http://kea:8000")
        self.assertEqual(client.timeout, 30)

    def test_custom_timeout(self):
        client = kea_client(url="http://kea:8000", timeout=10)
        self.assertEqual(client.timeout, 10)

    def test_cert_without_key_raises(self):
        with self.assertRaises(ValueError):
            kea_client(url="http://kea:8000", client_cert="/cert.pem")

    def test_key_without_cert_raises(self):
        with self.assertRaises(ValueError):
            kea_client(url="http://kea:8000", client_key="/key.pem")

    def test_basic_auth_configured(self):
        client = kea_client(url="http://kea:8000", username="admin", password="secret")
        self.assertIsNotNone(client._session.auth)

    def test_no_auth_when_username_only(self):
        # Partial auth — no password means no auth header set
        client = kea_client(url="http://kea:8000", username="admin")
        self.assertIsNone(client._session.auth)

    def test_clone_copies_url_and_timeout(self):
        """clone() produces a new KeaClient with the same url and timeout."""
        client = kea_client(url="http://kea:8000", timeout=15)
        cloned = client.clone()
        self.assertEqual(cloned.url, "http://kea:8000")
        self.assertEqual(cloned.timeout, 15)

    def test_clone_has_independent_session(self):
        """clone() creates a new requests.Session, not a reference to the original."""
        client = kea_client(url="http://kea:8000")
        cloned = client.clone()
        self.assertIsNot(cloned._session, client._session)

    def test_clone_copies_session_auth(self):
        """clone() copies auth credentials from the original session."""
        client = kea_client(url="http://kea:8000", username="admin", password="secret")
        cloned = client.clone()
        self.assertEqual(cloned._session.auth, client._session.auth)

    def test_clone_preserves_send_service(self):
        """clone() carries send_service so a cloned worker-thread client stays direct."""
        self.assertFalse(kea_client(url="http://kea:8000", send_service=False).clone().send_service)
        self.assertTrue(kea_client(url="http://kea:8000").clone().send_service)


_ENV_CA_BUNDLE = "/env/bundle.pem"
_VERSION_REPLY = [{"result": 0, "text": "3.0.0", "arguments": {"extended": "3.0.0"}}]


class TestKeaClientTLSSettings(TestCase):
    """The TLS settings of the client reach the transport, also when the environment names a CA bundle."""

    def _sent(self, client: KeaClient) -> dict:
        """Send version-get through the real session pipeline and return the TLS settings that reached the transport."""
        transport = record_transport(client, _VERSION_REPLY)
        with patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": _ENV_CA_BUNDLE}):
            client.command(KeaCommand.VERSION_GET, None)
        return transport.sent[-1]

    def test_a_ca_file_wins_over_the_environment_bundle(self):
        sent = self._sent(kea_client(url="https://kea:8000", verify="/etc/ssl/ca.pem"))
        self.assertEqual(sent["verify"], "/etc/ssl/ca.pem")

    def test_disabled_verification_stays_disabled_with_an_environment_bundle(self):
        sent = self._sent(kea_client(url="https://kea:8000", verify=False))
        self.assertIs(sent["verify"], False)

    def test_the_client_certificate_pair_reaches_the_transport(self):
        sent = self._sent(kea_client(url="https://kea:8000", client_cert="/cert.pem", client_key="/key.pem"))
        self.assertEqual(sent["cert"], ("/cert.pem", "/key.pem"))

    def test_default_verification_uses_the_environment_bundle(self):
        """Plain verification keeps the trust store of the operator."""
        for verify in (None, True):
            with self.subTest(verify=verify):
                sent = self._sent(kea_client(url="https://kea:8000", verify=verify))
                self.assertEqual(sent["verify"], _ENV_CA_BUNDLE)
                self.assertIsNone(sent["cert"])

    def test_a_clone_sends_the_same_tls_settings(self):
        client = kea_client(
            url="https://kea:8000", verify="/etc/ssl/ca.pem", client_cert="/cert.pem", client_key="/key.pem"
        )
        sent = self._sent(client.clone())
        self.assertEqual(sent, {"verify": "/etc/ssl/ca.pem", "cert": ("/cert.pem", "/key.pem")})


class TestKeaClientTLSFileError(TestCase):
    """A TLS file that requests cannot find is a request error of the client."""

    def _error(self, client: KeaClient) -> KeaTLSFileError:
        with self.assertRaises(KeaTLSFileError) as caught:
            client.command(KeaCommand.VERSION_GET, None)
        return caught.exception

    def test_a_missing_ca_file_is_a_request_error(self):
        error = self._error(kea_client(url="https://127.0.0.1:9/", verify="/nonexistent/ca.pem"))
        self.assertIsInstance(error, requests.RequestException)
        self.assertIs(type(error.__cause__), OSError)
        self.assertIn("/nonexistent/ca.pem", str(error.__cause__))

    def test_a_missing_client_certificate_is_a_request_error(self):
        client = kea_client(
            url="https://127.0.0.1:9/", verify=False, client_cert="/nonexistent/client.pem", client_key="/key.pem"
        )
        error = self._error(client)
        self.assertIsInstance(error, requests.RequestException)
        self.assertIs(type(error.__cause__), OSError)
        self.assertIn("/nonexistent/client.pem", str(error.__cause__))

    def test_a_request_error_passes_unchanged(self):
        refused = requests.ConnectionError("connection refused")
        with stub_kea({"version-get": refused}), self.assertRaises(requests.ConnectionError) as caught:
            kea_client(url="https://kea:8000").command(KeaCommand.VERSION_GET, None)
        self.assertIs(caught.exception, refused)


class TestKeaClientCommand(TestCase):
    """Tests for KeaClient.command()."""

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")

    def _patched_post(self, json_data):
        """Patch session.post to return *json_data*."""
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_command_returns_response_list(self):
        resp = [{"result": 0, "arguments": {"leases": []}, "text": "ok"}]
        with self._patched_post(resp):
            result = self.client.command(KeaCommand.LEASE4_GET_ALL, 4)
        self.assertEqual(result, resp)

    def test_command_sends_correct_body(self):
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.command(KeaCommand.STATUS_GET, 4, arguments={"extra": 1})

        call_kwargs = mock_post.call_args
        sent_json = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        self.assertEqual(sent_json["command"], "status-get")
        self.assertEqual(sent_json["service"], ["dhcp4"])
        self.assertEqual(sent_json["arguments"], {"extra": 1})

    def test_command_omits_service_when_none(self):
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.command(KeaCommand.LIST_COMMANDS, None)
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertNotIn("service", sent_json)

    def test_command_omits_service_when_send_service_false(self):
        """A direct-daemon client (send_service=False) drops a supplied service from the body."""
        client = kea_client(url="http://kea-daemon:8000", send_service=False)
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            client.command(KeaCommand.LEASE4_GET, 4)
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertNotIn("service", sent_json)

    def test_command_omits_arguments_when_none(self):
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.command(KeaCommand.LIST_COMMANDS, None)
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertNotIn("arguments", sent_json)

    def test_command_raises_kea_exception_on_a_failure_result(self):
        resp = [{"result": 1, "text": "unknown command"}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException):
                self.client.command(KeaCommand.LIST_COMMANDS, None)

    def test_the_kea_exception_carries_the_failing_reply(self):
        resp = [{"result": 2, "text": "not found"}]
        with self._patched_post(resp):
            try:
                self.client.command(KeaCommand.VERSION_GET, None)
                self.fail("Expected KeaException")
            except KeaException as exc:
                self.assertEqual(exc.response["result"], 2)

    def test_command_check_none_skips_validation(self):
        resp = [{"result": 1, "text": "error but accepted"}]
        with self._patched_post(resp):
            result = self.client.command(KeaCommand.VERSION_GET, None, check=None)
        self.assertEqual(result, resp)

    def test_command_custom_ok_codes(self):
        resp = [{"result": 3, "text": "empty"}]
        with self._patched_post(resp):
            result = self.client.command(KeaCommand.LEASE4_GET, 4, check=(0, 3))
        self.assertEqual(result, resp)

    def test_command_http_error_raises(self):
        mock_resp = _mock_http_response({}, status_code=500)
        with patch.object(self.client._session, "post", return_value=mock_resp):
            with self.assertRaises(requests.HTTPError):
                self.client.command(KeaCommand.VERSION_GET, None)

    def test_a_malformed_reply_body_raises_runtime_error_without_the_body(self):
        cases = {
            "not a list": _http_response({"result": 0, "text": "private body"}),
            "not JSON": _raw_http_response(b"<html>private body</html>"),
        }
        for name, reply in cases.items():
            with self.subTest(name), stub_kea({"version-get": reply}):
                with self.assertRaises(RuntimeError) as ctx:
                    self.client.command(KeaCommand.VERSION_GET, None)
                self.assertNotIn("private", str(ctx.exception))

    def test_command_uses_timeout(self):
        resp = [{"result": 0, "text": "ok"}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.command(KeaCommand.LIST_COMMANDS, None)
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs.get("timeout"), 30)

    def test_command_returns_every_reply_of_a_multi_entry_response(self):
        resp = [{"result": 0, "text": "ok"}, {"result": 0, "text": "ok"}]
        with self._patched_post(resp):
            result = self.client.command(KeaCommand.STATUS_GET, None)
        self.assertEqual(len(result), 2)

    def test_command_raises_on_second_failed_response(self):
        resp = [{"result": 0, "text": "ok"}, {"result": 1, "text": "failed"}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException) as ctx:
                self.client.command(KeaCommand.STATUS_GET, None)
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
    """Tests for KeaClient.get_available_commands(family) -> set[str]."""

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")

    def _patched_post(self, json_data):
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_returns_set_of_command_names(self):
        resp = [{"result": 0, "arguments": ["reservation-add", "reservation-get-page", "reservation-del"]}]
        with self._patched_post(resp):
            result = self.client.get_available_commands(4)
        self.assertIsInstance(result, set)
        self.assertIn("reservation-add", result)
        self.assertIn("reservation-get-page", result)
        self.assertIn("reservation-del", result)

    def test_handles_empty_arguments(self):
        resp = [{"result": 0, "arguments": []}]
        with self._patched_post(resp):
            result = self.client.get_available_commands(4)
        self.assertEqual(result, set())

    def test_sends_list_commands_to_correct_service(self):
        resp = [{"result": 0, "arguments": ["reservation-add"]}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.get_available_commands(4)
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["command"], "list-commands")
        self.assertEqual(sent_json["service"], ["dhcp4"])

    def test_works_for_dhcp6_service(self):
        resp = [{"result": 0, "arguments": ["reservation-add", "reservation-get-page"]}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            result = self.client.get_available_commands(6)
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
        self.client = kea_client(url="http://kea:8000")

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
        self.client = kea_client(url="http://kea:8000")

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
        self.client = kea_client(url="http://kea:8000")

    def _payload(self, mock_post):
        return mock_post.call_args_list[0].kwargs.get("json") or mock_post.call_args_list[0][1]["json"]

    def test_dhcp_disable_sends_correct_command(self):
        """dhcp-disable command is sent to the correct service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ) as mock_post:
            self.client.dhcp_disable(4)
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
            self.client.dhcp_disable(4)
        payload = self._payload(mock_post)
        self.assertNotIn("arguments", payload)

    def test_dhcp_disable_with_max_period_includes_arguments(self):
        """When max_period is given, arguments contains max-period."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ) as mock_post:
            self.client.dhcp_disable(4, max_period=300)
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
                self.client.dhcp_disable(4)

    def test_dhcp_disable_works_for_dhcp6(self):
        """dhcp-disable can target the dhcp6 service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ) as mock_post:
            self.client.dhcp_disable(6)
        payload = self._payload(mock_post)
        self.assertEqual(payload["service"], ["dhcp6"])

    def test_dhcp_disable_returns_none_on_success(self):
        """dhcp_disable returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_DISABLE_RESP),
        ):
            result = self.client.dhcp_disable(4)
        self.assertIsNone(result)


class TestDHCPEnable(TestCase):
    """Tests for KeaClient.dhcp_enable()."""

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")

    def _payload(self, mock_post):
        return mock_post.call_args_list[0].kwargs.get("json") or mock_post.call_args_list[0][1]["json"]

    def test_dhcp_enable_sends_correct_command(self):
        """dhcp-enable command is sent to the correct service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_ENABLE_RESP),
        ) as mock_post:
            self.client.dhcp_enable(4)
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
            self.client.dhcp_enable(4)
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
                self.client.dhcp_enable(4)

    def test_dhcp_enable_works_for_dhcp6(self):
        """dhcp-enable can target the dhcp6 service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_ENABLE_RESP),
        ) as mock_post:
            self.client.dhcp_enable(6)
        payload = self._payload(mock_post)
        self.assertEqual(payload["service"], ["dhcp6"])

    def test_dhcp_enable_returns_none_on_success(self):
        """dhcp_enable returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_DHCP_ENABLE_RESP),
        ):
            result = self.client.dhcp_enable(4)
        self.assertIsNone(result)


# TestLeaseChanges
# ---------------------------------------------------------------------------


def _recorded_changes(family: int) -> dict:
    return json.loads((Path(__file__).with_name("kea_recordings") / f"dhcp{family}.json").read_text())["leases"][
        "changes"
    ]


class TestLeaseChanges(TestCase):
    """Edit and delete read the shown Lease again and change only a Lease that agrees with the shown facts.

    The Kea replies are the ones recorded from a real Kea 3.2.0 (the ``changes`` section of the recordings).
    """

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")
        self.prefix = _recorded_changes(6)["before"]["arguments"]
        self.shown = shown_lease(typed_lease(self.prefix))

    def test_an_edit_sends_the_fresh_body_with_only_the_written_field_changed(self):
        daemon = LeaseDaemon(6, self.prefix)
        with stub_kea(daemon.responses()) as kea:
            result = self.client.lease_update(self.shown, LeaseEdit(hostname="renamed.example.org"))

        self.assertEqual(result, LeaseChanged(lease=typed_lease(self.prefix)))
        self.assertEqual(kea.commands(), ["lease6-get", "lease6-update"])
        self.assertEqual(
            kea.bodies("lease6-get")[0]["arguments"], {"ip-address": "2001:db8:100:600::", "type": "IA_PD"}
        )
        expire = self.prefix["cltt"] + self.prefix["valid-lft"]
        sent = kea.bodies("lease6-update")[0]["arguments"]
        self.assertEqual(sent, {**self.prefix, "hostname": "renamed.example.org", "expire": expire})
        self.assertNotIn("force-create", sent)
        # Kea 3.2.0 kept the prefix kind, its length and the nested user-context on the same update.
        after = _recorded_changes(6)["after-update"]["arguments"]
        self.assertEqual({**after, "hostname": self.prefix["hostname"]}, self.prefix)

    def test_a_fresh_read_that_contradicts_the_shown_facts_sends_no_change(self):
        cases = {
            ("binding",): {"iaid": 31},
            ("subnet_id",): {"subnet-id": 11},
            ("prefix_length",): {"prefix-len": 60},
            ("hostname",): {"hostname": "changed.example.org"},
        }
        for fields, change in cases.items():
            with self.subTest(fields=fields):
                with stub_kea(LeaseDaemon(6, {**self.prefix, **change}).responses()) as kea:
                    result = self.client.lease_update(self.shown, LeaseEdit(hostname="renamed.example.org"))
                self.assertEqual(result, LeaseConflict(fields=fields))
                self.assertEqual(kea.commands(), ["lease6-get"])

    def test_a_lease_that_is_gone_is_not_created_again(self):
        with stub_kea(LeaseDaemon(6).responses()) as kea:
            update = self.client.lease_update(self.shown, LeaseEdit(hostname="renamed.example.org"))
            delete = self.client.lease_delete(self.shown)

        self.assertEqual(update, LeaseAbsent(identity=self.shown.identity))
        self.assertEqual(delete, LeaseAbsent(identity=self.shown.identity))
        self.assertEqual(kea.commands(), ["lease6-get", "lease6-get"])

    def test_an_update_that_kea_refuses_as_changed_after_the_read_is_a_conflict(self):
        daemon = LeaseDaemon(6, self.prefix)
        daemon.before("lease6-update", lambda held: held.leases.clear())
        with stub_kea(daemon.responses()) as kea:
            result = self.client.lease_update(self.shown, LeaseEdit(hostname="renamed.example.org"))

        # Kea 3.2.0 answers result 4, and the update did not create the lease again.
        self.assertEqual(_recorded_changes(6)["update-absent"]["result"], 4)
        self.assertEqual(result, LeaseConflict(fields=()))
        self.assertEqual(kea.commands(), ["lease6-get", "lease6-update"])
        self.assertEqual(daemon.leases, {})

    def test_a_malformed_fresh_read_sends_no_change(self):
        malformed = {"result": 0, "text": "IPv6 lease found.", "arguments": {**self.prefix, "state": "assigned"}}
        with stub_kea({"lease6-get": malformed}) as kea:
            result = self.client.lease_delete(self.shown)

        self.assertIsInstance(result, LeaseLookupFailed)
        self.assertEqual(kea.commands(), ["lease6-get"])

    def test_an_edit_that_writes_nothing_is_refused_before_kea(self):
        with stub_kea({}) as kea, self.assertRaises(ValueError):
            self.client.lease_update(self.shown, LeaseEdit())
        self.assertEqual(kea.commands(), [])

    def test_a_delete_sends_the_kind_of_the_shown_lease(self):
        daemon = LeaseDaemon(6, self.prefix)
        with stub_kea(daemon.responses()) as kea:
            result = self.client.lease_delete(self.shown)

        self.assertEqual(result, LeaseChanged(lease=typed_lease(self.prefix)))
        self.assertEqual(
            kea.bodies("lease6-del")[0]["arguments"], {"ip-address": "2001:db8:100:600::", "type": "IA_PD"}
        )
        self.assertEqual(daemon.leases, {})
        # Kea 3.2.0 deletes a delegated prefix only with its type.
        self.assertEqual(_recorded_changes(6)["delete-without-type"]["result"], 3)

    def test_a_delete_that_finds_the_lease_gone_after_the_read_is_absent(self):
        daemon = LeaseDaemon(4, _recorded_changes(4)["before"]["arguments"])
        shown = shown_lease(typed_lease(_recorded_changes(4)["before"]["arguments"]))
        daemon.before("lease4-del", lambda held: held.leases.clear())
        with stub_kea(daemon.responses()) as kea:
            result = self.client.lease_delete(shown)

        self.assertEqual(result, LeaseAbsent(identity=shown.identity))
        self.assertEqual(kea.commands(), ["lease4-get", "lease4-del"])

    def test_a_creation_sends_only_the_facts_of_the_request(self):
        requests_by_family = {
            4: DHCPv4LeaseRequest(address=ipaddress.IPv4Address("192.0.2.50"), hw_address="aa:bb:cc:00:00:50"),
            6: DHCPv6LeaseRequest(address=ipaddress.IPv6Address("2001:db8:1::50"), duid="00:01:02:03", iaid=5),
        }
        for family, creation in requests_by_family.items():
            with self.subTest(family=family):
                daemon = LeaseDaemon(family)
                with stub_kea(daemon.responses()) as kea:
                    self.assertIsNone(self.client.lease_add(creation))
                self.assertEqual(kea.commands(), [f"lease{family}-add"])
                self.assertEqual(kea.bodies(f"lease{family}-add")[0]["arguments"], _creation_arguments(creation))

    def test_a_refused_creation_raises(self):
        creation = DHCPv4LeaseRequest(address=ipaddress.IPv4Address("192.0.2.50"), hw_address="aa:bb:cc:00:00:50")
        with stub_kea({"lease4-add": {"result": 1, "text": "address already in use"}}), self.assertRaises(KeaException):
            self.client.lease_add(creation)


# ---------------------------------------------------------------------------
# TestNetworkSubnetAdd
# ---------------------------------------------------------------------------

_NETWORK_SUBNET_ADD_OK = {"result": 0, "text": "Subnet added to shared network."}
_NETWORK_SUBNET_ADD_FAIL = {"result": 1, "text": "subnet not found"}


class TestNetworkSubnetAdd(TestCase):
    """Tests for KeaClient.network_subnet_add(version, name, subnet_id) -> None."""

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")

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
        self.client = kea_client(url="http://kea:8000")

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


class TestConfigChangeNotification(TestCase):
    """A failed cache invalidation does not stop a live configuration change or its persist step."""

    def test_a_failing_invalidation_is_logged_and_the_change_is_still_sent_and_persisted(self) -> None:
        invalidations: list[str] = []

        def fail() -> None:
            invalidations.append("invalidated")
            raise ConnectionError("cache unreachable")

        client = kea_client(url="http://kea:8000", on_config_change=fail)
        responses = {
            "network4-add": _OK,
            "config-get": {"result": 0, "arguments": {"Dhcp4": {}}},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        with stub_kea(responses) as kea, self.assertLogs("netbox_kea.kea", level="ERROR") as logs:
            client.network_add(4, "prod-net")
            persisted = client.persist(4)

        self.assertEqual(kea.commands(), ["network4-add", "config-get", "config-test", "config-write"])
        self.assertEqual(persisted.persistence, "persisted")
        self.assertEqual(invalidations, ["invalidated", "invalidated"])
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            ["Configuration changed for DHCPv4, but cache invalidation failed"] * 2,
        )
        self.assertTrue(all(record.exc_info for record in logs.records))


# ---------------------------------------------------------------------------
# TestLeaseGet
# ---------------------------------------------------------------------------


class TestLeaseGet(TestCase):
    """KeaClient.lease_get() reads one exact Lease: found, confirmed absent, or a failed observation."""

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")

    def test_found_lease_is_typed_and_the_request_names_the_family(self):
        for address in ("192.168.1.10", "2001:db8::1"):
            family = ipaddress.ip_address(address).version
            record = lease_record(address)
            with (
                self.subTest(family=family),
                stub_kea({f"lease{family}-get": {"result": 0, "arguments": record}}) as kea,
            ):
                result = self.client.lease_get(_identity(address))

                self.assertEqual(result, LeaseFound(lease=typed_lease(record)))
                body = kea.bodies(f"lease{family}-get")[0]
                self.assertEqual((body["service"], body["arguments"]), ([f"dhcp{family}"], {"ip-address": address}))

    def test_a_delegated_prefix_lookup_sends_its_kind(self):
        record = lease_record("2001:db8:100:100::", type="IA_PD", prefix_len=56)
        with stub_kea({"lease6-get": {"result": 0, "arguments": record}}) as kea:
            result = self.client.lease_get(_identity("2001:db8:100:100::", "delegated-prefix"))

        self.assertEqual(result, LeaseFound(lease=typed_lease(record)))
        self.assertEqual(
            kea.bodies("lease6-get")[0]["arguments"], {"ip-address": "2001:db8:100:100::", "type": "IA_PD"}
        )

    def test_not_found_is_confirmed_absence(self):
        with stub_kea({"lease4-get": {"result": 3, "text": "Lease not found."}}):
            result = self.client.lease_get(_identity("192.168.1.99"))

        self.assertEqual(result, LeaseAbsent(identity=_identity("192.168.1.99")))

    def test_a_malformed_record_is_a_failed_observation_not_absence(self):
        record = lease_record("192.168.1.10", drop=("state",))
        with stub_kea({"lease4-get": {"result": 0, "arguments": record}}):
            result = self.client.lease_get(_identity("192.168.1.10"))

        self.assertIsInstance(result, LeaseLookupFailed)
        self.assertEqual([(d.code, d.field) for d in result.diagnostics], [("missing-field", "state")])

    def test_an_unusable_reply_or_a_kea_error_fails_the_read(self):
        cases = (
            ({"result": 0, "arguments": None}, MalformedLeaseResponse),
            ({"result": 1, "text": "Internal server error"}, KeaException),
        )
        for reply, error in cases:
            with self.subTest(error=error.__name__), stub_kea({"lease4-get": reply}), self.assertRaises(error):
                self.client.lease_get(_identity("10.0.0.1"))


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
        self.client = kea_client(url="http://kea:8000")

    @patch("requests.Session.post")
    def test_rejects_selector_that_the_address_family_does_not_support(self, mock_post):
        with self.assertRaisesRegex(ValueError, "duid.*DHCPv4"):
            self.client.lease_search(version=4, selector="duid", value="01:02:03:04", server_id=1)

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
            (4, "subnet_id", "\u0661\u0662", None, "positive integer"),
            (4, "subnet_id", " 12", None, "positive integer"),
            (4, "subnet_id", 0, None, "from 1 to 4294967294"),
            (6, "subnet_id", MAX_SUBNET_ID + 1, None, "from 1 to 4294967294"),
            (4, "subnet_id", "99999999999999999999", None, "from 1 to 4294967294"),
        )

        for version, selector, value, state, message in cases:
            with self.subTest(version=version, selector=selector, value=repr(value), state=state), stub_kea({}) as kea:
                with self.assertRaisesRegex((ValueError, LeaseQueryNotMeasurable), message):
                    self.client.lease_search(version, selector, value, state=state, server_id=1)
                self.assertEqual(kea.commands(), [])

    def test_hardware_address_search_returns_matching_leases(self):
        lease = complete_lease({"ip-address": "198.18.0.10", "hw-address": "aa:bb:cc:dd:ee:ff"})
        with stub_kea(
            {
                "lease4-get-by-hw-address": {
                    "result": 0,
                    "arguments": {"leases": [lease]},
                }
            }
        ) as kea:
            result = self.client.lease_search(version=4, selector="hw", value="aa:bb:cc:dd:ee:ff", server_id=1)

        self.assertEqual(result.records, _typed(lease))
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
            lease = complete_lease({"ip-address": "198.18.0.10" if version == 4 else "2001:db8::10"})
            response_arguments = {"leases": [lease]} if multiple else lease
            responses = {command: {"result": 0, "arguments": response_arguments}}
            if selector == "subnet_id":
                responses[f"stat-lease{version}-get"] = _subnet_stats(version, int(value))
            with (
                self.subTest(version=version, selector=selector),
                stub_kea(responses) as kea,
            ):
                result = self.client.lease_search(version, selector, value, server_id=1)
                self.assertEqual(result.records, _typed(lease))
                self.assertEqual(kea.bodies(command)[0]["arguments"], arguments)

    def test_not_found_returns_empty_collection(self):
        with stub_kea({"lease6-get-by-hostname": {"result": 3, "text": "not found"}}):
            result = self.client.lease_search(6, "hostname", "missing.example.invalid", server_id=1)

        self.assertEqual(result.records, ())

    def test_malformed_lease_collection_is_rejected(self):
        with stub_kea(
            {
                "lease4-get-by-hostname": {
                    "result": 0,
                    "arguments": {"leases": "not-a-list"},
                }
            }
        ):
            with self.assertRaisesRegex(MalformedLeaseResponse, "malformed leases collection"):
                self.client.lease_search(4, "hostname", "host.example.invalid", server_id=1)

    def test_large_subnet_is_rejected_before_get_all(self):
        client = kea_client(url="http://kea:8000", max_unpaged_leases=100)
        with stub_kea({"stat-lease4-get": _subnet_stats(4, 12, assigned=101)}) as kea:
            with self.assertRaisesRegex(LeaseQueryTooBroad, "101.*100"):
                client.lease_search(4, "subnet_id", 12, server_id=1)

        self.assertEqual(kea.commands(), ["stat-lease4-get"])

    def test_state_qualifier_uses_the_subnet_scoped_state_command(self):
        client = kea_client(url="http://kea:8000", max_unpaged_leases=100)
        for version in (4, 6):
            lease = complete_lease({"ip-address": "198.18.0.10" if version == 4 else "2001:db8::10", "state": 1})
            with (
                self.subTest(version=version),
                stub_kea(
                    {
                        f"stat-lease{version}-get": _subnet_stats(version, 12, assigned=201, declined=1),
                        f"lease{version}-get-by-state": {"result": 0, "arguments": {"leases": [lease]}},
                    }
                ) as kea,
            ):
                result = client.lease_search(version, "subnet_id", 12, state=1, server_id=1)

                self.assertEqual(result.records, _typed(lease))
                self.assertEqual(kea.commands(), [f"stat-lease{version}-get", f"lease{version}-get-by-state"])
                self.assertEqual(
                    kea.bodies(f"lease{version}-get-by-state")[0]["arguments"],
                    {"subnet-id": 12, "state": 1},
                )

    def test_unmeasured_subnet_state_is_rejected_before_any_request(self):
        with stub_kea({}) as kea:
            with self.assertRaisesRegex(LeaseQueryNotMeasurable, "state 2"):
                self.client.lease_search(4, "subnet_id", 12, state=2, server_id=1)

        self.assertEqual(kea.commands(), [])

    def test_missing_statistics_hook_fails_closed(self):
        with stub_kea({"stat-lease4-get": {"result": 2, "text": "unknown command"}}) as kea:
            with self.assertRaises(LeaseQueryPreflightUnavailable):
                self.client.lease_search(4, "subnet_id", 12, server_id=1)

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
                    self.client.lease_search(4, "subnet_id", 12, server_id=1)

                self.assertEqual(ctx.exception.reason, "statistics")
                self.assertIn("stat_cmds", lease_query_guard_message(ctx.exception, None))
                self.assertEqual(kea.commands(), ["stat-lease4-get"])

    def test_missing_state_command_fails_closed_when_unmeasured(self):
        client = kea_client(url="http://kea:8000", max_unpaged_leases=None)
        with stub_kea(
            {
                "lease4-get-by-state": {"result": 2, "text": "unknown command"},
            }
        ) as kea:
            with self.assertRaises(LeaseQueryPreflightUnavailable) as ctx:
                client.lease_search(4, "subnet_id", 12, state=0, server_id=1)

        self.assertIn("3.1.5", lease_query_guard_message(ctx.exception, 0))
        self.assertEqual(kea.commands(), ["lease4-get-by-state"])

    def test_missing_state_command_does_not_fetch_unmeasured_retained_rows(self):
        for version in (4, 6):
            for limit in (100, None):
                with self.subTest(version=version, limit=limit):
                    client = kea_client(url="http://kea:8000", max_unpaged_leases=limit)
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
                                client.lease_search(version, "subnet_id", 12, state=0, server_id=1)
                            self.assertEqual(ctx.exception.reason, "state-command")
                        finally:
                            self.assertEqual(kea.commands(), commands)

    def test_missing_state_command_fails_closed_when_only_filtered_count_is_bounded(self):
        client = kea_client(url="http://kea:8000", max_unpaged_leases=100)
        with stub_kea(
            {
                "stat-lease4-get": _subnet_stats(4, 12, assigned=201, declined=1),
                "lease4-get-by-state": {"result": 2, "text": "unknown command"},
            }
        ) as kea:
            with self.assertRaises(LeaseQueryPreflightUnavailable):
                client.lease_search(4, "subnet_id", 12, state=1, server_id=1)
        self.assertEqual(kea.commands(), ["stat-lease4-get", "lease4-get-by-state"])

    def test_non_hook_statistics_error_propagates(self):
        with stub_kea({"stat-lease4-get": {"result": 1, "text": "database failure"}}):
            with self.assertRaises(KeaException):
                self.client.lease_search(4, "subnet_id", 12, server_id=1)

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
                    self.client.lease_search(4, "subnet_id", 12, server_id=1)

    def test_v6_delegated_prefixes_contribute_to_the_guard(self):
        client = kea_client(url="http://kea:8000", max_unpaged_leases=100)
        with stub_kea({"stat-lease6-get": _subnet_stats(6, 12, assigned=0, declined=0, assigned_pds=101)}) as kea:
            with self.assertRaisesRegex(LeaseQueryTooBroad, "101.*100"):
                client.lease_search(6, "subnet_id", 12, state=0, server_id=1)

        self.assertEqual(kea.commands(), ["stat-lease6-get"])

    def test_subnet_cidr_resolves_to_id_before_guarded_query(self):
        lease = complete_lease({"ip-address": "198.18.0.10", "state": 0})
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
            result = self.client.lease_search(4, "subnet", "198.18.0.0/24", state=0, server_id=1)

        self.assertEqual(result.records, _typed(lease))
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
            result = self.client.lease_search(4, "subnet", "198.18.0.0/24", server_id=1)

        self.assertEqual(result.records, ())
        self.assertEqual(kea.commands(), ["config-get", "stat-lease4-get", "lease4-get-all"])

    def test_subnet_cidr_matches_equivalent_configured_network_text(self):
        lease = complete_lease({"ip-address": "2001:db8::10", "state": 0})
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
            result = self.client.lease_search(6, "subnet", "2001:db8::/64", server_id=1)

        self.assertEqual(result.records, _typed(lease))
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
        client = kea_client(url="http://kea:8000", max_unpaged_leases=None)
        lease = complete_lease({"ip-address": "198.18.0.10", "state": 0})
        with stub_kea({"lease4-get-all": {"result": 0, "arguments": {"leases": [lease]}}}) as kea:
            result = client.lease_search(4, "subnet_id", 12, server_id=1)

        self.assertEqual(result.records, _typed(lease))
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
        self.client = kea_client(url="http://kea:8000")

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
        self.client = kea_client(url="http://kea:8000")

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
        self.client = kea_client(url="http://kea:8000")

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

    def test_a_live_pool_entry_that_the_edit_cannot_keep_raises_and_sends_no_update(self):
        """The update replaces the Pool list, so a skipped live entry would be deleted from Kea."""
        for label, entry in (
            ("not an object", "10.0.0.50-10.0.0.99"),
            ("no range", {"pool": "not a pool"}),
            ("outside the Subnet", {"pool": "10.0.1.50-10.0.1.99"}),
        ):
            live = {"id": 42, "subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10-10.0.0.20"}, entry]}
            with self.subTest(label), stub_kea({"subnet4-get": _subnet_get_reply(4, live)}) as kea:
                definition = self.client.subnet_definition(4, 42)
                with self.assertRaises(MalformedConfiguration):
                    definition.edited(_edit(pools=("10.0.0.10-10.0.0.20",)))
                self.assertEqual(kea.commands(), ["subnet4-get"])


class TestKeaClientContextManager(TestCase):
    """KeaClient supports context manager protocol for resource cleanup."""

    def test_close_closes_session(self):
        client = kea_client(url="http://kea:8000")
        with patch.object(client._session, "close") as mock_close:
            client.close()
            mock_close.assert_called_once()

    def test_context_manager_calls_close(self):
        client = kea_client(url="http://kea:8000")
        with patch.object(client, "close") as mock_close:
            with client:
                pass
            mock_close.assert_called_once()

    def test_clone_supports_context_manager(self):
        client = kea_client(url="http://kea:8000")
        with client.clone() as worker:
            self.assertIsInstance(worker, KeaClient)
            self.assertEqual(worker.url, client.url)


class TestConfigGetShapeGuard(TestCase):
    """Methods that call config-get raise KeaException on malformed arguments."""

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")

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
        self.client = kea_client(url="http://kea:8000")

    def test_explicit_cursor_is_sent_and_a_short_page_ends_only_the_rest_of_the_scope(self):
        first, second = lease_record("198.18.1.10"), lease_record("198.18.2.10")
        with stub_kea({"lease4-get-page": lease_page(first, second)}) as kea:
            result = self.client.lease_get_page(version=4, limit=10, cursor="198.18.0.255", server_id=7)

        self.assertEqual(result.records, _typed(first, second))
        self.assertEqual((result.server_id, result.coverage, result.next_cursor), (7, "page", None))
        self.assertFalse(result.complete)
        self.assertEqual(kea.bodies("lease4-get-page")[0]["arguments"], {"from": "198.18.0.255", "limit": 10})

    def test_a_full_page_continues_from_its_last_raw_address(self):
        records = [lease_record("2001:db8::10"), lease_record("2001:db8::20")]
        with stub_kea({"lease6-get-page": lease_page(*records)}) as kea:
            result = self.client.lease_get_page(version=6, limit=2, server_id=1)

        self.assertEqual(result.records, _typed(*records))
        self.assertEqual((result.coverage, str(result.next_cursor)), ("page", "2001:db8::20"))
        self.assertEqual(kea.bodies("lease6-get-page")[0]["arguments"], {"from": "::", "limit": 2})

    def test_a_first_page_that_ends_the_family_is_exhaustive(self):
        record = lease_record("198.18.0.10")
        with stub_kea({"lease4-get-page": lease_page(record)}):
            result = self.client.lease_get_page(version=4, limit=10, server_id=1)

        self.assertEqual((result.coverage, result.complete), ("exhaustive", True))

    def test_rejects_invalid_page_parameters_without_request(self):
        cases = (
            ((5,), {"limit": 10}, "version must be 4 or 6"),
            ((4,), {"limit": False}, "positive integer"),
            ((4,), {"limit": 10, "cursor": "not-an-address"}, "Invalid DHCPv4 lease cursor"),
            ((4,), {"limit": 10, "cursor": "2001:db8::1"}, "IPv6.*DHCPv4"),
        )

        for args, kwargs, message in cases:
            with self.subTest(args=args, kwargs=kwargs), stub_kea({}) as kea:
                with self.assertRaisesRegex(ValueError, message):
                    self.client.lease_get_page(*args, **kwargs, server_id=1)
                self.assertEqual(kea.commands(), [])

    def test_an_invalid_record_in_a_partial_page_is_a_diagnostic_beside_its_valid_sibling(self):
        good = lease_record("198.18.0.20")
        for address, code in (("not-an-address", "invalid-address"), ("2001:db8::1", "wrong-family")):
            malformed = {**lease_record("198.18.0.10"), "ip-address": address}
            with self.subTest(address=address), stub_kea({"lease4-get-page": lease_page(malformed, good)}):
                result = self.client.lease_get_page(version=4, limit=3, server_id=1)

                self.assertEqual(result.records, _typed(good))
                self.assertEqual([(d.code, d.source_position) for d in result.diagnostics], [(code, "leases[0]")])
                self.assertFalse(result.complete)

    def test_a_full_page_without_a_usable_last_address_fails_the_read(self):
        for address in ("not-an-address", "2001:db8::1"):
            with (
                self.subTest(address=address),
                stub_kea({"lease4-get-page": lease_page({**lease_record("198.18.0.10"), "ip-address": address})}),
                self.assertRaisesRegex(MalformedLeaseResponse, "usable continuation"),
            ):
                self.client.lease_get_page(version=4, limit=1, server_id=1)

    def test_rejects_count_that_does_not_match_the_lease_collection(self):
        """Kea defines count as the number of leases in the returned page."""
        page = {"result": 0, "arguments": {"leases": [lease_record("198.18.0.10")], "count": 0}}
        with stub_kea({"lease4-get-page": page}), self.assertRaisesRegex(MalformedLeaseResponse, "count"):
            self.client.lease_get_page(version=4, limit=10, server_id=1)


class TestLeaseGetAllPagination(TestCase):
    """KeaClient.lease_get_all() reads every page, accounts raw records and proves the end or reports it."""

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")

    def _all(self, records, **kwargs) -> tuple[LeaseSnapshot, list[dict]]:
        with stub_kea({"lease4-get-page": lease_pages(records)}) as kea:
            snapshot = self.client.lease_get_all(version=4, server_id=1, **kwargs)
        return snapshot, [body["arguments"] for body in kea.bodies("lease4-get-page")]

    def test_an_empty_daemon_is_an_exhaustive_complete_snapshot(self):
        snapshot, requests_sent = self._all([])

        self.assertEqual((snapshot.records, snapshot.coverage, snapshot.complete), ((), "exhaustive", True))
        self.assertEqual(requests_sent, [{"from": "0.0.0.0", "limit": 250}])  # noqa: S104 - Kea page start

    def test_pages_continue_from_the_last_raw_address(self):
        records = [lease_record(f"10.0.0.{index}") for index in range(1, 4)]

        snapshot, requests_sent = self._all(records, per_page=2)

        self.assertEqual(snapshot.records, _typed(*records))
        self.assertTrue(snapshot.attests_absence(_identity("10.0.0.9")))
        self.assertEqual([body["from"] for body in requests_sent], ["0.0.0.0", "10.0.0.2"])  # noqa: S104

    def test_a_page_with_zero_accepted_records_still_reaches_later_valid_data(self):
        malformed = [lease_record(f"10.0.0.{index}", drop=("state",)) for index in (1, 2)]
        valid = lease_record("10.0.0.3")

        snapshot, requests_sent = self._all([*malformed, valid], per_page=2)

        self.assertEqual(snapshot.records, _typed(valid))
        self.assertEqual(
            [(d.code, d.field, d.source_position) for d in snapshot.diagnostics],
            [("missing-field", "state", "pages[0].leases[0]"), ("missing-field", "state", "pages[0].leases[1]")],
        )
        self.assertEqual([body["from"] for body in requests_sent], ["0.0.0.0", "10.0.0.2"])  # noqa: S104
        self.assertEqual(snapshot.coverage, "exhaustive")
        self.assertFalse(snapshot.complete)
        self.assertFalse(snapshot.attests_absence(_identity("10.0.0.9")))

    def test_a_page_that_repeats_the_cursor_fails_the_read(self):
        page = lease_page(lease_record("198.18.0.10"))
        with stub_kea({"lease4-get-page": page}) as kea:
            with self.assertRaisesRegex(MalformedLeaseResponse, "not in ascending order after its cursor"):
                self.client.lease_get_all(version=4, per_page=1, server_id=1)

        self.assertEqual(len(kea.bodies("lease4-get-page")), 2)

    def test_a_full_page_without_a_usable_cursor_fails_the_read(self):
        for record in ("not-a-dict", {"hw-address": "aa:bb:cc:dd:ee:ff"}):
            with (
                self.subTest(record=record),
                stub_kea({"lease4-get-page": {"result": 0, "arguments": {"leases": [record], "count": 1}}}),
                self.assertRaisesRegex(MalformedLeaseResponse, "usable continuation"),
            ):
                self.client.lease_get_all(version=4, per_page=1, server_id=1)

    def test_the_cap_counts_raw_records_and_never_requests_past_it(self):
        malformed = [lease_record(f"10.0.0.{index}", state="x") for index in range(1, 4)]
        records = [*malformed, lease_record("10.0.0.4"), lease_record("10.0.0.5")]

        snapshot, requests_sent = self._all(records, per_page=10, max_leases=3)

        self.assertEqual(snapshot.records, ())
        self.assertEqual(len(snapshot.diagnostics), 3)
        self.assertEqual(requests_sent, [{"from": "0.0.0.0", "limit": 3}, {"from": "10.0.0.3", "limit": 1}])  # noqa: S104
        self.assertEqual((snapshot.coverage, str(snapshot.next_cursor)), ("page", "10.0.0.3"))
        self.assertFalse(snapshot.complete)

    def test_reaching_the_cap_exactly_at_the_end_is_proven_by_one_more_read(self):
        records = [lease_record("10.0.0.1"), lease_record("10.0.0.2")]

        snapshot, requests_sent = self._all(records, per_page=2, max_leases=2)

        self.assertEqual(snapshot.records, _typed(*records))
        self.assertEqual((snapshot.coverage, snapshot.next_cursor, snapshot.complete), ("exhaustive", None, True))
        self.assertEqual(requests_sent[1], {"from": "10.0.0.2", "limit": 1})

    def test_a_probe_record_without_a_usable_address_still_reports_the_cap(self):
        records = [lease_record("10.0.0.1"), lease_record("10.0.0.2")]
        probe = lease_page({**lease_record("10.0.0.3"), "ip-address": "not-an-address"})
        with stub_kea({"lease4-get-page": queued(lease_page(*records), probe)}):
            snapshot = self.client.lease_get_all(version=4, per_page=2, max_leases=2, server_id=1)

        self.assertEqual(snapshot.records, _typed(*records))
        self.assertEqual((snapshot.coverage, str(snapshot.next_cursor)), ("page", "10.0.0.2"))

    def test_invalid_bounds_raise_before_any_request(self):
        for kwargs, name in (
            ({"per_page": 0}, "per_page"),
            ({"per_page": -1}, "per_page"),
            ({"max_leases": 0}, "max_leases"),
            ({"max_leases": -1}, "max_leases"),
        ):
            with self.subTest(kwargs=kwargs), stub_kea({}) as kea:
                with self.assertRaisesRegex(ValueError, name):
                    self.client.lease_get_all(version=4, server_id=1, **kwargs)
                self.assertEqual(kea.commands(), [])

    def test_an_unusable_envelope_fails_the_read(self):
        cases = (
            ([], "malformed lease response"),
            ({"result": 0, "arguments": "unexpected"}, "leases collection"),
            ({"result": 0, "arguments": {"leases": "bad"}}, "leases collection"),
            ({"result": 0, "arguments": {"leases": [lease_record("10.0.0.1")], "count": "1"}}, "count"),
        )
        for reply, message in cases:
            with (
                self.subTest(message=message),
                stub_kea({"lease4-get-page": reply}),
                self.assertRaisesRegex(MalformedLeaseResponse, message),
            ):
                self.client.lease_get_all(version=4, server_id=1)


class TestPersistConfigFlag(TestCase):
    """Tests that persist_config=False skips the persist step."""

    def test_default_persist_config_is_true(self):
        """KeaClient defaults to persist_config=True."""
        client = kea_client(url="http://kea:8000")
        self.assertTrue(client.persist_config)

    def test_persist_config_false_stored(self):
        """KeaClient stores persist_config=False when passed."""
        client = kea_client(url="http://kea:8000", persist_config=False)
        self.assertFalse(client.persist_config)

    def test_clone_propagates_persist_config_false(self):
        """clone() copies persist_config=False to the new instance."""
        client = kea_client(url="http://kea:8000", persist_config=False)
        cloned = client.clone()
        self.assertFalse(cloned.persist_config)

    def test_clone_propagates_persist_config_true(self):
        """clone() copies persist_config=True (default) to the new instance."""
        client = kea_client(url="http://kea:8000", persist_config=True)
        cloned = client.clone()
        self.assertTrue(cloned.persist_config)

    def test_persist_config_false_sends_no_persist_step(self):
        """persist() sends nothing when persist_config=False."""
        client = kea_client(url="http://kea:8000", persist_config=False)
        with stub_kea({}) as kea:
            self.assertEqual(client.persist(4).persistence, "not-requested")
        self.assertEqual(kea.commands(), [])


# ---------------------------------------------------------------------------
# Coverage-gap tests — lines not yet exercised by the suite above
# ---------------------------------------------------------------------------


class TestGetAvailableCommandsMalformed(TestCase):
    """get_available_commands raises RuntimeError on empty / non-dict response."""

    def setUp(self):
        self.client = kea_client(url="http://kea:8000")

    def test_empty_list_raises_runtime_error(self):
        """Empty response list hits the 'not resp' branch and raises RuntimeError."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response([])):
            with self.assertRaises(RuntimeError):
                self.client.get_available_commands(4)
