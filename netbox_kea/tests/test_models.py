# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.models — Server model validation and client factory.

All Kea HTTP calls are mocked; these tests require no running services.
"""

from unittest.mock import patch

import requests
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from netbox.models import NetBoxModel

from netbox_kea.kea import KeaClient
from netbox_kea.models import Server, SyncConfig, _get_kea_timeout
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.utils import _make_db_server

_VERSION_OK = {"result": 0, "arguments": {"version": "2.5.0"}}

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
        """When dhcp4_url and dhcp4_username/dhcp4_password are set, v4 client uses them."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp4_url="http://kea-v4:8001",
            dhcp4_username="v4-user",
            dhcp4_password="v4-pass",
        )
        client = server.get_client(version=4)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "v4-user")
        self.assertEqual(client._session.auth.password, "v4-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v4_no_dhcp4_url_always_uses_ca_credentials(self):
        """When dhcp4_url is not set, v4 client always uses CA credentials even if per-protocol creds exist."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp4_username="v4-user",
            dhcp4_password="v4-pass",
        )
        client = server.get_client(version=4)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "ca-user")
        self.assertEqual(client._session.auth.password, "ca-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v4_falls_back_to_ca_credentials_when_v4_creds_empty(self):
        """When dhcp4_url is set but dhcp4 creds are blank, v4 client falls back to CA creds."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp4_url="http://kea-v4:8001",
            dhcp4_username="",
            dhcp4_password="",
        )
        client = server.get_client(version=4)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "ca-user")
        self.assertEqual(client._session.auth.password, "ca-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v6_uses_dhcp6_credentials_when_set(self):
        """When dhcp6_url and dhcp6_username/dhcp6_password are set, v6 client uses them."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp6_url="http://kea-v6:8002",
            dhcp6_username="v6-user",
            dhcp6_password="v6-pass",
        )
        client = server.get_client(version=6)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "v6-user")
        self.assertEqual(client._session.auth.password, "v6-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v6_falls_back_to_ca_credentials_when_v6_creds_empty(self):
        """When dhcp6_url is set but dhcp6 creds are blank, v6 client falls back to CA creds."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp6_url="http://kea-v6:8002",
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
            dhcp4_url="http://kea-v4:8001",
            dhcp4_username="v4-user",
            dhcp4_password="v4-pass",
        )
        client = server.get_client()
        self.assertEqual(client._session.auth.username, "ca-user")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v4_partial_override_falls_back_per_field(self):
        """When dhcp4_url set and only dhcp4_username provided, password falls back to ca_password."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp4_url="http://kea-v4:8001",
            dhcp4_username="v4-user",
            dhcp4_password="",
        )
        client = server.get_client(version=4)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "v4-user")
        self.assertEqual(client._session.auth.password, "ca-pass")

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_v6_partial_override_falls_back_per_field(self):
        """When dhcp6_url set and only dhcp6_password provided, username falls back to ca_username."""
        server = _make_server(
            ca_username="ca-user",
            ca_password="ca-pass",
            dhcp6_url="http://kea-v6:8002",
            dhcp6_username="",
            dhcp6_password="v6-pass",
        )
        client = server.get_client(version=6)
        self.assertIsNotNone(client._session.auth)
        self.assertEqual(client._session.auth.username, "ca-user")
        self.assertEqual(client._session.auth.password, "v6-pass")


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

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_valid_dhcp4_only_passes(self):
        server = _make_server(dhcp4=True, dhcp6=False)
        with stub_kea({"version-get": _VERSION_OK}):
            server.clean()  # must not raise

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_valid_dhcp6_only_passes(self):
        server = _make_server(dhcp4=False, dhcp6=True)
        with stub_kea({"version-get": _VERSION_OK}):
            server.clean()  # must not raise

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_valid_both_protocols_passes(self):
        server = _make_server(dhcp4=True, dhcp6=True)
        with stub_kea({"version-get": _VERSION_OK}):
            server.clean()  # must not raise

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp6_connection_failure_raises(self):
        server = _make_server(dhcp4=False, dhcp6=True)
        with stub_kea({"version-get": requests.exceptions.ConnectionError("Connection refused")}):
            with self.assertRaises(ValidationError) as ctx:
                server.clean()
        self.assertIn("dhcp6", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp4_connection_failure_raises(self):
        server = _make_server(dhcp4=True, dhcp6=False)
        with stub_kea({"version-get": requests.exceptions.Timeout("Timeout")}):
            with self.assertRaises(ValidationError) as ctx:
                server.clean()
        self.assertIn("dhcp4", ctx.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_connectivity_check_uses_version_specific_url(self):
        """clean() must reach each daemon at its protocol-specific URL, not the shared CA URL."""
        server = _make_server(dhcp4=True, dhcp6=True, dhcp4_url="http://v4:1", dhcp6_url="http://v6:2")
        with stub_kea({"version-get": _VERSION_OK}) as kea:
            server.clean()
        # The real per-version clients POST to the dual-URL endpoints.
        self.assertIn("http://v4:1", kea.urls())
        self.assertIn("http://v6:2", kea.urls())

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp6_kea_exception_raises_unable_to_reach(self):
        """KeaException during DHCPv6 check → 'Unable to reach' ValidationError."""
        server = _make_server(dhcp4=False, dhcp6=True)
        with stub_kea({"version-get": {"result": 1, "text": "error"}}):
            with self.assertRaises(ValidationError) as ctx:
                server.clean()
        self.assertIn("dhcp6", ctx.exception.message_dict)
        self.assertIn("Unable to reach", ctx.exception.message_dict["dhcp6"][0])

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp4_kea_exception_raises_unable_to_reach(self):
        """KeaException during DHCPv4 check → 'Unable to reach' ValidationError."""
        server = _make_server(dhcp4=True, dhcp6=False)
        with stub_kea({"version-get": {"result": 1, "text": "error"}}):
            with self.assertRaises(ValidationError) as ctx:
                server.clean()
        self.assertIn("dhcp4", ctx.exception.message_dict)
        self.assertIn("Unable to reach", ctx.exception.message_dict["dhcp4"][0])

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp6_json_decode_error_raises_internal_error(self):
        """JSONDecodeError during DHCPv6 check → 'An internal error occurred' ValidationError."""
        server = _make_server(dhcp4=False, dhcp6=True)
        with stub_kea({"version-get": requests.exceptions.JSONDecodeError("bad json", "", 0)}):
            with self.assertRaises(ValidationError) as ctx:
                server.clean()
        self.assertIn("dhcp6", ctx.exception.message_dict)
        self.assertIn("internal error", ctx.exception.message_dict["dhcp6"][0])
        self.assertNotIn("bad json", ctx.exception.message_dict["dhcp6"][0])

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp4_json_decode_error_raises_internal_error(self):
        """JSONDecodeError during DHCPv4 check → 'An internal error occurred' ValidationError."""
        server = _make_server(dhcp4=True, dhcp6=False)
        with stub_kea({"version-get": requests.exceptions.JSONDecodeError("bad json", "", 0)}):
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


class TestServerToObjectchangePasswordCensoring(TestCase):
    """to_objectchange() must censor passwords correctly in pre/post change data.

    Uses a real saved Server row so NetBoxModel.to_objectchange() builds the
    prechange_data / postchange_data dicts from actual model serialisation.
    server.snapshot() sets _prechange_snapshot; changing a field without saving
    lets postchange_data reflect the new in-memory value.
    """

    def _make_server(self, **kwargs) -> Server:
        return _make_db_server(**kwargs)

    def test_unchanged_password_masked_as_censor_token(self):
        """When pre and post passwords are the same, post shows CENSOR_TOKEN (not CHANGED)."""
        from netbox.constants import CENSOR_TOKEN, CENSOR_TOKEN_CHANGED

        server = self._make_server(ca_password="secret")
        server.snapshot()  # _prechange_snapshot = current state ("secret")
        result = server.to_objectchange("update")
        self.assertEqual(result.postchange_data["ca_password"], CENSOR_TOKEN)
        self.assertNotEqual(result.postchange_data["ca_password"], CENSOR_TOKEN_CHANGED)

    def test_changed_password_masked_as_censor_token_changed(self):
        """When post password differs from pre password, post shows CENSOR_TOKEN_CHANGED."""
        from netbox.constants import CENSOR_TOKEN_CHANGED

        server = self._make_server(ca_password="old-secret")
        server.snapshot()  # pre-state: "old-secret"
        server.ca_password = "new-secret"  # change in-memory; don't save
        result = server.to_objectchange("update")
        self.assertEqual(result.postchange_data["ca_password"], CENSOR_TOKEN_CHANGED)

    def test_pre_password_always_masked_as_censor_token(self):
        """prechange_data password is always replaced with CENSOR_TOKEN."""
        from netbox.constants import CENSOR_TOKEN

        server = self._make_server(ca_password="old-secret")
        server.snapshot()
        server.ca_password = "new-secret"
        result = server.to_objectchange("update")
        self.assertEqual(result.prechange_data["ca_password"], CENSOR_TOKEN)

    def test_no_prechange_data_does_not_raise(self):
        """to_objectchange("create") has no _prechange_snapshot → prechange_data is None,
        postchange password is masked as CENSOR_TOKEN_CHANGED (nothing to compare against)."""
        from netbox.constants import CENSOR_TOKEN_CHANGED

        server = self._make_server(ca_password="secret")
        # No snapshot() → no _prechange_snapshot attribute → prechange_data stays None.
        result = server.to_objectchange("create")
        self.assertIsNone(result.prechange_data)
        masked = result.postchange_data.get("ca_password")
        self.assertNotEqual(masked, "secret", "Password must be masked in postchange_data")
        self.assertEqual(masked, CENSOR_TOKEN_CHANGED)

    def test_dhcp4_password_masked_in_change_log(self):
        """dhcp4_password is censored in both prechange and postchange data."""
        from netbox.constants import CENSOR_TOKEN

        server = self._make_server(dhcp4_password="v4-secret")
        server.snapshot()
        result = server.to_objectchange("update")
        self.assertEqual(result.prechange_data["dhcp4_password"], CENSOR_TOKEN)
        self.assertEqual(result.postchange_data["dhcp4_password"], CENSOR_TOKEN)

    def test_dhcp6_password_masked_in_change_log(self):
        """dhcp6_password is censored in both prechange and postchange data."""
        from netbox.constants import CENSOR_TOKEN

        server = self._make_server(dhcp6_password="v6-secret")
        server.snapshot()
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
    def test_dhcp6_value_error_raises_reachability_message(self):
        """ValueError during DHCPv6 check must raise 'Unable to reach' message."""
        server = _make_server(dhcp4=False, dhcp6=True)
        with stub_kea({"version-get": ValueError("bad response")}):
            with self.assertRaises(ValidationError) as ctx:
                server.clean()
        msg = str(ctx.exception.message_dict.get("dhcp6", [""])[0])
        self.assertIn("Unable to reach", msg)
        self.assertNotIn("internal error", msg)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp4_value_error_raises_reachability_message(self):
        """ValueError during DHCPv4 check must raise 'Unable to reach' message."""
        server = _make_server(dhcp4=True, dhcp6=False)
        with stub_kea({"version-get": ValueError("bad response")}):
            with self.assertRaises(ValidationError) as ctx:
                server.clean()
        msg = str(ctx.exception.message_dict.get("dhcp4", [""])[0])
        self.assertIn("Unable to reach", msg)
        self.assertNotIn("internal error", msg)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp6_json_decode_error_raises_internal_error_message(self):
        """JSONDecodeError during DHCPv6 check must raise 'An internal error occurred' message."""
        server = _make_server(dhcp4=False, dhcp6=True)
        with stub_kea({"version-get": requests.exceptions.JSONDecodeError("Expecting value", "", 0)}):
            with self.assertRaises(ValidationError) as ctx:
                server.clean()
        msg = str(ctx.exception.message_dict.get("dhcp6", [""])[0])
        self.assertIn("internal error", msg)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    def test_dhcp4_json_decode_error_raises_internal_error_message(self):
        """JSONDecodeError during DHCPv4 check must raise 'An internal error occurred' message."""
        server = _make_server(dhcp4=True, dhcp6=False)
        with stub_kea({"version-get": requests.exceptions.JSONDecodeError("Expecting value", "", 0)}):
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

    def test_backfill_applies_disabled_fields_once(self):
        """When an existing row has backfill_applied=False and PLUGINS_CONFIG disables a
        field that is still True in the DB, SyncConfig.get() must set that field to False
        and mark backfill_applied=True so subsequent calls do not reset UI overrides."""
        SyncConfig.objects.create(
            pk=1,
            interval_minutes=5,
            sync_prefixes_enabled=True,  # DB is True (migration default)
            backfill_applied=False,  # not yet backfilled
        )
        plugins_cfg = {
            "netbox_kea": {
                "sync_prefixes_enabled": False,  # operator disabled this in PLUGINS_CONFIG
            }
        }
        with override_settings(PLUGINS_CONFIG=plugins_cfg):
            cfg = SyncConfig.get()

        # Backfill must have set sync_prefixes_enabled=False
        self.assertFalse(cfg.sync_prefixes_enabled)
        # And persisted the marker so it won't run again
        self.assertTrue(cfg.backfill_applied)
        # Verify the DB row was actually updated
        cfg.refresh_from_db()
        self.assertFalse(cfg.sync_prefixes_enabled)
        self.assertTrue(cfg.backfill_applied)

    def test_backfill_does_not_run_when_already_applied(self):
        """Once backfill_applied=True, SyncConfig.get() must not override UI-set values."""
        SyncConfig.objects.create(
            pk=1,
            interval_minutes=5,
            sync_prefixes_enabled=True,  # user set this to True via UI
            backfill_applied=True,  # already backfilled
        )
        plugins_cfg = {
            "netbox_kea": {
                "sync_prefixes_enabled": False,  # operator config says False
            }
        }
        with override_settings(PLUGINS_CONFIG=plugins_cfg):
            cfg = SyncConfig.get()

        # The UI override (True) must be preserved — backfill must NOT run again
        self.assertTrue(cfg.sync_prefixes_enabled)


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


class TestGetKeaTimeout(SimpleTestCase):
    """Tests for _get_kea_timeout() helper."""

    @override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": 10}})
    def test_returns_configured_value(self):
        self.assertEqual(_get_kea_timeout(), 10)

    @override_settings(PLUGINS_CONFIG={"netbox_kea": {}})
    def test_returns_default_when_key_missing(self):
        self.assertEqual(_get_kea_timeout(), 30)

    @override_settings(PLUGINS_CONFIG={})
    def test_returns_default_when_netbox_kea_section_missing(self):
        self.assertEqual(_get_kea_timeout(), 30)

    @override_settings(PLUGINS_CONFIG={"netbox_kea": None})
    def test_returns_default_when_netbox_kea_is_none(self):
        """PLUGINS_CONFIG["netbox_kea"]=None must not raise AttributeError."""
        self.assertEqual(_get_kea_timeout(), 30)

    @override_settings(PLUGINS_CONFIG=None)
    def test_returns_default_when_plugins_config_is_none(self):
        self.assertEqual(_get_kea_timeout(), 30)

    @override_settings(PLUGINS_CONFIG="not-a-dict")
    def test_returns_default_when_plugins_config_is_not_dict(self):
        self.assertEqual(_get_kea_timeout(), 30)

    @override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": "abc"}})
    def test_returns_default_when_kea_timeout_is_non_numeric_string(self):
        self.assertEqual(_get_kea_timeout(), 30)

    @override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": "15"}})
    def test_accepts_numeric_string(self):
        self.assertEqual(_get_kea_timeout(), 15)

    @override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": None}})
    def test_returns_default_when_kea_timeout_is_none(self):
        self.assertEqual(_get_kea_timeout(), 30)

    def test_custom_default(self):
        with self.settings(PLUGINS_CONFIG={}):
            self.assertEqual(_get_kea_timeout(default=60), 60)
