# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.kea — KeaClient, KeaException, check_response.

These tests mock all HTTP calls and require no running services.
"""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests

from netbox_kea.kea import KeaClient, KeaException, check_response


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


class TestKeaClientCommand(TestCase):
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
    return [_mock_http_response(r) for r in responses]


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
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
        ) as mock_post:
            self.client.pool_add(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        self.assertEqual(self._cmds(mock_post), ["list-commands", "subnet4-pool-add", "config-write"])

    def test_pool_add_v4_sends_correct_arguments(self):
        """subnet4-pool-add arguments include correct id and pool."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITHOUT_POOL_CMDS, _SUBNET4_GET, _OK, _OK),
        ) as mock_post:
            self.client.pool_add(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        self.assertEqual(
            self._cmds(mock_post),
            ["list-commands", "subnet4-get", "subnet4-delta-add", "config-write"],
        )

    def test_pool_add_delta_add_includes_subnet_cidr(self):
        """subnet4-delta-add payload includes the CIDR from subnet4-get."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITHOUT_POOL_CMDS, _SUBNET4_GET, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
        ) as mock_post:
            self.client.pool_del(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        self.assertEqual(self._cmds(mock_post), ["list-commands", "subnet4-pool-del", "config-write"])

    def test_pool_del_v4_sends_correct_arguments(self):
        """subnet4-pool-del arguments include correct id and pool."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITHOUT_POOL_CMDS, _SUBNET4_GET, _OK, _OK),
        ) as mock_post:
            self.client.pool_del(version=4, subnet_id=1, pool="10.0.0.50-10.0.0.99")
        self.assertEqual(
            self._cmds(mock_post),
            ["list-commands", "subnet4-get", "subnet4-delta-del", "config-write"],
        )

    def test_pool_del_delta_del_includes_subnet_cidr(self):
        """subnet4-delta-del payload includes the CIDR from subnet4-get."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_LIST_WITHOUT_POOL_CMDS, _SUBNET4_GET, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
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
            side_effect=_side_effects(_LIST_WITH_POOL_CMDS, _OK, _OK),
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
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _OK),
        ) as mock_post:
            self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        self.assertIn("subnet4-add", self._cmds(mock_post))

    def test_subnet_add_v6_sends_correct_command(self):
        """subnet6-add is sent for version=6."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET6_LIST_RESP, _SUBNET6_ADD_RESP, _OK),
        ) as mock_post:
            self.client.subnet_add(version=6, subnet_cidr="2001:db8:99::/48")
        self.assertIn("subnet6-add", self._cmds(mock_post))
        self.assertNotIn("subnet4-add", self._cmds(mock_post))

    def test_subnet_add_sends_subnet_cidr(self):
        """subnet4-add payload includes the subnet CIDR."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _OK),
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
            side_effect=_side_effects(_SUBNET4_ADD_RESP, _OK),
        ) as mock_post:
            self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24", subnet_id=42)
        add_call = next(
            c.kwargs.get("json") or c[1]["json"]
            for c in mock_post.call_args_list
            if (c.kwargs.get("json") or c[1]["json"])["command"] == "subnet4-add"
        )
        self.assertEqual(add_call["arguments"]["subnet4"][0]["id"], 42)
        # Exactly 2 calls: subnet4-add + config-write (no subnet4-list)
        self.assertEqual(len(mock_post.call_args_list), 2)

    def test_subnet_add_auto_assigns_id_as_max_plus_one(self):
        """When no subnet_id provided, auto-assigns max existing ID + 1."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _OK),
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
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _OK),
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
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _OK),
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
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _OK),
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

    def test_subnet_add_returns_none_on_success(self):
        """subnet_add returns None on success."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET4_LIST_RESP, _SUBNET4_ADD_RESP, _OK),
        ):
            result = self.client.subnet_add(version=4, subnet_cidr="10.99.0.0/24")
        self.assertIsNone(result)


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
            side_effect=_side_effects(_SUBNET_DEL_RESP, _OK),
        ) as mock_post:
            self.client.subnet_del(version=4, subnet_id=5)
        self.assertIn("subnet4-del", self._cmds(mock_post))

    def test_subnet_del_v6_sends_correct_command(self):
        """subnet6-del is sent for version=6."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET_DEL_RESP, _OK),
        ) as mock_post:
            self.client.subnet_del(version=6, subnet_id=7)
        self.assertIn("subnet6-del", self._cmds(mock_post))
        self.assertNotIn("subnet4-del", self._cmds(mock_post))

    def test_subnet_del_sends_correct_id(self):
        """subnet4-del payload contains the correct subnet ID."""
        with patch.object(
            self.client._session,
            "post",
            side_effect=_side_effects(_SUBNET_DEL_RESP, _OK),
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
            side_effect=_side_effects(_SUBNET_DEL_RESP, _OK),
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
            side_effect=_side_effects(_SUBNET_DEL_RESP, _OK),
        ):
            result = self.client.subnet_del(version=4, subnet_id=1)
        self.assertIsNone(result)
