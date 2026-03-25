# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.models — Server model validation and client factory.

All Kea HTTP calls are mocked; these tests require no running services.
"""

from unittest.mock import MagicMock, patch

import requests
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings
from netbox.models import NetBoxModel

from netbox_kea.kea import KeaClient
from netbox_kea.models import Server

# Default PLUGINS_CONFIG used across model tests so we don't need full NetBox config.
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


def _make_server(**kwargs) -> Server:
    """Return an unsaved Server instance with sensible defaults."""
    defaults = {
        "name": "test-server",
        "server_url": "http://kea:8000",
        "dhcp4": True,
        "dhcp6": True,
    }
    defaults.update(kwargs)
    return Server(**defaults)


class TestServerStr(SimpleTestCase):
    """Tests for Server.__str__."""

    def test_str_returns_name(self):
        server = _make_server(name="my-kea")
        self.assertEqual(str(server), "my-kea")


class TestServerGetClient(SimpleTestCase):
    """Tests for Server.get_client() — URL selection logic."""

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_no_version_uses_server_url(self):
        server = _make_server()
        client = server.get_client()
        self.assertIsInstance(client, KeaClient)
        self.assertEqual(client.url, "http://kea:8000")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_version4_falls_back_to_server_url(self):
        server = _make_server(dhcp4_url=None)
        client = server.get_client(version=4)
        self.assertEqual(client.url, "http://kea:8000")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_version4_uses_dhcp4_url_override(self):
        server = _make_server(dhcp4_url="http://kea-v4:8001")
        client = server.get_client(version=4)
        self.assertEqual(client.url, "http://kea-v4:8001")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_version6_falls_back_to_server_url(self):
        server = _make_server(dhcp6_url=None)
        client = server.get_client(version=6)
        self.assertEqual(client.url, "http://kea:8000")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_version6_uses_dhcp6_url_override(self):
        server = _make_server(dhcp6_url="http://kea-v6:8002")
        client = server.get_client(version=6)
        self.assertEqual(client.url, "http://kea-v6:8002")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dual_url_v4_and_v6_independent(self):
        server = _make_server(dhcp4_url="http://v4:1", dhcp6_url="http://v6:2")
        self.assertEqual(server.get_client(version=4).url, "http://v4:1")
        self.assertEqual(server.get_client(version=6).url, "http://v6:2")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_get_client_passes_credentials(self):
        server = _make_server(username="admin", password="secret")
        client = server.get_client()
        self.assertIsNotNone(client._session.auth)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_get_client_ssl_verify_false(self):
        server = _make_server(ssl_verify=False)
        client = server.get_client()
        self.assertFalse(client._session.verify)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_get_client_ca_file_path(self):
        server = _make_server(ca_file_path="/certs/ca.pem", ssl_verify=True)
        client = server.get_client()
        # ca_file_path takes precedence over ssl_verify in the verify= argument
        self.assertEqual(client._session.verify, "/certs/ca.pem")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_get_client_cert_key_pair(self):
        server = _make_server(client_cert_path="/cert.pem", client_key_path="/key.pem")
        client = server.get_client()
        self.assertEqual(client._session.cert, ("/cert.pem", "/key.pem"))

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_get_client_uses_configured_timeout(self):
        """Timeout is read from PLUGINS_CONFIG."""
        server = _make_server()
        client = server.get_client()
        self.assertEqual(client.timeout, 30)

    @override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": 5}})
    def test_get_client_custom_timeout(self):
        server = _make_server()
        client = server.get_client()
        self.assertEqual(client.timeout, 5)


class TestServerCleanFieldValidation(SimpleTestCase):
    """Tests for Server.clean() — field-level validation that runs before connectivity checks."""

    # Patch super().clean() to avoid NetBox DB introspection in these field-only tests.
    def setUp(self):
        patcher = patch.object(NetBoxModel, "clean", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp4_false_and_dhcp6_false_raises(self):
        server = _make_server(dhcp4=False, dhcp6=False)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("dhcp6", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_cert_without_key_raises(self):
        server = _make_server(client_cert_path="/cert.pem", client_key_path=None)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("client_cert_path", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_key_without_cert_raises(self):
        server = _make_server(client_cert_path=None, client_key_path="/key.pem")
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("client_cert_path", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_ca_file_with_ssl_verify_false_raises(self):
        server = _make_server(ca_file_path="/ca.pem", ssl_verify=False)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("ca_file_path", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.os.path.isfile", return_value=False)
    def test_nonexistent_cert_path_raises(self, _mock_isfile):
        server = _make_server(client_cert_path="/missing.pem", client_key_path="/key.pem")
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("client_cert_path", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.os.path.isfile", side_effect=lambda p: p != "/missing-key.pem")
    def test_nonexistent_key_path_raises(self, _mock_isfile):
        server = _make_server(client_cert_path="/cert.pem", client_key_path="/missing-key.pem")
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("client_key_path", ctx.exception.message_dict)


class TestServerCleanConnectivity(SimpleTestCase):
    """Tests for Server.clean() — live connectivity checks (Kea API mocked)."""

    def setUp(self):
        patcher = patch.object(NetBoxModel, "clean", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _make_mock_client(self):
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.return_value = [{"result": 0, "arguments": {"version": "2.5.0"}}]
        return mock_client

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_valid_dhcp4_only_passes(self, mock_kea_cls):
        mock_kea_cls.return_value = self._make_mock_client()
        server = _make_server(dhcp4=True, dhcp6=False)
        server.clean()  # must not raise

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_valid_dhcp6_only_passes(self, mock_kea_cls):
        mock_kea_cls.return_value = self._make_mock_client()
        server = _make_server(dhcp4=False, dhcp6=True)
        server.clean()  # must not raise

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_valid_both_protocols_passes(self, mock_kea_cls):
        mock_kea_cls.return_value = self._make_mock_client()
        server = _make_server(dhcp4=True, dhcp6=True)
        server.clean()  # must not raise

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp6_connection_failure_raises(self, mock_kea_cls):
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = requests.exceptions.ConnectionError("Connection refused")
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=False, dhcp6=True)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("dhcp6", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp4_connection_failure_raises(self, mock_kea_cls):
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = requests.exceptions.Timeout("Timeout")
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=True, dhcp6=False)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("dhcp4", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_connectivity_check_uses_version_specific_url(self, mock_kea_cls):
        """get_client(version=4|6) must be called during clean(), not get_client()."""
        mock_kea_cls.return_value = self._make_mock_client()
        server = _make_server(dhcp4=True, dhcp6=True, dhcp4_url="http://v4:1", dhcp6_url="http://v6:2")
        server.clean()

        # Verify KeaClient was constructed with the protocol-specific URLs
        calls = [call.kwargs.get("url") or call.args[0] for call in mock_kea_cls.call_args_list]
        self.assertIn("http://v4:1", calls)
        self.assertIn("http://v6:2", calls)


class TestServerHasControlAgentDefault(SimpleTestCase):
    """Test that has_control_agent defaults to True."""

    def test_has_control_agent_default_true(self):
        server = Server(name="s", server_url="http://kea:8000", dhcp4=True)
        self.assertTrue(server.has_control_agent)
