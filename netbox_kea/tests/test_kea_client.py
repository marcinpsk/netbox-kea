# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.kea — KeaClient, KeaException, check_response.

These tests mock all HTTP calls and require no running services.
"""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests

from netbox_kea.kea import (
    AmbiguousConfigSetError,
    KeaClient,
    KeaConfigPersistError,
    KeaConfigTestError,
    KeaException,
    PartialPersistError,
    check_response,
)


def _mock_http_response(json_data, status_code=200):
    """Build a mock requests.Response returning *json_data*."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    else:
        mock_resp.raise_for_status.return_value = None
    return mock_resp


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


class TestReservationGetPage(TestCase):
    """Tests for KeaClient.reservation_get_page(service, source_index, from_index, limit) -> tuple[list, int, int]."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _patched_post(self, json_data):
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_returns_hosts_and_next_pagination(self):
        hosts = [{"subnet-id": 1, "ip-address": "192.168.1.100", "hw-address": "aa:bb:cc:dd:ee:ff"}]
        # Simulate a full page (1 host, limit=1) so next pagination is returned.
        resp = [
            {
                "result": 0,
                "arguments": {
                    "count": 1,
                    "hosts": hosts,
                    "next": {"source-index": 1, "from": 1},
                },
            }
        ]
        with self._patched_post(resp):
            result_hosts, next_from, next_src = self.client.reservation_get_page("dhcp4", limit=1)
        self.assertEqual(result_hosts, hosts)
        self.assertEqual(next_from, 1)
        self.assertEqual(next_src, 1)

    def test_returns_next_cursor_regardless_of_page_size(self):
        """Kea's next cursor is always read from the response, even on a partial page."""
        hosts = [{"subnet-id": 1, "ip-address": "192.168.1.100", "hw-address": "aa:bb:cc:dd:ee:ff"}]
        resp = [
            {
                "result": 0,
                "arguments": {
                    "count": 1,
                    "hosts": hosts,
                    "next": {"source-index": 1, "from": 1},
                },
            }
        ]
        with self._patched_post(resp):
            # limit=100 but only 1 host returned — cursor still read from Kea's next field
            result_hosts, next_from, next_src = self.client.reservation_get_page("dhcp4", limit=100)
        self.assertEqual(result_hosts, hosts)
        self.assertEqual(next_from, 1)
        self.assertEqual(next_src, 1)

    def test_sends_correct_arguments_defaults(self):
        resp = [
            {
                "result": 0,
                "arguments": {"count": 0, "hosts": [], "next": {"source-index": 0, "from": 0}},
            }
        ]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_get_page("dhcp4")
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["command"], "reservation-get-page")
        self.assertEqual(sent_json["service"], ["dhcp4"])
        self.assertEqual(sent_json["arguments"]["source-index"], 0)
        self.assertEqual(sent_json["arguments"]["from"], 0)
        self.assertEqual(sent_json["arguments"]["limit"], 100)
        self.assertNotIn("count", sent_json["arguments"])
        self.assertNotIn("index", sent_json["arguments"])

    def test_sends_correct_custom_arguments(self):
        resp = [
            {
                "result": 0,
                "arguments": {"count": 0, "hosts": [], "next": {"source-index": 1, "from": 75}},
            }
        ]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_get_page("dhcp4", source_index=1, from_index=50, limit=25)
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["arguments"]["source-index"], 1)
        self.assertEqual(sent_json["arguments"]["from"], 50)
        self.assertEqual(sent_json["arguments"]["limit"], 25)

    def test_result_3_returns_empty_tuple(self):
        resp = [{"result": 3, "text": "0 IPv4 host(s) found."}]
        with self._patched_post(resp):
            result_hosts, next_from, next_src = self.client.reservation_get_page("dhcp4")
        self.assertEqual(result_hosts, [])
        self.assertEqual(next_from, 0)
        self.assertEqual(next_src, 0)

    def test_result_1_raises_kea_exception(self):
        resp = [{"result": 1, "text": "Command not supported by the server"}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException):
                self.client.reservation_get_page("dhcp4")

    def test_result_2_raises_kea_exception(self):
        resp = [{"result": 2, "text": "unknown command 'reservation-get-page'"}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException):
                self.client.reservation_get_page("dhcp4")


class TestReservationAdd(TestCase):
    """Tests for KeaClient.reservation_add(service, reservation) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _patched_post(self, json_data):
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_sends_correct_command_and_payload(self):
        reservation = {
            "subnet-id": 1,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "192.168.1.100",
            "hostname": "testhost.example.com",
        }
        resp = [{"result": 0, "text": "Host added."}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_add("dhcp4", reservation)
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["command"], "reservation-add")
        self.assertEqual(sent_json["service"], ["dhcp4"])
        self.assertEqual(sent_json["arguments"]["reservation"], reservation)

    def test_returns_none_on_success(self):
        reservation = {"subnet-id": 1, "ip-address": "192.168.1.100"}
        resp = [{"result": 0, "text": "Host added."}]
        with self._patched_post(resp):
            result = self.client.reservation_add("dhcp4", reservation)
        self.assertIsNone(result)

    def test_raises_kea_exception_on_error(self):
        reservation = {"subnet-id": 1, "ip-address": "192.168.1.100"}
        resp = [{"result": 1, "text": "failed to add host: conflicts with existing reservation"}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException):
                self.client.reservation_add("dhcp4", reservation)


class TestReservationUpdate(TestCase):
    """Tests for KeaClient.reservation_update(service, reservation) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _patched_post(self, json_data):
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_sends_correct_command_and_payload(self):
        reservation = {
            "subnet-id": 1,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "192.168.1.100",
            "hostname": "updated-host.example.com",
        }
        resp = [{"result": 0, "text": "Host updated."}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_update("dhcp4", reservation)
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["command"], "reservation-update")
        self.assertEqual(sent_json["service"], ["dhcp4"])
        self.assertEqual(sent_json["arguments"]["reservation"], reservation)

    def test_returns_none_on_success(self):
        reservation = {"subnet-id": 1, "ip-address": "192.168.1.100"}
        resp = [{"result": 0, "text": "Host updated."}]
        with self._patched_post(resp):
            result = self.client.reservation_update("dhcp4", reservation)
        self.assertIsNone(result)

    def test_raises_kea_exception_on_error(self):
        reservation = {"subnet-id": 1, "ip-address": "192.168.1.100"}
        resp = [{"result": 1, "text": "failed to update host: host not found"}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException):
                self.client.reservation_update("dhcp4", reservation)


class TestReservationDel(TestCase):
    """Tests for KeaClient.reservation_del(service, subnet_id, ip_address, identifier_type, identifier) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _patched_post(self, json_data):
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_sends_del_by_ip_address(self):
        resp = [{"result": 0, "text": "Host deleted."}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_del("dhcp4", subnet_id=1, ip_address="192.168.1.100")
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["command"], "reservation-del")
        self.assertEqual(sent_json["service"], ["dhcp4"])
        self.assertEqual(sent_json["arguments"]["subnet-id"], 1)
        self.assertEqual(sent_json["arguments"]["ip-address"], "192.168.1.100")
        self.assertNotIn("identifier-type", sent_json["arguments"])

    def test_sends_del_by_identifier_type_and_identifier(self):
        resp = [{"result": 0, "text": "Host deleted."}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_del(
                "dhcp4",
                subnet_id=1,
                identifier_type="hw-address",
                identifier="aa:bb:cc:dd:ee:ff",
            )
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["arguments"]["subnet-id"], 1)
        self.assertEqual(sent_json["arguments"]["identifier-type"], "hw-address")
        self.assertEqual(sent_json["arguments"]["identifier"], "aa:bb:cc:dd:ee:ff")
        self.assertNotIn("ip-address", sent_json["arguments"])

    def test_raises_value_error_without_ip_or_identifier(self):
        with self.assertRaises(ValueError):
            self.client.reservation_del("dhcp4", subnet_id=1)

    def test_returns_none_on_success(self):
        resp = [{"result": 0, "text": "Host deleted."}]
        with self._patched_post(resp):
            result = self.client.reservation_del("dhcp4", subnet_id=1, ip_address="192.168.1.100")
        self.assertIsNone(result)

    def test_raises_kea_exception_on_error(self):
        resp = [{"result": 1, "text": "Host not found."}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException):
                self.client.reservation_del("dhcp4", subnet_id=1, ip_address="192.168.1.100")

    def test_sends_ipv6_del_by_ip_address(self):
        resp = [{"result": 0, "text": "Host deleted."}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_del("dhcp6", subnet_id=2, ip_address="2001:db8::100")
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["service"], ["dhcp6"])
        self.assertEqual(sent_json["arguments"]["ip-address"], "2001:db8::100")

    def test_raises_value_error_when_both_ip_and_identifier_type_given(self):
        """Providing both ip_address and identifier_type raises ValueError (mutually exclusive)."""
        with self.assertRaises(ValueError):
            self.client.reservation_del(
                "dhcp4",
                subnet_id=1,
                ip_address="192.168.1.100",
                identifier_type="hw-address",
                identifier="aa:bb:cc:dd:ee:ff",
            )

    def test_raises_value_error_when_identifier_type_given_without_identifier(self):
        """Providing identifier_type without identifier raises ValueError."""
        with self.assertRaises(ValueError):
            self.client.reservation_del("dhcp4", subnet_id=1, identifier_type="hw-address")


class TestReservationGet(TestCase):
    """Tests for KeaClient.reservation_get(service, subnet_id, ip_address, identifier_type, identifier) -> dict | None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _patched_post(self, json_data):
        return patch.object(self.client._session, "post", return_value=_mock_http_response(json_data))

    def test_returns_host_dict_on_result_0(self):
        host = {
            "subnet-id": 1,
            "ip-address": "192.168.1.100",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
        }
        # Kea returns the host dict directly in "arguments" (no nested "host" key)
        resp = [{"result": 0, "arguments": host}]
        with self._patched_post(resp):
            result = self.client.reservation_get("dhcp4", subnet_id=1, ip_address="192.168.1.100")
        self.assertEqual(result, host)

    def test_returns_none_on_result_3_not_found(self):
        resp = [{"result": 3, "text": "Host not found."}]
        with self._patched_post(resp):
            result = self.client.reservation_get("dhcp4", subnet_id=1, ip_address="192.168.1.100")
        self.assertIsNone(result)

    def test_sends_correct_command_by_ip_address(self):
        resp = [{"result": 3, "text": "Host not found."}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_get("dhcp4", subnet_id=1, ip_address="192.168.1.100")
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["command"], "reservation-get")
        self.assertEqual(sent_json["service"], ["dhcp4"])
        self.assertEqual(sent_json["arguments"]["subnet-id"], 1)
        self.assertEqual(sent_json["arguments"]["ip-address"], "192.168.1.100")

    def test_sends_correct_command_by_identifier(self):
        resp = [{"result": 3, "text": "Host not found."}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)) as mock_post:
            self.client.reservation_get(
                "dhcp4",
                subnet_id=1,
                identifier_type="hw-address",
                identifier="aa:bb:cc:dd:ee:ff",
            )
        sent_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        self.assertEqual(sent_json["arguments"]["identifier-type"], "hw-address")
        self.assertEqual(sent_json["arguments"]["identifier"], "aa:bb:cc:dd:ee:ff")

    def test_raises_kea_exception_on_result_1(self):
        resp = [{"result": 1, "text": "Command failed."}]
        with self._patched_post(resp):
            with self.assertRaises(KeaException):
                self.client.reservation_get("dhcp4", subnet_id=1, ip_address="192.168.1.100")

    def test_raises_value_error_when_both_ip_and_identifier_type_given(self):
        """Providing both ip_address and identifier_type raises ValueError (mutually exclusive)."""
        with self.assertRaises(ValueError):
            self.client.reservation_get(
                "dhcp4",
                subnet_id=1,
                ip_address="192.168.1.100",
                identifier_type="hw-address",
                identifier="aa:bb:cc:dd:ee:ff",
            )

    def test_raises_value_error_with_neither_ip_nor_identifier(self):
        """Providing neither ip_address nor identifier_type+identifier raises ValueError."""
        with self.assertRaises(ValueError):
            self.client.reservation_get("dhcp4", subnet_id=1)

    def test_raises_value_error_when_identifier_type_given_without_identifier(self):
        """Providing identifier_type without identifier raises ValueError."""
        with self.assertRaises(ValueError):
            self.client.reservation_get("dhcp4", subnet_id=1, identifier_type="hw-address")


# ---------------------------------------------------------------------------
# TestPoolAdd / TestPoolDel
# ---------------------------------------------------------------------------

_LIST_WITH_POOL_CMDS = [
    {
        "result": 0,
        "arguments": [
            "subnet4-pool-add",
            "subnet6-pool-add",
            "subnet4-pool-del",
            "subnet6-pool-del",
            "config-write",
        ],
    }
]
_LIST_WITHOUT_POOL_CMDS = [
    {
        "result": 0,
        "arguments": [
            "subnet4-delta-add",
            "subnet4-delta-del",
            "subnet6-delta-add",
            "subnet6-delta-del",
            "config-write",
        ],
    }
]
_OK = [{"result": 0, "text": "ok"}]
_SUBNET4_GET = [{"result": 0, "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}]
_SUBNET6_GET = [{"result": 0, "arguments": {"subnet6": [{"id": 2, "subnet": "2001:db8::/48"}]}}]


def _side_effects(*responses):
    import copy

    return [_mock_http_response(copy.deepcopy(r)) for r in responses]


class TestPoolAdd(TestCase):
    """Tests for KeaClient.pool_add() — covers Kea 2.x (pool-add) and 3.x (delta-add) paths."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _cmds(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"])["command"] for c in mock_post.call_args_list]

    def test_pool_add_uses_pool_add_when_available(self):
        """When subnet4-pool-add is in list-commands, it is used directly."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_add(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        self.assertEqual(
            self._cmds(mock_post), ["list-commands", "subnet4-pool-add", "config-get", "config-test", "config-write"]
        )

    def test_pool_add_v4_sends_correct_arguments(self):
        """subnet4-pool-add arguments include correct id and pool."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_add(version=4, subnet_id=3, pool="192.168.1.100-192.168.1.200")
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-pool-add"
        )
        subnet_args = add_call["arguments"]["subnet4"][0]
        self.assertEqual(subnet_args["id"], 3)
        self.assertEqual(subnet_args["pools"][0]["pool"], "192.168.1.100-192.168.1.200")

    def test_pool_add_v6_uses_subnet6_command(self):
        """subnet6-pool-add is used for version=6."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP_V6, _OK, _OK),
        ) as mock_post:
            self.client.pool_add(version=6, subnet_id=2, pool="2001:db8::/64")
        cmds = self._cmds(mock_post)
        self.assertIn("subnet6-pool-add", cmds)
        self.assertNotIn("subnet4-pool-add", cmds)

    def test_pool_add_falls_back_to_delta_add(self):
        """When subnet4-pool-add is unavailable, subnet4-delta-add is used."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITHOUT_POOL_CMDS, _SUBNET4_GET, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_add(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        self.assertEqual(
            self._cmds(mock_post),
            ["list-commands", "subnet4-get", "subnet4-delta-add", "config-get", "config-test", "config-write"],
        )

    def test_pool_add_delta_add_includes_subnet_cidr(self):
        """subnet4-delta-add payload includes the CIDR from subnet4-get."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITHOUT_POOL_CMDS, _SUBNET4_GET, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_add(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        delta_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-delta-add"
        )
        subnet_args = delta_call["arguments"]["subnet4"][0]
        self.assertEqual(subnet_args["subnet"], "10.0.0.0/24")
        self.assertEqual(subnet_args["id"], 1)
        self.assertEqual(subnet_args["pools"][0]["pool"], "10.0.0.50-10.0.0.99")

    def test_pool_add_calls_config_write_after_add(self):
        """config-write is called and appears after pool-add."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_add(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        cmds = self._cmds(mock_post)
        self.assertIn("config-write", cmds)
        self.assertLess(cmds.index("subnet4-pool-add"), cmds.index("config-write"))

    def test_pool_add_raises_kea_exception_on_error(self):
        """KeaException is raised when pool-add returns result != 0."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _LIST_WITH_POOL_CMDS,
                [{"result": 1, "text": "Pool already exists."}],
            ),
        ):
            with self.assertRaises(KeaException):
                self.client.pool_add(version=4, subnet_id=1, pool="10.0.0.1-10.0.0.10")

    def test_pool_add_returns_none_on_success(self):
        """pool_add returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ):
            result = self.client.pool_add(version=4, subnet_id=1, pool="10.0.0.1-10.0.0.10")
        self.assertIsNone(result)


class TestPoolDel(TestCase):
    """Tests for KeaClient.pool_del() — covers Kea 2.x (pool-del) and 3.x (delta-del) paths."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _cmds(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"])["command"] for c in mock_post.call_args_list]

    def test_pool_del_uses_pool_del_when_available(self):
        """When subnet4-pool-del is in list-commands, it is used directly."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_del(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        self.assertEqual(
            self._cmds(mock_post), ["list-commands", "subnet4-pool-del", "config-get", "config-test", "config-write"]
        )

    def test_pool_del_v4_sends_correct_arguments(self):
        """subnet4-pool-del arguments include correct id and pool."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_del(version=4, subnet_id=5, pool="10.5.0.1-10.5.0.50")
        del_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-pool-del"
        )
        subnet_args = del_call["arguments"]["subnet4"][0]
        self.assertEqual(subnet_args["id"], 5)
        self.assertEqual(subnet_args["pools"][0]["pool"], "10.5.0.1-10.5.0.50")

    def test_pool_del_v6_uses_subnet6_command(self):
        """subnet6-pool-del is used for version=6."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP_V6, _OK, _OK),
        ) as mock_post:
            self.client.pool_del(version=6, subnet_id=2, pool="2001:db8::/64")
        cmds = self._cmds(mock_post)
        self.assertIn("subnet6-pool-del", cmds)
        self.assertNotIn("subnet4-pool-del", cmds)

    def test_pool_del_falls_back_to_delta_del(self):
        """When subnet4-pool-del is unavailable, subnet4-delta-del is used."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITHOUT_POOL_CMDS, _SUBNET4_GET, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_del(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        self.assertEqual(
            self._cmds(mock_post),
            ["list-commands", "subnet4-get", "subnet4-delta-del", "config-get", "config-test", "config-write"],
        )

    def test_pool_del_delta_del_includes_subnet_cidr(self):
        """subnet4-delta-del payload includes the CIDR from subnet4-get."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITHOUT_POOL_CMDS, _SUBNET4_GET, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_del(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        delta_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-delta-del"
        )
        subnet_args = delta_call["arguments"]["subnet4"][0]
        self.assertEqual(subnet_args["subnet"], "10.0.0.0/24")
        self.assertEqual(subnet_args["id"], 1)
        self.assertEqual(subnet_args["pools"][0]["pool"], "10.0.0.50-10.0.0.99")

    def test_pool_del_calls_config_write_after_del(self):
        """config-write is called and appears after pool-del."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.pool_del(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        cmds = self._cmds(mock_post)
        self.assertIn("config-write", cmds)
        self.assertLess(cmds.index("subnet4-pool-del"), cmds.index("config-write"))

    def test_pool_del_raises_kea_exception_on_error(self):
        """KeaException is raised when pool-del returns result != 0."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _LIST_WITH_POOL_CMDS,
                [{"result": 3, "text": "Pool not found."}],
            ),
        ):
            with self.assertRaises(KeaException):
                self.client.pool_del(version=4, subnet_id=1, pool="10.0.0.1-10.0.0.10")

    def test_pool_del_returns_none_on_success(self):
        """pool_del returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ):
            result = self.client.pool_del(version=4, subnet_id=1, pool="10.0.0.1-10.0.0.10")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestSubnetAdd / TestSubnetDel
# ---------------------------------------------------------------------------

_SUBNET4_ADD_RESP = [
    {"result": 0, "arguments": {"subnets": [{"id": 10, "subnet": "10.99.0.0/24"}]}, "text": "IPv4 subnet added"}
]
_SUBNET6_ADD_RESP = [
    {"result": 0, "arguments": {"subnets": [{"id": 20, "subnet": "2001:db8:99::/48"}]}, "text": "IPv6 subnet added"}
]
_SUBNET_DEL_RESP = [{"result": 0, "text": "IPv4 subnet deleted"}]
# subnet-list response returned before auto-ID lookup
_SUBNET4_LIST_RESP = [
    {"result": 0, "arguments": {"subnets": [{"id": 1, "subnet": "10.0.0.0/8"}, {"id": 2, "subnet": "192.168.0.0/16"}]}}
]
_SUBNET6_LIST_RESP = [{"result": 0, "arguments": {"subnets": [{"id": 5, "subnet": "2001:db8::/32"}]}}]


class TestSubnetAdd(TestCase):
    """Tests for KeaClient.subnet_add()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _cmds(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"])["command"] for c in mock_post.call_args_list]

    def test_subnet_add_v4_sends_correct_command(self):
        """subnet4-add is sent for version=4."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        self.assertIn("subnet4-add", self._cmds(mock_post))

    def test_subnet_add_v6_sends_correct_command(self):
        """subnet6-add is sent for version=6."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET6_LIST_RESP, _SUBNET6_ADD_RESP, _CONFIG_GET_RUNNING_RESP_V6, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(version=6, subnet_cidr="2001:db8:99::/48")
        self.assertIn("subnet6-add", self._cmds(mock_post))
        self.assertNotIn("subnet4-add", self._cmds(mock_post))

    def test_subnet_add_sends_subnet_cidr(self):
        """subnet4-add payload includes the subnet CIDR."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-add"
        )
        subnet_arg = add_call["arguments"]["subnet4"][0]
        self.assertEqual(subnet_arg["subnet"], "10.99.0.0/24")

    def test_subnet_add_includes_optional_id(self):
        """subnet4-add payload includes id when provided (no list call)."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24", subnet_id=42)
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-add"
        )
        self.assertEqual(add_call["arguments"]["subnet4"][0]["id"], 42)
        # Exactly 4 calls: subnet4-add + config-get + config-test + config-write (no subnet4-list)
        self.assertEqual(len(mock_post.call_args_list), 4)

    def test_subnet_add_auto_assigns_id_as_max_plus_one(self):
        """When no subnet_id provided, auto-assigns max existing ID + 1."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-add"
        )
        # _SUBNET4_LIST_RESP has ids 1 and 2, so auto-assigned should be 3
        self.assertEqual(add_call["arguments"]["subnet4"][0]["id"], 3)

    def test_subnet_add_includes_pools(self):
        """subnet4-add payload includes pools when provided."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(
                version=4,
                subnet_cidr="10.99.0.0/24",
                pools=["10.99.0.100-10.99.0.200"],
            )
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-add"
        )
        self.assertEqual(
            add_call["arguments"]["subnet4"][0]["pools"],
            [{"pool": "10.99.0.100-10.99.0.200"}],
        )

    def test_subnet_add_includes_option_data(self):
        """subnet4-add payload includes option-data for gateway/DNS/NTP."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(
                version=4,
                subnet_cidr="10.99.0.0/24",
                gateway="10.99.0.1",
                dns_servers=["8.8.8.8", "8.8.4.4"],
                ntp_servers=["pool.ntp.org"],
            )
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-add"
        )
        opts = {o["name"]: o["data"] for o in add_call["arguments"]["subnet4"][0]["option-data"]}
        self.assertEqual(opts["routers"], "10.99.0.1")
        self.assertIn("8.8.8.8", opts["domain-name-servers"])
        self.assertIn("pool.ntp.org", opts["ntp-servers"])

    def test_subnet_add_calls_config_write(self):
        """config-write is called after subnet4-add."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        cmds = self._cmds(mock_post)
        self.assertIn("config-write", cmds)
        self.assertLess(cmds.index("subnet4-add"), cmds.index("config-write"))

    def test_subnet_add_raises_on_kea_error(self):
        """KeaException is raised when Kea returns result != 0."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, [{"result": 1, "text": "subnet already exists"}]),
        ):
            with self.assertRaises(KeaException):
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")

    def test_subnet_add_returns_assigned_id(self):
        """subnet_add returns the authoritative ID echoed back by Kea in the add response."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ):
            result = self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        # _SUBNET4_ADD_RESP echoes back id=10; we prefer the Kea-provided ID over the
        # locally-computed max_id+1 value so we always have the authoritative ID.
        self.assertEqual(result, 10)

    def test_subnet_add_without_explicit_id_falls_back_when_list_fails(self):
        """When subnet{v}-list raises KeaException, subnet_add falls back to no explicit ID."""
        list_error = [{"result": 2, "text": "unknown command"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(list_error, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-add"
        )
        # No explicit id should be set when the list call fails
        self.assertNotIn("id", add_call["arguments"]["subnet4"][0])

    def test_subnet_add_list_fails_returns_kea_assigned_id(self):
        """When subnet{v}-list fails, subnet_add returns the ID Kea echoes back in the add response."""
        list_error = [{"result": 2, "text": "unknown command"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(list_error, _SUBNET4_ADD_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ):
            result = self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        # _SUBNET4_ADD_RESP has id=10; that should be the return value even though list failed.
        self.assertEqual(result, 10)

    def test_subnet_add_retries_on_duplicate_id(self):
        """subnet_add retries with id+1 when Kea rejects with 'duplicate' in error."""
        duplicate_resp = [{"result": 1, "text": "duplicate subnet id"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_LIST_RESP,  # subnet4-list → ids 1, 2 → assigns id=3
                duplicate_resp,  # first subnet4-add attempt → duplicate
                _SUBNET4_ADD_RESP,  # second subnet4-add attempt (id=4) → success; Kea echoes id=10
                _CONFIG_GET_RUNNING_RESP,  # config-get
                _OK,  # config-test
                _OK,  # config-write
            ),
        ) as mock_post:
            result = self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        add_calls = [
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-add"
        ]
        self.assertEqual(len(add_calls), 2)
        self.assertEqual(add_calls[0]["arguments"]["subnet4"][0]["id"], 3)
        self.assertEqual(add_calls[1]["arguments"]["subnet4"][0]["id"], 4)
        # Kea echoes back id=10 in _SUBNET4_ADD_RESP — that is the authoritative return value.
        self.assertEqual(result, 10)

    def test_subnet_add_v6_uses_dns_servers_option_name(self):
        """For DHCPv6, dns_servers option name must be 'dns-servers' not 'domain-name-servers'."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET6_LIST_RESP, _SUBNET6_ADD_RESP, _CONFIG_GET_RUNNING_RESP_V6, _OK, _OK),
        ) as mock_post:
            self.client.subnet_add(
                version=6,
                subnet_cidr="2001:db8:99::/48",
                dns_servers=["2001:4860:4860::8888"],
                ntp_servers=["ntp.example.com"],
            )
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet6-add"
        )
        opts = {o["name"]: o["data"] for o in add_call["arguments"]["subnet6"][0]["option-data"]}
        self.assertIn("dns-servers", opts)
        self.assertNotIn("domain-name-servers", opts)
        self.assertIn("sntp-servers", opts)
        self.assertNotIn("ntp-servers", opts)

    def test_raises_when_all_retries_exhausted_with_duplicate_id(self):
        """subnet_add raises the last KeaException when all 3 retry attempts get a duplicate-id error."""
        _DUPLICATE_ID_RESP = [{"result": 1, "text": "duplicate subnet id: X"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_LIST_RESP,
                _DUPLICATE_ID_RESP,
                _DUPLICATE_ID_RESP,
                _DUPLICATE_ID_RESP,
            ),
        ):
            with self.assertRaises(KeaException):
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")

    def test_partial_persist_error_carries_subnet_id(self):
        """PartialPersistError raised by subnet_add includes the assigned subnet_id."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_LIST_RESP,  # subnet4-list → ids 1, 2
                _SUBNET4_ADD_RESP,  # subnet4-add → Kea echoes id=10
                _CONFIG_GET_RUNNING_RESP,  # config-get (for config-test preflight)
                _CONFIG_TEST_OK_RESP,  # config-test
                [{"result": 1, "text": "write failed"}],  # config-write → fail
            ),
        ):
            with self.assertRaises(PartialPersistError) as ctx:
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        # The exception must carry the Kea-assigned ID (echoed back in _SUBNET4_ADD_RESP as 10)
        # so callers can still use it for follow-up operations (e.g. network assignment).
        self.assertEqual(ctx.exception.subnet_id, 10)

    def test_partial_persist_error_subnet_id_none_when_kea_does_not_echo_back(self):
        """PartialPersistError.subnet_id is None when list fails AND Kea echoes no id back."""
        # Must use a failed list call so no locally-computed id ends up in subnet_def
        list_error = [{"result": 2, "text": "unknown command"}]
        no_id_add_resp = [
            {"result": 0, "text": "Subnet added.", "arguments": {"subnets": [{"subnet": "10.99.0.0/24"}]}}
        ]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                list_error,  # subnet4-list fails → no locally-assigned id
                no_id_add_resp,  # subnet4-add → Kea does not echo id
                _CONFIG_GET_RUNNING_RESP,
                _CONFIG_TEST_OK_RESP,
                [{"result": 1, "text": "write failed"}],
            ),
        ):
            with self.assertRaises(PartialPersistError) as ctx:
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        self.assertIsNone(ctx.exception.subnet_id)


class TestSubnetDel(TestCase):
    """Tests for KeaClient.subnet_del()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _cmds(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"])["command"] for c in mock_post.call_args_list]

    def test_subnet_del_v4_sends_correct_command(self):
        """subnet4-del is sent for version=4."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET_DEL_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_del(version=4, subnet_id=5)
        self.assertIn("subnet4-del", self._cmds(mock_post))

    def test_subnet_del_v6_sends_correct_command(self):
        """subnet6-del is sent for version=6."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET_DEL_RESP, _CONFIG_GET_RUNNING_RESP_V6, _OK, _OK),
        ) as mock_post:
            self.client.subnet_del(version=6, subnet_id=7)
        self.assertIn("subnet6-del", self._cmds(mock_post))
        self.assertNotIn("subnet4-del", self._cmds(mock_post))

    def test_subnet_del_sends_correct_id(self):
        """subnet4-del payload contains the correct subnet ID."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET_DEL_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_del(version=4, subnet_id=42)
        del_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-del"
        )
        self.assertEqual(del_call["arguments"]["id"], 42)

    def test_subnet_del_calls_config_write(self):
        """config-write is called after subnet4-del."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET_DEL_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ) as mock_post:
            self.client.subnet_del(version=4, subnet_id=1)
        cmds = self._cmds(mock_post)
        self.assertIn("config-write", cmds)
        self.assertLess(cmds.index("subnet4-del"), cmds.index("config-write"))

    def test_subnet_del_raises_on_kea_error(self):
        """KeaException is raised when Kea returns result != 0."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects([{"result": 3, "text": "subnet not found"}]),
        ):
            with self.assertRaises(KeaException):
                self.client.subnet_del(version=4, subnet_id=999)

    def test_subnet_del_returns_none_on_success(self):
        """subnet_del returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET_DEL_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _OK),
        ):
            result = self.client.subnet_del(version=4, subnet_id=1)
        self.assertIsNone(result)


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


# ─────────────────────────────────────────────────────────────────────────────
# subnet_update
# ─────────────────────────────────────────────────────────────────────────────

_SUBNET_UPDATE_RESP = [{"result": 0, "text": "IPv4 subnet successfully updated"}]
_CONFIG_WRITE_RESP = [{"result": 0, "text": "Configuration written."}]


class TestSubnetUpdate(TestCase):
    """Tests for KeaClient.subnet_update()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [c.kwargs.get("json") or c[1]["json"] for c in mock_post.call_args_list]

    def _update_payload(self, mock_post):
        return next(p for p in self._payloads(mock_post) if "update" in p["command"])

    def test_sends_subnet4_update_command(self):
        """subnet4-update command is sent for version=4."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24")
        cmds = [p["command"] for p in self._payloads(mock_post)]
        self.assertIn("subnet4-update", cmds)

    def test_sends_subnet6_update_command(self):
        """subnet6-update command is sent for version=6."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET6_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=6, subnet_id=2, subnet_cidr="2001:db8::/48")
        cmds = [p["command"] for p in self._payloads(mock_post)]
        self.assertIn("subnet6-update", cmds)

    def test_includes_subnet_id_and_cidr(self):
        """The update payload must include both id and subnet fields."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=42, subnet_cidr="192.168.1.0/24")
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertEqual(subnet_obj["id"], 42)
        self.assertEqual(subnet_obj["subnet"], "192.168.1.0/24")

    def test_includes_pools_when_provided(self):
        """Pools are formatted as [{"pool": "..."}, ...] in the subnet object."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(
                version=4,
                subnet_id=1,
                subnet_cidr="10.0.0.0/24",
                pools=["10.0.0.100-10.0.0.200"],
            )
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertEqual(subnet_obj["pools"], [{"pool": "10.0.0.100-10.0.0.200"}])

    def test_sets_empty_pools_list_when_none(self):
        """When pools=None, pools key is absent (Kea keeps existing pools)."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", pools=None)
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertNotIn("pools", subnet_obj)

    def test_sets_explicit_empty_pools_when_empty_list(self):
        """When pools=[], the update sends pools:[] to remove all pools."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", pools=[])
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertEqual(subnet_obj["pools"], [])

    def test_includes_gateway_option_for_v4(self):
        """Gateway sets the 'routers' option-data entry for DHCPv4."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", gateway="10.0.0.1")
        payload = self._update_payload(mock_post)
        option_data = payload["arguments"]["subnet4"][0].get("option-data", [])
        routers = next((o for o in option_data if o["name"] == "routers"), None)
        self.assertIsNotNone(routers)
        self.assertEqual(routers["data"], "10.0.0.1")

    def test_includes_dns_servers_option(self):
        """DNS servers set the 'domain-name-servers' option for DHCPv4."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(
                version=4,
                subnet_id=1,
                subnet_cidr="10.0.0.0/24",
                dns_servers=["8.8.8.8", "1.1.1.1"],
            )
        payload = self._update_payload(mock_post)
        option_data = payload["arguments"]["subnet4"][0].get("option-data", [])
        dns = next((o for o in option_data if o["name"] == "domain-name-servers"), None)
        self.assertIsNotNone(dns)
        self.assertIn("8.8.8.8", dns["data"])

    def test_includes_valid_lft_when_provided(self):
        """valid_lft is included in the subnet object when not None."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", valid_lft=7200)
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertEqual(subnet_obj["valid-lft"], 7200)

    def test_calls_config_write_after_update(self):
        """config-write is called after subnet{v}-update to persist the change."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24")
        cmds = [p["command"] for p in self._payloads(mock_post)]
        self.assertIn("config-write", cmds)

    def test_raises_on_kea_error(self):
        """KeaException is raised when Kea returns a non-zero result."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects([{"result": 1, "text": "subnet not found"}]),
        ):
            with self.assertRaises(KeaException):
                self.client.subnet_update(version=4, subnet_id=99, subnet_cidr="10.0.0.0/24")

    def test_returns_none_on_success(self):
        """subnet_update returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ):
            result = self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24")
        self.assertIsNone(result)

    def test_sends_renew_timer_when_provided(self):
        """F11: subnet_update must include renew-timer in subnet payload when renew_timer is given."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", renew_timer=600)
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertEqual(subnet_obj["renew-timer"], 600)

    def test_sends_rebind_timer_when_provided(self):
        """F11: subnet_update must include rebind-timer in subnet payload when rebind_timer is given."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", rebind_timer=900)
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertEqual(subnet_obj["rebind-timer"], 900)

    def test_omits_renew_timer_when_not_provided(self):
        """F11: renew-timer must not appear in subnet payload when renew_timer=None."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24")
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertNotIn("renew-timer", subnet_obj)

    def test_omits_rebind_timer_when_not_provided(self):
        """F11: rebind-timer must not appear in subnet payload when rebind_timer=None."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24")
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertNotIn("rebind-timer", subnet_obj)

    def test_sends_ntp_servers_option_when_provided(self):
        """subnet_update must include ntp-servers option-data when ntp_servers is given."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", ntp_servers=["10.0.0.1"])
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        opt = next((o for o in subnet_obj.get("option-data", []) if o.get("name") == "ntp-servers"), None)
        self.assertIsNotNone(opt, "ntp-servers option-data entry not found")
        self.assertEqual(opt["data"], "10.0.0.1")

    def test_sends_min_valid_lft_when_provided(self):
        """subnet_update must include min-valid-lft when min_valid_lft is given."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", min_valid_lft=300)
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertEqual(subnet_obj["min-valid-lft"], 300)

    def test_sends_max_valid_lft_when_provided(self):
        """subnet_update must include max-valid-lft when max_valid_lft is given."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET, _SUBNET_UPDATE_RESP, _CONFIG_GET_RUNNING_RESP, _OK, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=1, subnet_cidr="10.0.0.0/24", max_valid_lft=7200)
        payload = self._update_payload(mock_post)
        subnet_obj = payload["arguments"]["subnet4"][0]
        self.assertEqual(subnet_obj["max-valid-lft"], 7200)


# ─────────────────────────────────────────────────────────────────────────────
# _persist_config — config-get → config-test(args) → config-write(args)
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_GET_RUNNING_RESP = [{"result": 0, "arguments": {"Dhcp4": {"valid-lifetime": 3600}, "hash": "abc123"}}]
_CONFIG_GET_RUNNING_RESP_V6 = [{"result": 0, "arguments": {"Dhcp6": {"valid-lifetime": 3600}, "hash": "abc123"}}]
_CONFIG_TEST_OK_RESP = [{"result": 0, "text": "Configuration seems OK."}]
_CONFIG_TEST_NOT_SUPPORTED_RESP = [{"result": 2, "text": "Command not supported."}]
_CONFIG_TEST_FAIL_RESP = [{"result": 1, "text": "Configuration check failed."}]


class TestPersistConfig(TestCase):
    """Tests for KeaClient._persist_config() — verifies config-get → config-test(args) → config-write(args) flow."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_flow_is_config_get_then_config_test_then_config_write(self):
        """_persist_config issues config-get, then config-test, then config-write in order."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client._persist_config("dhcp4")
        cmds = self._cmds(mock_post)
        self.assertEqual(cmds, ["config-get", "config-test", "config-write"])

    def test_config_test_receives_config_from_config_get(self):
        """config-test is called with the full arguments returned by config-get (hash stripped)."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client._persist_config("dhcp4")
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        expected_config = {k: v for k, v in _CONFIG_GET_RUNNING_RESP[0]["arguments"].items() if k != "hash"}
        self.assertEqual(test_payload["arguments"], expected_config)

    def test_config_write_receives_config_from_config_get(self):
        """config-write is called without arguments (Kea config-write only accepts optional filename)."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client._persist_config("dhcp4")
        payloads = self._payloads(mock_post)
        write_payload = next(p for p in payloads if p["command"] == "config-write")
        self.assertNotIn("arguments", write_payload)

    def test_config_test_not_supported_falls_through_to_config_write(self):
        """When config-test returns result=2 (not supported), config-write is still called."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_NOT_SUPPORTED_RESP, _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client._persist_config("dhcp4")
        self.assertIn("config-write", self._cmds(mock_post))

    def test_config_test_failure_raises_kea_config_persist_error(self):
        """When config-test (called with proper args) returns a non-zero, non-2 result, KeaConfigPersistError is raised.

        _persist_config() is called AFTER a native mutation is already live, so the running config
        has changed.  The appropriate exception is KeaConfigPersistError (change is live but
        config-write was skipped), not KeaConfigTestError (which means the mutation was not applied).
        """

        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_FAIL_RESP),
        ):
            with self.assertRaises(KeaConfigPersistError):
                self.client._persist_config("dhcp4")

    def test_config_write_not_called_when_config_test_fails(self):
        """When config-test returns an error, config-write is NOT called."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_FAIL_RESP),
        ) as mock_post:
            with self.assertRaises(KeaConfigPersistError):
                self.client._persist_config("dhcp4")
        self.assertNotIn("config-write", self._cmds(mock_post))

    def test_config_write_failure_raises_partial_persist_error(self):
        """When config-write fails, PartialPersistError is raised."""

        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, [{"result": 1, "text": "write failed"}]
            ),
        ):
            with self.assertRaises(PartialPersistError):
                self.client._persist_config("dhcp4")

    def test_correct_service_used_for_all_commands(self):
        """config-get, config-test, and config-write are all sent with the correct service."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_RUNNING_RESP_V6, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client._persist_config("dhcp6")
        payloads = self._payloads(mock_post)
        for payload in payloads:
            self.assertEqual(payload["service"], ["dhcp6"])
        # config-test arguments should reference Dhcp6, not Dhcp4
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        self.assertIn("Dhcp6", test_payload["arguments"])
        self.assertNotIn("Dhcp4", test_payload["arguments"])

    def test_config_get_failure_falls_back_to_no_args_config_write(self):
        """When config-get fails, config-test is skipped and config-write is called without arguments."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects([{"result": 1, "text": "config-get failed"}], _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client._persist_config("dhcp4")
        cmds = self._cmds(mock_post)
        self.assertIn("config-write", cmds)
        self.assertNotIn("config-test", cmds)
        write_payload = next(p for p in self._payloads(mock_post) if p["command"] == "config-write")
        self.assertNotIn("arguments", write_payload)

    def test_config_write_requests_exception_raises_partial_persist_error(self):
        """requests.RequestException from config-write is wrapped in PartialPersistError."""
        import requests as req

        def _side_effect(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            if cmd == "config-write":
                raise req.RequestException("connection reset")
            return _mock_http_response(_CONFIG_GET_RUNNING_RESP if cmd == "config-get" else _CONFIG_TEST_OK_RESP)

        with patch.object(self.client._session, "post", side_effect=_side_effect):
            with self.assertRaises(PartialPersistError):
                self.client._persist_config("dhcp4")

    def test_config_write_value_error_raises_partial_persist_error(self):
        """ValueError from config-write is wrapped in PartialPersistError."""

        def _side_effect(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            if cmd == "config-write":
                raise ValueError("bad JSON")
            return _mock_http_response(_CONFIG_GET_RUNNING_RESP if cmd == "config-get" else _CONFIG_TEST_OK_RESP)

        with patch.object(self.client._session, "post", side_effect=_side_effect):
            with self.assertRaises(PartialPersistError):
                self.client._persist_config("dhcp4")

    def test_config_test_transport_error_raises_kea_config_persist_error(self):
        """requests.RequestException from config-test aborts config-write and raises KeaConfigPersistError."""
        import requests as req

        def _side_effect(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            if cmd == "config-test":
                raise req.RequestException("timeout")
            return _mock_http_response(_CONFIG_GET_RUNNING_RESP)

        with patch.object(self.client._session, "post", side_effect=_side_effect):
            with self.assertRaises(KeaConfigPersistError):
                self.client._persist_config("dhcp4")

    def test_config_test_transport_error_does_not_call_config_write(self):
        """When config-test has a transport error, config-write must NOT be called."""
        import requests as req

        calls: list[str] = []

        def _side_effect(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            calls.append(cmd)
            if cmd == "config-test":
                raise req.RequestException("timeout")
            return _mock_http_response(_CONFIG_GET_RUNNING_RESP)

        with patch.object(self.client._session, "post", side_effect=_side_effect):
            with self.assertRaises(KeaConfigPersistError):
                self.client._persist_config("dhcp4")
        self.assertNotIn("config-write", calls)


# Minimal config-get response containing one v4 subnet with one existing option
_CONFIG_GET_WITH_SUBNET = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [
                    {
                        "id": 1,
                        "subnet": "10.0.0.0/24",
                        "option-data": [
                            {"name": "domain-name-servers", "data": "8.8.8.8"},
                        ],
                    }
                ]
            }
        },
    }
]
_CONFIG_GET_NO_SUBNET = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [
                    {"id": 99, "subnet": "192.168.0.0/24", "option-data": []},
                ]
            }
        },
    }
]


class TestSubnetOptionUpdate(TestCase):
    """Tests for KeaClient.subnet_update_options()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_calls_config_get_then_config_test_then_config_write(self):
        """subnet_update_options calls config-get, config-test, config-write in order."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SUBNET,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.subnet_update_options(version=4, subnet_id=1, options=[])
        cmds = self._cmds(mock_post)
        self.assertEqual(cmds, ["config-get", "config-test", "config-set", "config-write"])

    def test_replaces_option_data_in_config_write_payload(self):
        """config-set and config-write are called with updated option-data replacing the old list."""
        new_opts = [{"name": "routers", "data": "10.0.0.1"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SUBNET,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.subnet_update_options(version=4, subnet_id=1, options=new_opts)
        payloads = self._payloads(mock_post)
        # Both config-test and config-set must carry the same updated option-data
        for cmd in ("config-test", "config-set"):
            payload = next(p for p in payloads if p["command"] == cmd)
            subnet = payload["arguments"]["Dhcp4"]["subnet4"][0]
            self.assertEqual(subnet["option-data"], new_opts, f"{cmd} payload has wrong option-data")

    def test_clears_option_data_when_empty_list_given(self):
        """Passing options=[] removes all existing options from the subnet in config-test and config-set."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SUBNET,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.subnet_update_options(version=4, subnet_id=1, options=[])
        payloads = self._payloads(mock_post)
        for cmd in ("config-test", "config-set"):
            payload = next(p for p in payloads if p["command"] == cmd)
            subnet = payload["arguments"]["Dhcp4"]["subnet4"][0]
            self.assertEqual(subnet["option-data"], [], f"{cmd} payload should have empty option-data")

    def test_raises_kea_exception_when_subnet_id_not_found(self):
        """KeaException raised if subnet_id does not exist in config."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_NO_SUBNET),
        ):
            with self.assertRaises(KeaException):
                self.client.subnet_update_options(version=4, subnet_id=1, options=[])

    def test_raises_partial_persist_error_on_config_write_failure(self):
        """PartialPersistError raised when config-write fails after successful config-test."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SUBNET,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                [{"result": 1, "text": "write failed"}],
            ),
        ):
            with self.assertRaises(PartialPersistError):
                self.client.subnet_update_options(version=4, subnet_id=1, options=[])

    def test_skips_config_test_gracefully_when_not_supported(self):
        """If config-test returns result=2, config-set and config-write still proceed."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SUBNET,
                _CONFIG_TEST_NOT_SUPPORTED_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.subnet_update_options(version=4, subnet_id=1, options=[])
        # Must not raise; both config-set and config-write must have been called
        cmds = self._cmds(mock_post)
        self.assertIn("config-set", cmds)
        self.assertIn("config-write", cmds)

    def test_v6_uses_dhcp6_service_and_subnet6_key(self):
        """For version=6, config-get uses dhcp6 service and Dhcp6.subnet6 key."""
        config_get_v6 = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp6": {
                        "subnet6": [
                            {"id": 10, "subnet": "2001:db8::/32", "option-data": []},
                        ]
                    }
                },
            }
        ]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(config_get_v6, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client.subnet_update_options(version=6, subnet_id=10, options=[])
        payloads = self._payloads(mock_post)
        get_payload = payloads[0]
        self.assertEqual(get_payload["service"], ["dhcp6"])
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        self.assertIn("Dhcp6", test_payload["arguments"])

    def test_returns_none_on_success(self):
        """subnet_update_options returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SUBNET, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ):
            result = self.client.subnet_update_options(version=4, subnet_id=1, options=[])
        self.assertIsNone(result)

    def test_raises_kea_config_test_error_on_config_test_failure(self):
        """subnet_update_options raises KeaConfigTestError when config-test returns result=1."""

        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_WITH_SUBNET, _CONFIG_TEST_FAIL_RESP),
        ):
            with self.assertRaises(KeaConfigTestError):
                self.client.subnet_update_options(version=4, subnet_id=1, options=[])

    def test_finds_subnet_inside_shared_network(self):
        """subnet_update_options locates subnet inside shared-networks when not at top-level."""
        config_with_shared_net = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [
                            {
                                "name": "prod",
                                "subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "option-data": []}],
                            }
                        ],
                    }
                },
            }
        ]
        new_options = [{"name": "routers", "data": "10.0.0.1"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                config_with_shared_net, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.subnet_update_options(version=4, subnet_id=1, options=new_options)
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        shared_nets = test_payload["arguments"]["Dhcp4"]["shared-networks"]
        subnet_opts = shared_nets[0]["subnet4"][0]["option-data"]
        self.assertEqual(subnet_opts, new_options)


# TestServerOptionsUpdate
# ---------------------------------------------------------------------------

# Minimal config-get response with server-level option-data for v4
_SERVER_CONFIG_GET_V4 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "option-data": [
                    {"name": "domain-name-servers", "data": "8.8.8.8"},
                ],
                "subnet4": [],
            }
        },
    }
]


class TestServerOptionsUpdate(TestCase):
    """Tests for KeaClient.server_update_options()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_calls_config_get_then_config_test_then_config_write(self):
        """server_update_options calls config-get, config-test, config-write in order."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SERVER_CONFIG_GET_V4,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.server_update_options(version=4, options=[])
        self.assertEqual(self._cmds(mock_post), ["config-get", "config-test", "config-set", "config-write"])

    def test_replaces_option_data_in_config_test_payload(self):
        """config-test is called with updated Dhcp4.option-data replacing the old list."""
        new_opts = [{"name": "routers", "data": "10.0.0.1"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SERVER_CONFIG_GET_V4,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.server_update_options(version=4, options=new_opts)
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        self.assertEqual(test_payload["arguments"]["Dhcp4"]["option-data"], new_opts)

    def test_clears_option_data_when_empty_list_given(self):
        """Passing options=[] removes all existing server-level options."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SERVER_CONFIG_GET_V4,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.server_update_options(version=4, options=[])
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        self.assertEqual(test_payload["arguments"]["Dhcp4"]["option-data"], [])

    def test_raises_partial_persist_error_on_config_write_failure(self):
        """PartialPersistError raised when config-write fails after successful config-test."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SERVER_CONFIG_GET_V4,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                [{"result": 1, "text": "write failed"}],
            ),
        ):
            with self.assertRaises(PartialPersistError):
                self.client.server_update_options(version=4, options=[])

    def test_skips_config_test_gracefully_when_not_supported(self):
        """If config-test returns result=2, config-set and config-write still proceed."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SERVER_CONFIG_GET_V4,
                _CONFIG_TEST_NOT_SUPPORTED_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.server_update_options(version=4, options=[])
        cmds = self._cmds(mock_post)
        self.assertIn("config-set", cmds)
        self.assertIn("config-write", cmds)

    def test_v6_uses_dhcp6_service_and_dhcp6_key(self):
        """For version=6, config-get uses dhcp6 service and Dhcp6 key."""
        config_get_v6 = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp6": {
                        "option-data": [],
                        "subnet6": [],
                    }
                },
            }
        ]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(config_get_v6, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client.server_update_options(version=6, options=[])
        payloads = self._payloads(mock_post)
        self.assertEqual(payloads[0]["service"], ["dhcp6"])
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        self.assertIn("Dhcp6", test_payload["arguments"])

    def test_returns_none_on_success(self):
        """server_update_options returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SERVER_CONFIG_GET_V4, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ):
            result = self.client.server_update_options(version=4, options=[])
        self.assertIsNone(result)

    def test_raises_kea_config_test_error_on_config_test_failure(self):
        """server_update_options raises KeaConfigTestError when config-test returns result=1."""

        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SERVER_CONFIG_GET_V4, _CONFIG_TEST_FAIL_RESP),
        ):
            with self.assertRaises(KeaConfigTestError):
                self.client.server_update_options(version=4, options=[])


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
# TestNetworkAdd
# ---------------------------------------------------------------------------

_NETWORK_ADD_OK = [{"result": 0, "text": "shared network added."}]
_NETWORK_ADD_FAIL = [{"result": 1, "text": "duplicate network name"}]


class TestNetworkAdd(TestCase):
    """Tests for KeaClient.network_add(version, name, options) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_sends_correct_command_and_service(self):
        """network4-add is sent with service dhcp4 and the correct network name."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_ADD_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_add(version=4, name="prod-net")
        payload = next(p for p in self._payloads(mock_post) if p["command"] == "network4-add")
        self.assertEqual(payload["service"], ["dhcp4"])
        self.assertEqual(payload["arguments"]["shared-networks"][0]["name"], "prod-net")

    def test_options_included_when_provided(self):
        """When options are provided, option-data is included in the network payload."""
        opts = [{"name": "domain-name-servers", "data": "8.8.8.8"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_ADD_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_add(version=4, name="opt-net", options=opts)
        payload = next(p for p in self._payloads(mock_post) if p["command"] == "network4-add")
        self.assertEqual(payload["arguments"]["shared-networks"][0]["option-data"], opts)

    def test_persist_config_called_after_network_add(self):
        """config-write is called after network4-add succeeds."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_ADD_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_add(version=4, name="my-net")
        self.assertIn("config-write", self._cmds(mock_post))

    def test_raises_kea_exception_on_add_failure(self):
        """KeaException is raised when network4-add returns a non-zero result."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_NETWORK_ADD_FAIL)):
            with self.assertRaises(KeaException):
                self.client.network_add(version=4, name="dup-net")


# ---------------------------------------------------------------------------
# TestNetworkDel
# ---------------------------------------------------------------------------

_NETWORK_DEL_OK = [{"result": 0, "text": "shared network deleted."}]
_NETWORK_DEL_FAIL = [{"result": 1, "text": "network not found"}]


class TestNetworkDel(TestCase):
    """Tests for KeaClient.network_del(version, name) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_sends_correct_command_and_name(self):
        """network4-del is sent with service dhcp4 and the correct network name."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_DEL_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_del(version=4, name="old-net")
        payload = next(p for p in self._payloads(mock_post) if p["command"] == "network4-del")
        self.assertEqual(payload["service"], ["dhcp4"])
        self.assertEqual(payload["arguments"]["name"], "old-net")

    def test_persist_config_called_after_del(self):
        """config-write is called after network4-del succeeds."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_DEL_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_del(version=4, name="old-net")
        self.assertIn("config-write", self._cmds(mock_post))

    def test_raises_kea_exception_on_failure(self):
        """KeaException is raised when network4-del returns a non-zero result."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_NETWORK_DEL_FAIL)):
            with self.assertRaises(KeaException):
                self.client.network_del(version=4, name="missing-net")


# ---------------------------------------------------------------------------
# TestNetworkSubnetAdd
# ---------------------------------------------------------------------------

_NETWORK_SUBNET_ADD_OK = [{"result": 0, "text": "Subnet added to shared network."}]
_NETWORK_SUBNET_ADD_FAIL = [{"result": 1, "text": "subnet not found"}]


class TestNetworkSubnetAdd(TestCase):
    """Tests for KeaClient.network_subnet_add(version, name, subnet_id) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_sends_correct_command_and_args(self):
        """network4-subnet-add is sent with name and id in arguments."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_SUBNET_ADD_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_subnet_add(version=4, name="prod-net", subnet_id=5)
        payload = next(p for p in self._payloads(mock_post) if p["command"] == "network4-subnet-add")
        self.assertEqual(payload["service"], ["dhcp4"])
        self.assertEqual(payload["arguments"]["name"], "prod-net")
        self.assertEqual(payload["arguments"]["id"], 5)

    def test_persist_config_called_after_subnet_add(self):
        """config-write is called after network4-subnet-add succeeds."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_SUBNET_ADD_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_subnet_add(version=4, name="prod-net", subnet_id=5)
        self.assertIn("config-write", self._cmds(mock_post))

    def test_raises_kea_exception_on_failure(self):
        """KeaException is raised when the command returns a non-zero result."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_NETWORK_SUBNET_ADD_FAIL)):
            with self.assertRaises(KeaException):
                self.client.network_subnet_add(version=4, name="prod-net", subnet_id=99)


# ---------------------------------------------------------------------------
# TestNetworkSubnetDel
# ---------------------------------------------------------------------------

_NETWORK_SUBNET_DEL_OK = [{"result": 0, "text": "Subnet removed from shared network."}]
_NETWORK_SUBNET_DEL_FAIL = [{"result": 1, "text": "subnet not found in network"}]


class TestNetworkSubnetDel(TestCase):
    """Tests for KeaClient.network_subnet_del(version, name, subnet_id) -> None."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_sends_correct_command_and_args(self):
        """network4-subnet-del is sent with name and id in arguments."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_SUBNET_DEL_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_subnet_del(version=4, name="prod-net", subnet_id=5)
        payload = next(p for p in self._payloads(mock_post) if p["command"] == "network4-subnet-del")
        self.assertEqual(payload["service"], ["dhcp4"])
        self.assertEqual(payload["arguments"]["name"], "prod-net")
        self.assertEqual(payload["arguments"]["id"], 5)

    def test_persist_config_called_after_subnet_del(self):
        """config-write is called after network4-subnet-del succeeds."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _NETWORK_SUBNET_DEL_OK, _CONFIG_GET_RUNNING_RESP, _CONFIG_TEST_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.network_subnet_del(version=4, name="prod-net", subnet_id=5)
        self.assertIn("config-write", self._cmds(mock_post))

    def test_raises_kea_exception_on_failure(self):
        """KeaException is raised when the command returns a non-zero result."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_NETWORK_SUBNET_DEL_FAIL)):
            with self.assertRaises(KeaException):
                self.client.network_subnet_del(version=4, name="prod-net", subnet_id=99)


# ---------------------------------------------------------------------------
# Fixtures for option-def tests
# ---------------------------------------------------------------------------

_OPTION_DEF_CONFIG_V4 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "option-def": [
                    {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4"},
                ],
            }
        },
    }
]

_OPTION_DEF_CONFIG_V6 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp6": {
                "option-def": [
                    {"name": "my-v6-opt", "code": 201, "type": "uint32", "space": "dhcp6"},
                ],
            }
        },
    }
]

_OPTION_DEF_CONFIG_EMPTY = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {},
        },
    }
]


class TestOptionDefList(TestCase):
    """Tests for KeaClient.option_def_list()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_returns_option_def_list_v4(self):
        """option_def_list(4) returns the Dhcp4.option-def list from config."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_OPTION_DEF_CONFIG_V4)):
            result = self.client.option_def_list(version=4)
        self.assertEqual(result, [{"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4"}])

    def test_returns_option_def_list_v6(self):
        """option_def_list(6) returns the Dhcp6.option-def list."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_OPTION_DEF_CONFIG_V6)):
            result = self.client.option_def_list(version=6)
        self.assertEqual(result, [{"name": "my-v6-opt", "code": 201, "type": "uint32", "space": "dhcp6"}])

    def test_returns_empty_list_when_no_option_def_key(self):
        """Returns [] when Dhcp4 has no 'option-def' key."""
        with patch.object(self.client._session, "post", return_value=_mock_http_response(_OPTION_DEF_CONFIG_EMPTY)):
            result = self.client.option_def_list(version=4)
        self.assertEqual(result, [])

    def test_calls_config_get_on_correct_service(self):
        """option_def_list(4) sends config-get to the dhcp4 service."""

        def _payloads(mock_post):
            return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_OPTION_DEF_CONFIG_V4),
        ) as mock_post:
            self.client.option_def_list(version=4)
        payload = _payloads(mock_post)[0]
        self.assertEqual(payload["command"], "config-get")
        self.assertEqual(payload["service"], ["dhcp4"])


class TestOptionDefAdd(TestCase):
    """Tests for KeaClient.option_def_add()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_calls_config_get_test_write_in_order(self):
        """option_def_add calls config-get, config-test, config-write in order."""
        new_def = {"name": "new-opt", "code": 201, "type": "string", "space": "dhcp4"}
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_V4, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.option_def_add(version=4, option_def=new_def)
        self.assertEqual(self._cmds(mock_post), ["config-get", "config-test", "config-set", "config-write"])

    def test_appends_new_def_to_existing_list(self):
        """New option-def is appended to the existing list in config-test payload."""
        new_def = {"name": "new-opt", "code": 201, "type": "string", "space": "dhcp4"}
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_V4, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.option_def_add(version=4, option_def=new_def)
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        defs = test_payload["arguments"]["Dhcp4"]["option-def"]
        self.assertEqual(len(defs), 2)
        self.assertIn(new_def, defs)

    def test_creates_option_def_key_when_absent(self):
        """When Dhcp4 has no 'option-def' key, option_def_add creates it."""
        new_def = {"name": "first-opt", "code": 202, "type": "uint8", "space": "dhcp4"}
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_EMPTY, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.option_def_add(version=4, option_def=new_def)
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        defs = test_payload["arguments"]["Dhcp4"]["option-def"]
        self.assertEqual(defs, [new_def])

    def test_raises_partial_persist_error_on_config_write_failure(self):
        """PartialPersistError raised when config-write fails after successful config-test."""
        new_def = {"name": "new-opt", "code": 201, "type": "string", "space": "dhcp4"}
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_V4, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, [{"result": 1, "text": "fail"}]
            ),
        ):
            with self.assertRaises(PartialPersistError):
                self.client.option_def_add(version=4, option_def=new_def)

    def test_raises_kea_exception_on_config_test_failure(self):
        """KeaException raised when config-test rejects the new option-def."""
        new_def = {"name": "bad-opt", "code": 99, "type": "string", "space": "dhcp4"}
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_OPTION_DEF_CONFIG_V4, [{"result": 1, "text": "bad config"}]),
        ):
            with self.assertRaises(KeaException):
                self.client.option_def_add(version=4, option_def=new_def)

    def test_v6_uses_dhcp6_service_and_dhcp6_key(self):
        """For version=6, option_def_add targets dhcp6 service and Dhcp6 key."""
        new_def = {"name": "v6-opt", "code": 250, "type": "ipv6-address", "space": "dhcp6"}
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_V6, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.option_def_add(version=6, option_def=new_def)
        payloads = self._payloads(mock_post)
        get_payload = next(p for p in payloads if p["command"] == "config-get")
        self.assertEqual(get_payload["service"], ["dhcp6"])
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        self.assertIn("Dhcp6", test_payload["arguments"])

    def test_skips_config_test_gracefully_when_not_supported(self):
        """When config-test returns result=2 (not supported), option_def_add skips pre-flight and still config-sets and config-writes."""
        new_def = {"name": "new-opt", "code": 201, "type": "string", "space": "dhcp4"}
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_V4, _CONFIG_TEST_NOT_SUPPORTED_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.option_def_add(version=4, option_def=new_def)
        cmds = self._cmds(mock_post)
        self.assertIn("config-set", cmds)
        self.assertIn("config-write", cmds)


class TestOptionDefDel(TestCase):
    """Tests for KeaClient.option_def_del()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_calls_config_get_test_write_in_order(self):
        """option_def_del calls config-get, config-test, config-write in order."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_V4, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.option_def_del(version=4, code=200, space="dhcp4")
        self.assertEqual(self._cmds(mock_post), ["config-get", "config-test", "config-set", "config-write"])

    def test_removes_matching_option_def(self):
        """option_def_del removes the entry with matching code+space from config."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_V4, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP
            ),
        ) as mock_post:
            self.client.option_def_del(version=4, code=200, space="dhcp4")
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        defs = test_payload["arguments"]["Dhcp4"]["option-def"]
        self.assertEqual(defs, [])

    def test_raises_kea_exception_when_not_found(self):
        """KeaException raised when code+space not found in option-def list."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_OPTION_DEF_CONFIG_V4),
        ):
            with self.assertRaises(KeaException):
                self.client.option_def_del(version=4, code=999, space="dhcp4")

    def test_raises_partial_persist_error_on_config_write_failure(self):
        """PartialPersistError raised when config-write fails."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _OPTION_DEF_CONFIG_V4, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, [{"result": 1, "text": "fail"}]
            ),
        ):
            with self.assertRaises(PartialPersistError):
                self.client.option_def_del(version=4, code=200, space="dhcp4")

    def test_does_not_remove_entry_with_different_space(self):
        """option_def_del only removes entries matching both code AND space."""
        config_two_spaces = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "option-def": [
                            {"name": "opt-a", "code": 200, "type": "string", "space": "dhcp4"},
                            {"name": "opt-b", "code": 200, "type": "string", "space": "myspace"},
                        ],
                    }
                },
            }
        ]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(config_two_spaces, _CONFIG_TEST_OK_RESP, _CONFIG_SET_OK_RESP, _CONFIG_WRITE_RESP),
        ) as mock_post:
            self.client.option_def_del(version=4, code=200, space="dhcp4")
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        defs = test_payload["arguments"]["Dhcp4"]["option-def"]
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0]["space"], "myspace")

    def test_raises_kea_config_test_error_on_config_test_failure(self):
        """KeaConfigTestError raised when config-test returns result=1 (invalid config after del)."""

        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_OPTION_DEF_CONFIG_V4, _CONFIG_TEST_FAIL_RESP),
        ):
            with self.assertRaises(KeaConfigTestError):
                self.client.option_def_del(version=4, code=200, space="dhcp4")


# ─────────────────────────────────────────────────────────────────────────────
# TestNetworkUpdate (F2b)
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_SET_OK_RESP = [{"result": 0, "text": "Configuration set."}]

_CONFIG_GET_WITH_SHARED_NETWORK = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "shared-networks": [
                    {
                        "name": "prod-net",
                        "description": "Old description",
                        "option-data": [],
                        "subnet4": [],
                    },
                    {
                        "name": "other-net",
                        "description": "Unrelated network",
                        "option-data": [],
                        "subnet4": [],
                    },
                ],
                "subnet4": [],
            }
        },
    }
]

_CONFIG_GET_NO_SHARED_NETWORK = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "shared-networks": [],
                "subnet4": [],
            }
        },
    }
]


class TestNetworkUpdate(TestCase):
    """Tests for KeaClient.network_update() — read-modify-write for shared networks."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payloads(self, mock_post):
        return [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]

    def _cmds(self, mock_post):
        return [p["command"] for p in self._payloads(mock_post)]

    def test_calls_config_get_then_config_test_then_config_set_then_config_write(self):
        """network_update issues config-get, config-test, config-set, config-write in order."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SHARED_NETWORK,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.network_update(version=4, name="prod-net")
        self.assertEqual(self._cmds(mock_post), ["config-get", "config-test", "config-set", "config-write"])

    def test_updates_description_in_config_write_payload(self):
        """config-test and config-set payloads both contain the updated description."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SHARED_NETWORK,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.network_update(version=4, name="prod-net", description="New description")
        payloads = self._payloads(mock_post)
        for cmd in ("config-test", "config-set"):
            payload = next(p for p in payloads if p["command"] == cmd)
            networks = payload["arguments"]["Dhcp4"]["shared-networks"]
            target = next(n for n in networks if n["name"] == "prod-net")
            other = next(n for n in networks if n["name"] == "other-net")
            self.assertEqual(target["description"], "New description")
            self.assertNotEqual(other.get("description"), "New description")

    def test_updates_relay_addresses(self):
        """relay_addresses list is stored under network['relay']['ip-addresses'] in both config-test and config-set."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SHARED_NETWORK,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.network_update(version=4, name="prod-net", relay_addresses=["10.0.0.1"])
        payloads = self._payloads(mock_post)
        for cmd in ("config-test", "config-set"):
            payload = next(p for p in payloads if p["command"] == cmd)
            networks = payload["arguments"]["Dhcp4"]["shared-networks"]
            target = next(n for n in networks if n["name"] == "prod-net")
            other = next(n for n in networks if n["name"] == "other-net")
            self.assertEqual(target["relay"]["ip-addresses"], ["10.0.0.1"])
            self.assertIsNone(other.get("relay"))

    def test_raises_kea_exception_when_network_not_found(self):
        """KeaException raised if the named shared network is absent from the config."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_CONFIG_GET_NO_SHARED_NETWORK),
        ):
            with self.assertRaises(KeaException):
                self.client.network_update(version=4, name="prod-net")

    def test_raises_partial_persist_error_on_config_write_failure(self):
        """PartialPersistError raised when config-write fails after successful config-set."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SHARED_NETWORK,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                [{"result": 1, "text": "write failed"}],
            ),
        ):
            with self.assertRaises(PartialPersistError):
                self.client.network_update(version=4, name="prod-net")

    def test_skips_config_test_gracefully_when_not_supported(self):
        """If config-test returns result=2, config-set and config-write still proceed."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SHARED_NETWORK,
                _CONFIG_TEST_NOT_SUPPORTED_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.network_update(version=4, name="prod-net")
        cmds = self._cmds(mock_post)
        self.assertIn("config-set", cmds)
        self.assertIn("config-write", cmds)

    def test_updates_interface_in_payload(self):
        """interface field is set on the network in config-test and config-set payloads."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SHARED_NETWORK,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.network_update(version=4, name="prod-net", interface="eth1")
        payloads = self._payloads(mock_post)
        for cmd in ("config-test", "config-set"):
            payload = next(p for p in payloads if p["command"] == cmd)
            networks = payload["arguments"]["Dhcp4"]["shared-networks"]
            target = next(n for n in networks if n["name"] == "prod-net")
            other = next(n for n in networks if n["name"] == "other-net")
            self.assertEqual(target["interface"], "eth1")
            self.assertNotEqual(other.get("interface"), "eth1")

    def test_clears_relay_addresses_when_empty_list_provided(self):
        """Passing an empty relay_addresses list removes the 'relay' key from the network."""
        config_with_relay = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {
                                "name": "prod-net",
                                "relay": {"ip-addresses": ["10.0.0.1"]},
                                "option-data": [],
                                "subnet4": [],
                            },
                            {
                                "name": "other-net",
                                "relay": {"ip-addresses": ["10.0.0.2"]},
                                "option-data": [],
                                "subnet4": [],
                            },
                        ],
                        "subnet4": [],
                    }
                },
            }
        ]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                config_with_relay,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.network_update(version=4, name="prod-net", relay_addresses=[])
        payloads = self._payloads(mock_post)
        for cmd in ("config-test", "config-set"):
            payload = next(p for p in payloads if p["command"] == cmd)
            networks = payload["arguments"]["Dhcp4"]["shared-networks"]
            target = next(n for n in networks if n["name"] == "prod-net")
            other = next(n for n in networks if n["name"] == "other-net")
            self.assertNotIn("relay", target)
            # The other network's relay must be untouched.
            self.assertIn("relay", other)

    def test_updates_options_in_payload(self):
        """options list is written to 'option-data' on the network in config-test and config-set payloads."""
        new_options = [{"name": "domain-name-servers", "data": "8.8.8.8"}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SHARED_NETWORK,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.network_update(version=4, name="prod-net", options=new_options)
        payloads = self._payloads(mock_post)
        for cmd in ("config-test", "config-set"):
            payload = next(p for p in payloads if p["command"] == cmd)
            networks = payload["arguments"]["Dhcp4"]["shared-networks"]
            target = next(n for n in networks if n["name"] == "prod-net")
            other = next(n for n in networks if n["name"] == "other-net")
            self.assertEqual(target["option-data"], new_options)
            # The other network's option-data must be untouched.
            self.assertNotEqual(other.get("option-data"), new_options)

    def test_config_test_failure_raises_kea_config_test_error(self):
        """Non-2 config-test failure raises KeaConfigTestError (not PartialPersistError)."""

        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _CONFIG_GET_WITH_SHARED_NETWORK,
                _CONFIG_TEST_FAIL_RESP,
            ),
        ):
            with self.assertRaises(KeaConfigTestError):
                self.client.network_update(version=4, name="prod-net")

    def test_hash_key_stripped_from_config_before_config_test(self):
        """The 'hash' key present in Kea 2.4+ config-get responses is stripped before config-test."""
        config_with_hash = [
            {
                "result": 0,
                "arguments": {
                    "hash": "abc123",
                    "Dhcp4": {
                        "shared-networks": [{"name": "prod-net", "option-data": [], "subnet4": []}],
                        "subnet4": [],
                    },
                },
            }
        ]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                config_with_hash,
                _CONFIG_TEST_OK_RESP,
                _CONFIG_SET_OK_RESP,
                _CONFIG_WRITE_RESP,
            ),
        ) as mock_post:
            self.client.network_update(version=4, name="prod-net")
        payloads = self._payloads(mock_post)
        test_payload = next(p for p in payloads if p["command"] == "config-test")
        self.assertNotIn("hash", test_payload["arguments"])

    def test_config_set_transport_error_raises_ambiguous_config_set_error(self):
        """requests.RequestException from config-set is wrapped in AmbiguousConfigSetError (ambiguous state)."""
        import requests as req

        def _side_effect(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            if cmd == "config-set":
                raise req.RequestException("connection dropped")
            return _mock_http_response(_CONFIG_GET_WITH_SHARED_NETWORK if cmd == "config-get" else _CONFIG_TEST_OK_RESP)

        with patch.object(self.client._session, "post", side_effect=_side_effect):
            with self.assertRaises(AmbiguousConfigSetError):
                self.client.network_update(version=4, name="prod-net")

    def test_config_set_transport_error_does_not_call_config_write(self):
        """When config-set has a transport error, config-write must NOT be called."""
        import requests as req

        calls: list[str] = []

        def _side_effect(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            calls.append(cmd)
            if cmd == "config-set":
                raise req.RequestException("connection dropped")
            return _mock_http_response(_CONFIG_GET_WITH_SHARED_NETWORK if cmd == "config-get" else _CONFIG_TEST_OK_RESP)

        with patch.object(self.client._session, "post", side_effect=_side_effect):
            with self.assertRaises(PartialPersistError):
                self.client.network_update(version=4, name="prod-net")
        self.assertNotIn("config-write", calls)


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


# ---------------------------------------------------------------------------
# TestReservationGetByIp
# ---------------------------------------------------------------------------


class TestReservationGetByIp(TestCase):
    """Tests for KeaClient.reservation_get_by_ip()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _payload(self, mock_post, call_index):
        return (
            mock_post.call_args_list[call_index].kwargs.get("json") or mock_post.call_args_list[call_index][1]["json"]
        )

    _LIST4_RESP = [{"result": 0, "arguments": {"subnets": [{"id": 1, "subnet": "10.0.0.0/24"}]}}]
    _LIST6_RESP = [{"result": 0, "arguments": {"subnets": [{"id": 5, "subnet": "2001:db8::/64"}]}}]
    _RESERVATION_FOUND = [
        {
            "result": 0,
            "arguments": {
                "ip-address": "10.0.0.5",
                "hw-address": "aa:bb:cc:00:00:01",
                "hostname": "myhost",
                "subnet-id": 1,
            },
        }
    ]
    _RESERVATION_NOT_FOUND = [{"result": 3, "text": "Host not found."}]

    def test_returns_reservation_when_ip_in_subnet(self):
        """Returns the reservation dict when the IP is in a matching subnet."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(self._LIST4_RESP, self._RESERVATION_FOUND),
        ):
            result = self.client.reservation_get_by_ip(4, "10.0.0.5")
        self.assertIsNotNone(result)
        self.assertEqual(result["ip-address"], "10.0.0.5")

    def test_returns_none_when_ip_not_in_any_subnet(self):
        """Returns None when no subnet CIDR contains the IP — no reservation-get is called."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(self._LIST4_RESP),
        ) as mock_post:
            result = self.client.reservation_get_by_ip(4, "192.168.99.1")
        self.assertIsNone(result)
        self.assertEqual(len(mock_post.call_args_list), 1)  # only subnet4-list called

    def test_returns_none_when_reservation_not_found_in_subnet(self):
        """Returns None when subnet matches the IP but reservation-get returns result=3."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(self._LIST4_RESP, self._RESERVATION_NOT_FOUND),
        ):
            result = self.client.reservation_get_by_ip(4, "10.0.0.99")
        self.assertIsNone(result)

    def test_v4_calls_subnet4_list_and_dhcp4_service(self):
        """Uses subnet4-list and dhcp4 service for version=4."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(self._LIST4_RESP, self._RESERVATION_FOUND),
        ) as mock_post:
            self.client.reservation_get_by_ip(4, "10.0.0.5")
        first = self._payload(mock_post, 0)
        self.assertEqual(first["command"], "subnet4-list")
        self.assertEqual(first["service"], ["dhcp4"])

    def test_v6_calls_subnet6_list_and_dhcp6_service(self):
        """Uses subnet6-list and dhcp6 service for version=6."""
        res6_found = [{"result": 0, "arguments": {"ip-addresses": ["2001:db8::1"], "subnet-id": 5}}]
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(self._LIST6_RESP, res6_found),
        ) as mock_post:
            self.client.reservation_get_by_ip(6, "2001:db8::1")
        first = self._payload(mock_post, 0)
        self.assertEqual(first["command"], "subnet6-list")
        self.assertEqual(first["service"], ["dhcp6"])

    def test_reservation_get_called_with_correct_subnet_id(self):
        """Calls reservation-get with the subnet-id of the matching subnet."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(self._LIST4_RESP, self._RESERVATION_FOUND),
        ) as mock_post:
            self.client.reservation_get_by_ip(4, "10.0.0.5")
        second = self._payload(mock_post, 1)
        self.assertEqual(second["command"], "reservation-get")
        self.assertEqual(second["arguments"]["subnet-id"], 1)
        self.assertEqual(second["arguments"]["ip-address"], "10.0.0.5")

    def test_propagates_kea_exception_from_subnet_list(self):
        """Propagates KeaException when subnet4-list itself fails."""
        error_resp = [{"result": 1, "text": "command not supported"}]
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(error_resp),
        ):
            from netbox_kea.kea import KeaException

            with self.assertRaises(KeaException):
                self.client.reservation_get_by_ip(4, "10.0.0.5")


# ---------------------------------------------------------------------------
# TestPersistConfig — additional exception-type coverage
# ---------------------------------------------------------------------------


class TestPersistConfigExceptionTypes(TestCase):
    """Tests that requests.RequestException and ValueError in config-get fall back to config-write."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _cmds(self, mock_post):
        payloads = [(c.kwargs.get("json") or c[1]["json"]) for c in mock_post.call_args_list]
        return [p["command"] for p in payloads]

    def test_requests_request_exception_falls_back_to_config_write(self):
        """A requests.RequestException from config-get causes fallback to bare config-write."""
        import requests as req

        def _side_effect(url, **kwargs):
            if kwargs.get("json", {}).get("command") == "config-get":
                raise req.RequestException("network error")
            return _mock_http_response(_CONFIG_WRITE_RESP)

        with patch.object(self.client._session, "post", side_effect=_side_effect) as mock_post:
            self.client._persist_config("dhcp4")
        cmds = self._cmds(mock_post)
        self.assertIn("config-write", cmds)
        self.assertNotIn("config-test", cmds)

    def test_value_error_falls_back_to_config_write(self):
        """A ValueError from config-get causes fallback to bare config-write."""

        def _side_effect(url, **kwargs):
            if kwargs.get("json", {}).get("command") == "config-get":
                raise ValueError("unexpected value")
            return _mock_http_response(_CONFIG_WRITE_RESP)

        with patch.object(self.client._session, "post", side_effect=_side_effect) as mock_post:
            self.client._persist_config("dhcp4")
        cmds = self._cmds(mock_post)
        self.assertIn("config-write", cmds)
        self.assertNotIn("config-test", cmds)


# ---------------------------------------------------------------------------
# TestGetSubnetCidr
# ---------------------------------------------------------------------------


class TestGetSubnetCidr(TestCase):
    """Tests for KeaClient._get_subnet_cidr()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_returns_cidr_for_known_subnet(self):
        """_get_subnet_cidr returns the CIDR string from the subnet4-get response."""
        resp = [{"result": 0, "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)):
            cidr = self.client._get_subnet_cidr(version=4, subnet_id=1)
        self.assertEqual(cidr, "10.0.0.0/24")

    def test_raises_kea_exception_when_subnet_not_in_response(self):
        """_get_subnet_cidr raises KeaException when subnet4-get returns empty subnets list."""
        resp = [{"result": 0, "arguments": {"subnet4": []}}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)):
            with self.assertRaises(KeaException):
                self.client._get_subnet_cidr(version=4, subnet_id=999)

    def test_raises_kea_exception_when_arguments_missing(self):
        """_get_subnet_cidr raises KeaException when arguments key is absent."""
        resp = [{"result": 0, "arguments": {}}]
        with patch.object(self.client._session, "post", return_value=_mock_http_response(resp)):
            with self.assertRaises(KeaException):
                self.client._get_subnet_cidr(version=4, subnet_id=42)


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
                    "valid-lft": 3600,
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
# TestFindSubnetIdByCidr
# ---------------------------------------------------------------------------

_CONFIG_GET_WITH_SUBNETS_RESP = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [
                    {"id": 5, "subnet": "10.0.0.0/24"},
                    {"id": 6, "subnet": "192.168.1.0/24"},
                ],
                "shared-networks": [
                    {
                        "name": "prod",
                        "subnet4": [{"id": 99, "subnet": "172.16.0.0/16"}],
                    }
                ],
            }
        },
    }
]
_CONFIG_GET_EMPTY_RESP = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [],
                "shared-networks": [],
            }
        },
    }
]


class TestFindSubnetIdByCidr(TestCase):
    """Tests for KeaClient._find_subnet_id_by_cidr()."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_finds_subnet_at_top_level(self):
        """_find_subnet_id_by_cidr returns the id for a top-level subnet."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_CONFIG_GET_WITH_SUBNETS_RESP),
        ):
            result = self.client._find_subnet_id_by_cidr(version=4, cidr="10.0.0.0/24")
        self.assertEqual(result, 5)

    def test_finds_subnet_inside_shared_network(self):
        """_find_subnet_id_by_cidr finds subnets nested inside shared-networks."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_CONFIG_GET_WITH_SUBNETS_RESP),
        ):
            result = self.client._find_subnet_id_by_cidr(version=4, cidr="172.16.0.0/16")
        self.assertEqual(result, 99)

    def test_returns_none_when_not_found(self):
        """_find_subnet_id_by_cidr returns None when no subnet matches the CIDR."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(_CONFIG_GET_EMPTY_RESP),
        ):
            result = self.client._find_subnet_id_by_cidr(version=4, cidr="10.99.0.0/24")
        self.assertIsNone(result)

    def test_returns_none_when_config_get_raises(self):
        """_find_subnet_id_by_cidr returns None when config-get fails (best-effort probe)."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=requests.ConnectionError("unreachable"),
        ):
            result = self.client._find_subnet_id_by_cidr(version=4, cidr="10.0.0.0/24")
        self.assertIsNone(result)

    def test_returns_none_when_kea_error(self):
        """_find_subnet_id_by_cidr returns None when Kea returns result!=0."""
        resp = [{"result": 1, "text": "command not supported"}]
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response(resp),
        ):
            result = self.client._find_subnet_id_by_cidr(version=4, cidr="10.0.0.0/24")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestSubnetUpdateMerge
# ---------------------------------------------------------------------------

_LIVE_SUBNET4_WITH_RELAY = {
    "id": 42,
    "subnet": "10.0.0.0/24",
    "relay": {"ip-addresses": ["10.0.0.254"]},
    "allocator": "random",
    "client-class": "premium",
    "pools": [{"pool": "10.0.0.50-10.0.0.99"}],
    "option-data": [
        {"name": "routers", "data": "10.0.0.1"},
        {"name": "domain-name", "data": "old.example.com"},  # NOT managed — must be preserved
    ],
    "valid-lft": 7200,
}
_SUBNET4_GET_WITH_RELAY_RESP = [{"result": 0, "arguments": {"subnet4": [_LIVE_SUBNET4_WITH_RELAY]}}]
_SUBNET_UPDATE_OK = [{"result": 0, "arguments": {}, "text": "IPv4 subnet updated"}]

_LIVE_SUBNET6_WITH_DNS = {
    "id": 100,
    "subnet": "2001:db8::/48",
    "option-data": [
        {"name": "dns-servers", "data": "2001:4860:4860::8888"},
        {"name": "domain-search", "data": "example.com"},  # NOT managed — must be preserved
    ],
    "valid-lft": 3600,
}
_SUBNET6_GET_WITH_DNS_RESP = [{"result": 0, "arguments": {"subnet6": [_LIVE_SUBNET6_WITH_DNS]}}]
_SUBNET_UPDATE_OK_V6 = [{"result": 0, "arguments": {}, "text": "IPv6 subnet updated"}]


class TestSubnetUpdateMerge(TestCase):
    """Tests for KeaClient.subnet_update() — verifies read-modify-write merge behaviour."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def _run_update(self, **kwargs):
        """Run subnet_update with sensible defaults, returning the args sent to subnet4-update."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET_WITH_RELAY_RESP,  # subnet4-get
                _SUBNET_UPDATE_OK,  # subnet4-update
                _CONFIG_GET_RUNNING_RESP,  # config-get (in _persist_config)
                _OK,  # config-test
                _OK,  # config-write
            ),
        ) as mock_post:
            defaults = {"version": 4, "subnet_id": 42, "subnet_cidr": "10.0.0.0/24"}
            defaults.update(kwargs)
            self.client.subnet_update(**defaults)
        update_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-update"
        )
        return update_call["arguments"]["subnet4"][0]

    def test_preserves_relay_field(self):
        """subnet_update must include the live relay config in the sent dict."""
        sent = self._run_update()
        self.assertEqual(sent.get("relay"), {"ip-addresses": ["10.0.0.254"]})

    def test_preserves_allocator_field(self):
        """subnet_update must include the live allocator field."""
        sent = self._run_update()
        self.assertEqual(sent.get("allocator"), "random")

    def test_preserves_client_class_field(self):
        """subnet_update must include the live client-class field."""
        sent = self._run_update()
        self.assertEqual(sent.get("client-class"), "premium")

    def test_preserves_unmanaged_option_data(self):
        """subnet_update must keep option-data entries not managed by the form."""
        sent = self._run_update(dns_servers=["8.8.8.8"])
        names = [o["name"] for o in sent.get("option-data", [])]
        self.assertIn("domain-name", names)  # unmanaged — must survive

    def test_replaces_managed_option_data(self):
        """subnet_update must replace managed option-data (routers) with the new value."""
        sent = self._run_update(gateway="10.0.0.2")
        routers = [o for o in sent.get("option-data", []) if o["name"] == "routers"]
        self.assertEqual(len(routers), 1)
        self.assertEqual(routers[0]["data"], "10.0.0.2")

    def test_removes_managed_option_when_cleared(self):
        """subnet_update must remove routers option-data when gateway=None."""
        sent = self._run_update(gateway=None)
        names = [o["name"] for o in sent.get("option-data", [])]
        self.assertNotIn("routers", names)

    def test_overrides_valid_lft_when_provided(self):
        """subnet_update must set valid-lft to the new value when provided."""
        sent = self._run_update(valid_lft=1800)
        self.assertEqual(sent.get("valid-lft"), 1800)

    def test_keeps_live_valid_lft_when_none(self):
        """subnet_update must keep the live valid-lft when the caller passes None."""
        sent = self._run_update(valid_lft=None)
        self.assertEqual(sent.get("valid-lft"), 7200)  # from live subnet

    def test_pools_replaced_when_provided(self):
        """subnet_update must replace pools when argument is not None."""
        sent = self._run_update(pools=["10.0.0.50-10.0.0.99"])
        self.assertEqual(sent.get("pools"), [{"pool": "10.0.0.50-10.0.0.99"}])

    def test_pools_kept_when_none(self):
        """subnet_update must keep live pools when pools=None."""
        sent = self._run_update(pools=None)
        self.assertEqual(sent.get("pools"), [{"pool": "10.0.0.50-10.0.0.99"}])

    def test_calls_subnet_get_first(self):
        """subnet_update must call subnet{v}-get before subnet{v}-update."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET4_GET_WITH_RELAY_RESP,
                _SUBNET_UPDATE_OK,
                _CONFIG_GET_RUNNING_RESP,
                _OK,
                _OK,
            ),
        ) as mock_post:
            self.client.subnet_update(version=4, subnet_id=42, subnet_cidr="10.0.0.0/24")
        cmds = [(c.kwargs.get("json") or c[1]["json"])["command"] for c in mock_post.call_args_list]
        self.assertLess(cmds.index("subnet4-get"), cmds.index("subnet4-update"))

    def _run_update_v6(self, **kwargs):
        """Run subnet_update for version=6, returning the args sent to subnet6-update."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(
                _SUBNET6_GET_WITH_DNS_RESP,  # subnet6-get
                _SUBNET_UPDATE_OK_V6,  # subnet6-update
                _CONFIG_GET_RUNNING_RESP_V6,  # config-get
                _OK,  # config-test
                _OK,  # config-write
            ),
        ) as mock_post:
            defaults = {"version": 6, "subnet_id": 100, "subnet_cidr": "2001:db8::/48"}
            defaults.update(kwargs)
            self.client.subnet_update(**defaults)
        update_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet6-update"
        )
        return update_call["arguments"]["subnet6"][0]

    def test_preserves_unmanaged_option_data_v6(self):
        """subnet_update v6 must keep option-data entries not managed by the form."""
        sent = self._run_update_v6(dns_servers=["2001:4860:4860::8844"])
        names = [o["name"] for o in sent.get("option-data", [])]
        self.assertIn("domain-search", names)  # unmanaged — must survive

    def test_replaces_dns_servers_option_v6(self):
        """subnet_update v6 must replace dns-servers option-data with the new value."""
        sent = self._run_update_v6(dns_servers=["2001:4860:4860::8844"])
        dns = [o for o in sent.get("option-data", []) if o["name"] == "dns-servers"]
        self.assertEqual(len(dns), 1)
        self.assertEqual(dns[0]["data"], "2001:4860:4860::8844")


# ---------------------------------------------------------------------------
# TestSubnetAddAmbiguousCreate
# ---------------------------------------------------------------------------

_CONFIG_GET_WITH_NEW_SUBNET = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [{"id": 10, "subnet": "10.99.0.0/24"}],
                "shared-networks": [],
            }
        },
    }
]
_CONFIG_GET_WITHOUT_NEW_SUBNET = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}],
                "shared-networks": [],
            }
        },
    }
]


class TestSubnetAddAmbiguousCreate(TestCase):
    """Tests for subnet_add() transport-error probe logic."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_raises_partial_persist_error_when_subnet_found_after_transport_error(self):
        """If subnet-add transport fails but config-get confirms the subnet exists,
        PartialPersistError is raised with the found subnet_id set."""

        def _side(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            if cmd.startswith("subnet4-list"):
                return _mock_http_response(_SUBNET4_LIST_RESP)
            if cmd == "subnet4-add":
                raise requests.ConnectionError("connection reset")
            if cmd == "config-get":
                return _mock_http_response(_CONFIG_GET_WITH_NEW_SUBNET)
            return _mock_http_response(_OK)

        with patch.object(self.client._session, "post", side_effect=_side):
            with self.assertRaises(PartialPersistError) as ctx:
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        self.assertEqual(ctx.exception.subnet_id, 10)

    def test_reraises_transport_error_when_subnet_not_found_after_probe(self):
        """If subnet-add transport fails and config-get confirms the subnet does NOT exist,
        the original requests.ConnectionError is re-raised (not PartialPersistError)."""

        def _side(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            if cmd.startswith("subnet4-list"):
                return _mock_http_response(_SUBNET4_LIST_RESP)
            if cmd == "subnet4-add":
                raise requests.ConnectionError("connection reset")
            if cmd == "config-get":
                return _mock_http_response(_CONFIG_GET_WITHOUT_NEW_SUBNET)
            return _mock_http_response(_OK)

        with patch.object(self.client._session, "post", side_effect=_side):
            with self.assertRaises(requests.ConnectionError):
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")

    def test_reraises_transport_error_when_probe_also_fails(self):
        """If both subnet-add and the config-get probe fail with transport errors,
        the original exception is re-raised."""

        def _side(url, **kwargs):
            raise requests.ConnectionError("all down")

        with patch.object(self.client._session, "post", side_effect=_side):
            with self.assertRaises(requests.ConnectionError):
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24", subnet_id=42)


# ---------------------------------------------------------------------------
# TestSubnetAddValueError  (F1)
# ---------------------------------------------------------------------------


class TestSubnetAddValueError(TestCase):
    """subnet_add() catches ValueError in addition to requests.RequestException."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_raises_partial_persist_when_subnet_found_after_value_error(self):
        """If subnet-add raises ValueError and config-get confirms subnet exists,
        PartialPersistError is raised with the found subnet_id."""

        call_count = [0]

        def _side(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            call_count[0] += 1
            if cmd.startswith("subnet4-list"):
                return _mock_http_response(_SUBNET4_LIST_RESP)
            if cmd == "subnet4-add":
                raise ValueError("response was not JSON")
            if cmd == "config-get":
                return _mock_http_response(_CONFIG_GET_WITH_NEW_SUBNET)
            return _mock_http_response(_OK)

        with patch.object(self.client._session, "post", side_effect=_side):
            with self.assertRaises(PartialPersistError) as ctx:
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        self.assertEqual(ctx.exception.subnet_id, 10)

    def test_reraises_value_error_when_subnet_not_found(self):
        """If subnet-add raises ValueError and probe shows subnet not created,
        the original ValueError is re-raised."""

        def _side(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            if cmd.startswith("subnet4-list"):
                return _mock_http_response(_SUBNET4_LIST_RESP)
            if cmd == "subnet4-add":
                raise ValueError("bad JSON")
            if cmd == "config-get":
                return _mock_http_response(_CONFIG_GET_WITHOUT_NEW_SUBNET)
            return _mock_http_response(_OK)

        with patch.object(self.client._session, "post", side_effect=_side):
            with self.assertRaises(ValueError):
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")


# ---------------------------------------------------------------------------
# TestFindSubnetIdNarrowedExcept  (F2)
# ---------------------------------------------------------------------------


class TestFindSubnetIdNarrowedExcept(TestCase):
    """_find_subnet_id_by_cidr narrowed except propagates unexpected exceptions."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_returns_none_on_kea_exception(self):
        """KeaException from config-get returns None (safe probe failure)."""
        with patch.object(
            self.client._session,
            "post",
            return_value=_mock_http_response([{"result": 1, "text": "error", "arguments": None}]),
        ):
            result = self.client._find_subnet_id_by_cidr(4, "10.0.0.0/24")
        self.assertIsNone(result)

    def test_returns_none_on_requests_exception(self):
        """requests.RequestException from config-get returns None."""
        with patch.object(self.client._session, "post", side_effect=requests.ConnectionError("down")):
            result = self.client._find_subnet_id_by_cidr(4, "10.0.0.0/24")
        self.assertIsNone(result)

    def test_returns_none_on_value_error(self):
        """ValueError from config-get returns None."""
        with patch.object(self.client._session, "post", side_effect=ValueError("bad JSON")):
            result = self.client._find_subnet_id_by_cidr(4, "10.0.0.0/24")
        self.assertIsNone(result)

    def test_propagates_attribute_error(self):
        """An AttributeError (programming bug) must NOT be swallowed by the probe."""
        with patch.object(self.client._session, "post", side_effect=AttributeError("bug")):
            with self.assertRaises(AttributeError):
                self.client._find_subnet_id_by_cidr(4, "10.0.0.0/24")


# ---------------------------------------------------------------------------
# TestSubnetAddKeaConfigPersistError
# ---------------------------------------------------------------------------


class TestSubnetAddKeaConfigPersistError(TestCase):
    """subnet_add() attaches subnet_id to KeaConfigPersistError from _persist_config."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_kea_config_persist_error_gets_subnet_id(self):
        """When _persist_config raises KeaConfigPersistError, subnet_id is attached before re-raising."""
        call_count = [0]

        def _side(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            call_count[0] += 1
            if cmd.startswith("subnet4-list"):
                return _mock_http_response(_SUBNET4_LIST_RESP)
            if cmd == "subnet4-add":
                return _mock_http_response(
                    [{"result": 0, "arguments": {"subnets": [{"id": 99, "subnet": "10.99.0.0/24"}]}}]
                )
            if cmd == "config-get":
                return _mock_http_response([{"result": 0, "arguments": {"Dhcp4": {}, "hash": "abc"}}])
            if cmd == "config-test":
                return _mock_http_response([{"result": 1, "text": "config-test rejected"}])
            if cmd == "config-write":
                return _mock_http_response(_OK)
            return _mock_http_response(_OK)

        with patch.object(self.client._session, "post", side_effect=_side):
            with self.assertRaises(KeaConfigPersistError) as ctx:
                self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        self.assertEqual(ctx.exception.subnet_id, 99)


# ---------------------------------------------------------------------------
# TestPersistConfigMalformedArguments
# ---------------------------------------------------------------------------


class TestPersistConfigMalformedArguments(TestCase):
    """_persist_config degrades gracefully when config-get arguments is null/malformed."""

    def setUp(self):
        self.client = KeaClient(url="http://kea:8000")

    def test_null_arguments_does_not_raise_attribute_error(self):
        """config-get returning null arguments should not crash _persist_config."""
        call_count = [0]

        def _side(url, **kwargs):
            cmd = kwargs.get("json", {}).get("command", "")
            call_count[0] += 1
            if cmd == "config-get":
                return _mock_http_response([{"result": 0, "arguments": None}])
            if cmd == "config-write":
                return _mock_http_response([{"result": 0, "text": "Config written successfully!"}])
            return _mock_http_response(_OK)

        with patch.object(self.client._session, "post", side_effect=_side):
            self.client._persist_config("dhcp4")


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

    @patch("requests.Session.post")
    def test_server_update_options_null_arguments_raises(self, mock_post):
        mock_post.return_value = self._null_args_response()
        with self.assertRaises(KeaException):
            self.client.server_update_options(version=4, options=[])

    @patch("requests.Session.post")
    def test_option_def_add_null_arguments_raises(self, mock_post):
        mock_post.return_value = self._null_args_response()
        with self.assertRaises(KeaException):
            self.client.option_def_add(
                version=4, option_def={"name": "test", "code": 200, "type": "string", "space": "dhcp4"}
            )

    @patch("requests.Session.post")
    def test_option_def_del_null_arguments_raises(self, mock_post):
        mock_post.return_value = self._null_args_response()
        with self.assertRaises(KeaException):
            self.client.option_def_del(version=4, code=200, space="dhcp4")


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

    @patch("requests.Session.post")
    def test_empty_page_breaks_loop(self, mock_post):
        """An empty page list causes the loop to break instead of advancing cursor."""
        # First call: empty page with count=250 (would normally continue)
        mock_post.side_effect = [
            self._page_response(leases=[], count=250),
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
    def test_multi_page_aggregates_leases(self, mock_post):
        """Two pages of leases are combined into a single list."""
        page1 = [{"ip-address": "10.0.0.1"}, {"ip-address": "10.0.0.2"}]
        page2 = [{"ip-address": "10.0.0.3"}]
        mock_post.side_effect = [
            self._page_response(leases=page1, count=2),  # full page → advance cursor
            self._page_response(leases=page2, count=1),  # partial page → stop
        ]
        leases, truncated = self.client.lease_get_all(version=4, per_page=2)
        self.assertEqual(len(leases), 3)
        self.assertFalse(truncated)
