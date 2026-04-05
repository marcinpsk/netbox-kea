# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.models — Server model validation and client factory.

All Kea HTTP calls are mocked; these tests require no running services.
"""

from unittest.mock import MagicMock, patch

import requests
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from netbox.models import NetBoxModel

from netbox_kea.kea import KeaClient
from netbox_kea.models import Server, SyncConfig
from netbox_kea.tests.utils import _make_db_server

# Default PLUGINS_CONFIG used across model tests so we don't need full NetBox config.
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


def _make_server(**kwargs) -> Server:
    """Return an unsaved Server instance with sensible defaults."""
    defaults = {
        "name": "test-server",
        "ca_url": "http://kea:8000",
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
    def test_no_version_uses_ca_url(self):
        server = _make_server()
        client = server.get_client()
        self.assertIsInstance(client, KeaClient)
        self.assertEqual(client.url, "http://kea:8000")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_version4_falls_back_to_ca_url(self):
        server = _make_server(dhcp4_url=None)
        client = server.get_client(version=4)
        self.assertEqual(client.url, "http://kea:8000")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_version4_uses_dhcp4_url_override(self):
        server = _make_server(dhcp4_url="http://kea-v4:8001")
        client = server.get_client(version=4)
        self.assertEqual(client.url, "http://kea-v4:8001")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_version6_falls_back_to_ca_url(self):
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
        server = _make_server(ca_username="admin", ca_password="secret")
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

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v4_uses_dhcp4_credentials_when_set(self):
        """When dhcp4_username/dhcp4_password are set, v4 client uses them."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp4_username="v4-user",
            dhcp4_password="v4-pass",
        )
        client = server.get_client(version=4)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "v4-user")
        self.assertEqual(client._session.auth.password, "v4-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v4_falls_back_to_ca_credentials_when_v4_creds_empty(self):
        """When dhcp4_username/dhcp4_password are blank, v4 client uses CA creds."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp4_username="",
            dhcp4_password="",
        )
        client = server.get_client(version=4)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "ca-user")
        self.assertEqual(client._session.auth.password, "ca-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v6_uses_dhcp6_credentials_when_set(self):
        """When dhcp6_username/dhcp6_password are set, v6 client uses them."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp6_username="v6-user",
            dhcp6_password="v6-pass",
        )
        client = server.get_client(version=6)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "v6-user")
        self.assertEqual(client._session.auth.password, "v6-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v6_falls_back_to_ca_credentials_when_v6_creds_empty(self):
        """When dhcp6_username/dhcp6_password are blank, v6 client uses CA creds."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp6_username="",
            dhcp6_password="",
        )
        client = server.get_client(version=6)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "ca-user")
        self.assertEqual(client._session.auth.password, "ca-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_no_version_uses_ca_credentials(self):
        """When version=None, always use CA credentials."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp4_username="v4-user",
            dhcp4_password="v4-pass",
        )
        client = server.get_client()
        self.assertEqual(client._session.auth.username, "ca-user")


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

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp6_kea_exception_raises_unable_to_reach(self, mock_kea_cls):
        """KeaException during DHCPv6 check → 'Unable to reach' ValidationError."""
        from netbox_kea.kea import KeaException

        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = KeaException({"result": 1, "text": "error"})
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=False, dhcp6=True)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("dhcp6", ctx.exception.message_dict)
        self.assertIn("Unable to reach", ctx.exception.message_dict["dhcp6"][0])

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp4_kea_exception_raises_unable_to_reach(self, mock_kea_cls):
        """KeaException during DHCPv4 check → 'Unable to reach' ValidationError."""
        from netbox_kea.kea import KeaException

        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = KeaException({"result": 1, "text": "error"})
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=True, dhcp6=False)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("dhcp4", ctx.exception.message_dict)
        self.assertIn("Unable to reach", ctx.exception.message_dict["dhcp4"][0])

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp6_json_decode_error_raises_internal_error(self, mock_kea_cls):
        """JSONDecodeError during DHCPv6 check → 'An internal error occurred' ValidationError."""
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = requests.exceptions.JSONDecodeError("bad json", "", 0)
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=False, dhcp6=True)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("dhcp6", ctx.exception.message_dict)
        self.assertIn("internal error", ctx.exception.message_dict["dhcp6"][0])
        self.assertNotIn("bad json", ctx.exception.message_dict["dhcp6"][0])

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp4_json_decode_error_raises_internal_error(self, mock_kea_cls):
        """JSONDecodeError during DHCPv4 check → 'An internal error occurred' ValidationError."""
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = requests.exceptions.JSONDecodeError("bad json", "", 0)
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=True, dhcp6=False)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        self.assertIn("dhcp4", ctx.exception.message_dict)
        self.assertIn("internal error", ctx.exception.message_dict["dhcp4"][0])
        self.assertNotIn("bad json", ctx.exception.message_dict["dhcp4"][0])


class TestServerHasControlAgentDefault(SimpleTestCase):
    """Test that has_control_agent defaults to True."""

    def test_has_control_agent_default_true(self):
        server = Server(name="s", ca_url="http://kea:8000", dhcp4=True)
        self.assertTrue(server.has_control_agent)


# ---------------------------------------------------------------------------
# to_objectchange — password censoring
# ---------------------------------------------------------------------------


class TestServerToObjectchangePasswordCensoring(SimpleTestCase):
    """to_objectchange() must censor passwords correctly in pre/post change data."""

    def _make_objectchange(self, pre_password, post_password):
        """Build a mock ObjectChange with configurable pre/post password values."""
        from unittest.mock import MagicMock

        from netbox.constants import CENSOR_TOKEN, CENSOR_TOKEN_CHANGED  # noqa: F401

        prechange_data = {"ca_password": pre_password, "name": "test-server"} if pre_password is not None else {}
        postchange_data = {"ca_password": post_password, "name": "test-server"} if post_password is not None else None

        objectchange = MagicMock()
        objectchange.prechange_data = prechange_data
        objectchange.postchange_data = postchange_data
        return objectchange

    def _call_to_objectchange(self, pre_password, post_password):
        from netbox.constants import CENSOR_TOKEN, CENSOR_TOKEN_CHANGED  # noqa: F401

        server = Server(name="test-server", ca_url="http://kea:8000", dhcp4=True)
        objectchange = self._make_objectchange(pre_password, post_password)
        with patch.object(NetBoxModel, "to_objectchange", return_value=objectchange):
            return server.to_objectchange("update")

    def test_unchanged_password_masked_as_censor_token(self):
        """When pre and post passwords are the same, post shows CENSOR_TOKEN (not CHANGED)."""
        from netbox.constants import CENSOR_TOKEN, CENSOR_TOKEN_CHANGED

        result = self._call_to_objectchange(pre_password="secret", post_password="secret")
        self.assertEqual(result.postchange_data["ca_password"], CENSOR_TOKEN)
        self.assertNotEqual(result.postchange_data["ca_password"], CENSOR_TOKEN_CHANGED)

    def test_changed_password_masked_as_censor_token_changed(self):
        """When post password differs from pre password, post shows CENSOR_TOKEN_CHANGED."""
        from netbox.constants import CENSOR_TOKEN_CHANGED

        result = self._call_to_objectchange(pre_password="old-secret", post_password="new-secret")
        self.assertEqual(result.postchange_data["ca_password"], CENSOR_TOKEN_CHANGED)

    def test_pre_password_always_masked_as_censor_token(self):
        """prechange_data password is always replaced with CENSOR_TOKEN."""
        from netbox.constants import CENSOR_TOKEN

        result = self._call_to_objectchange(pre_password="old-secret", post_password="new-secret")
        self.assertEqual(result.prechange_data["ca_password"], CENSOR_TOKEN)

    def test_no_prechange_data_does_not_raise(self):
        """to_objectchange handles None prechange_data without raising and masks postchange password."""
        from netbox.constants import CENSOR_TOKEN_CHANGED

        server = Server(name="test-server", ca_url="http://kea:8000", dhcp4=True, ca_password="secret")
        objectchange = self._make_objectchange(pre_password=None, post_password="secret")
        objectchange.prechange_data = None
        with patch.object(NetBoxModel, "to_objectchange", return_value=objectchange):
            result = server.to_objectchange("create")
        # Should not crash; None prechange is preserved (or converted to empty dict)
        self.assertIn(result.prechange_data, (None, {}))
        # postchange_data password must be masked (changed from original "secret")
        masked = result.postchange_data.get("ca_password")
        self.assertNotEqual(masked, "secret", "Password must be masked in postchange_data")
        self.assertEqual(masked, CENSOR_TOKEN_CHANGED)

    def test_dhcp4_password_masked_in_change_log(self):
        """dhcp4_password is censored in both prechange and postchange data."""
        from netbox.constants import CENSOR_TOKEN

        server = Server(name="test-server", ca_url="http://kea:8000", dhcp4=True)
        objectchange = MagicMock()
        objectchange.prechange_data = {"dhcp4_password": "v4-secret", "name": "s"}
        objectchange.postchange_data = {"dhcp4_password": "v4-secret", "name": "s"}
        with patch.object(NetBoxModel, "to_objectchange", return_value=objectchange):
            result = server.to_objectchange("update")
        self.assertEqual(result.prechange_data["dhcp4_password"], CENSOR_TOKEN)
        self.assertEqual(result.postchange_data["dhcp4_password"], CENSOR_TOKEN)

    def test_dhcp6_password_masked_in_change_log(self):
        """dhcp6_password is censored in both prechange and postchange data."""
        from netbox.constants import CENSOR_TOKEN

        server = Server(name="test-server", ca_url="http://kea:8000", dhcp4=True)
        objectchange = MagicMock()
        objectchange.prechange_data = {"dhcp6_password": "v6-secret", "name": "s"}
        objectchange.postchange_data = {"dhcp6_password": "v6-secret", "name": "s"}
        with patch.object(NetBoxModel, "to_objectchange", return_value=objectchange):
            result = server.to_objectchange("update")
        self.assertEqual(result.prechange_data["dhcp6_password"], CENSOR_TOKEN)
        self.assertEqual(result.postchange_data["dhcp6_password"], CENSOR_TOKEN)


# ---------------------------------------------------------------------------
# Server.clean() — exception type routing tests
# ---------------------------------------------------------------------------


class TestServerCleanExceptionRouting(SimpleTestCase):
    """RequestException/ValueError → 'Unable to reach', JSONDecodeError → 'An internal error occurred'."""

    def setUp(self):
        patcher = patch.object(NetBoxModel, "clean", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp6_value_error_raises_reachability_message(self, mock_kea_cls):
        """ValueError during DHCPv6 check must raise 'Unable to reach' message."""
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = ValueError("bad response")
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=False, dhcp6=True)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        msg = str(ctx.exception.message_dict.get("dhcp6", [""])[0])
        self.assertIn("Unable to reach", msg)
        self.assertNotIn("internal error", msg)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp4_value_error_raises_reachability_message(self, mock_kea_cls):
        """ValueError during DHCPv4 check must raise 'Unable to reach' message."""
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = ValueError("bad response")
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=True, dhcp6=False)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        msg = str(ctx.exception.message_dict.get("dhcp4", [""])[0])
        self.assertIn("Unable to reach", msg)
        self.assertNotIn("internal error", msg)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp6_json_decode_error_raises_internal_error_message(self, mock_kea_cls):
        """JSONDecodeError during DHCPv6 check must raise 'An internal error occurred' message."""
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=False, dhcp6=True)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        msg = str(ctx.exception.message_dict.get("dhcp6", [""])[0])
        self.assertIn("internal error", msg)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.KeaClient")
    def test_dhcp4_json_decode_error_raises_internal_error_message(self, mock_kea_cls):
        """JSONDecodeError during DHCPv4 check must raise 'An internal error occurred' message."""
        mock_client = MagicMock(spec=KeaClient)
        mock_client.command.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        mock_kea_cls.return_value = mock_client
        server = _make_server(dhcp4=True, dhcp6=False)
        with self.assertRaises(ValidationError) as ctx:
            server.clean()
        msg = str(ctx.exception.message_dict.get("dhcp4", [""])[0])
        self.assertIn("internal error", msg)


class TestSyncConfig(TestCase):
    """Tests for the SyncConfig singleton model."""

    def test_get_creates_with_defaults_when_missing(self):
        cfg = SyncConfig.get()
        self.assertEqual(cfg.interval_minutes, 5)
        self.assertTrue(cfg.sync_enabled)

    def test_get_returns_existing_record(self):
        SyncConfig.objects.create(pk=1, interval_minutes=10, sync_enabled=False)
        cfg = SyncConfig.get()
        self.assertEqual(cfg.interval_minutes, 10)
        self.assertFalse(cfg.sync_enabled)

    def test_get_is_idempotent(self):
        cfg1 = SyncConfig.get()
        cfg2 = SyncConfig.get()
        self.assertEqual(cfg1.pk, cfg2.pk)
        self.assertEqual(SyncConfig.objects.count(), 1)

    def test_get_uses_default_interval_on_first_create(self):
        cfg = SyncConfig.get(default_interval=15)
        self.assertEqual(cfg.interval_minutes, 15)

    def test_get_does_not_override_existing_interval(self):
        SyncConfig.objects.create(pk=1, interval_minutes=30)
        cfg = SyncConfig.get(default_interval=99)
        self.assertEqual(cfg.interval_minutes, 30)

    def test_save_forces_pk_to_1(self):
        cfg = SyncConfig(interval_minutes=10)
        cfg.pk = 999
        cfg.save()
        self.assertEqual(cfg.pk, 1)
        self.assertEqual(SyncConfig.objects.count(), 1)

    def test_save_second_instance_merges_to_singleton(self):
        SyncConfig.objects.create(pk=1, interval_minutes=5)
        cfg2 = SyncConfig(interval_minutes=20)
        cfg2.save()
        self.assertEqual(SyncConfig.objects.count(), 1)
        self.assertEqual(SyncConfig.objects.get(pk=1).interval_minutes, 20)

    def test_delete_raises_type_error(self):
        cfg = SyncConfig.get()
        with self.assertRaises(TypeError):
            cfg.delete()


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSyncEnabled(TestCase):
    def test_sync_enabled_defaults_to_true(self):
        server = _make_db_server()
        self.assertTrue(server.sync_enabled)

    def test_sync_enabled_can_be_set_false(self):
        server = _make_db_server()
        server.sync_enabled = False
        server.save(update_fields=["sync_enabled"])
        server.refresh_from_db()
        self.assertFalse(server.sync_enabled)
