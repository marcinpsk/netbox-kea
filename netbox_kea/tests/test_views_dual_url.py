# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Dual-URL regression tests for ``get_client(version=)`` routing in views.

When a Server has distinct ``dhcp4_url`` and ``dhcp6_url`` fields, every view
that calls ``server.get_client(version=self.dhcp_version)`` must construct
``KeaClient`` with the protocol-specific URL rather than falling back to
``ca_url``.

These tests create a server with all three URLs set to different values, then
hit representative views for each protocol version and assert that
``KeaClient.__init__`` received the expected URL.

Closes: https://github.com/marcinpsk/netbox-kea/issues/40
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import Server

from .utils import _PLUGINS_CONFIG, User, _kea_command_side_effect

# Distinct URLs so we can tell which one was selected.
_SERVER_URL = "https://kea-default.example.com"
_DHCP4_URL = "https://kea-v4.example.com:8001"
_DHCP6_URL = "https://kea-v6.example.com:8002"


def _make_dual_url_server(**kwargs) -> Server:
    """Create a Server with distinct v4, v6, and default URLs."""
    defaults = {
        "name": "dual-url-server",
        "ca_url": _SERVER_URL,
        "dhcp4_url": _DHCP4_URL,
        "dhcp6_url": _DHCP6_URL,
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


def _assert_keaclient_url(test_case, MockKeaClient, expected_url):
    """Assert KeaClient was instantiated with the expected URL at least once."""
    test_case.assertTrue(
        MockKeaClient.called,
        "KeaClient was never instantiated — the view may not have called get_client().",
    )
    actual_urls = []
    for call in MockKeaClient.call_args_list:
        url = call.kwargs.get("url") if call.kwargs else None
        if url is None and call.args:
            url = call.args[0]
        actual_urls.append(url)
    test_case.assertIn(
        expected_url,
        actual_urls,
        f"KeaClient was never instantiated with {expected_url!r}. All calls: {MockKeaClient.call_args_list}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class _DualURLBase(TestCase):
    """Shared setUp for dual-URL view tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="dual_url_user",
            email="test@example.com",
            password="testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_dual_url_server()


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLSubnetViews(_DualURLBase):
    """Subnet views must use dhcp4_url for v4 and dhcp6_url for v6."""

    @patch("netbox_kea.models.KeaClient")
    def test_subnets4_uses_dhcp4_url(self, MockKeaClient):
        """GET subnets4 → KeaClient constructed with dhcp4_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _DHCP4_URL)

    @patch("netbox_kea.models.KeaClient")
    def test_subnets6_uses_dhcp6_url(self, MockKeaClient):
        """GET subnets6 → KeaClient constructed with dhcp6_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _DHCP6_URL)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLLeaseViews(_DualURLBase):
    """Lease views must use dhcp4_url for v4 and dhcp6_url for v6.

    Lease views only instantiate KeaClient when performing a search via
    the ``export`` GET param, so we trigger an export to exercise
    ``get_client(version=...)``.
    """

    @patch("netbox_kea.models.KeaClient")
    def test_leases4_uses_dhcp4_url(self, MockKeaClient):
        """Export leases4 → KeaClient constructed with dhcp4_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self.client.get(url, {"export": "1", "by": "subnet", "q": "10.0.0.0/24"})
        self.assertIn(response.status_code, (200, 302))
        _assert_keaclient_url(self, MockKeaClient, _DHCP4_URL)

    @patch("netbox_kea.models.KeaClient")
    def test_leases6_uses_dhcp6_url(self, MockKeaClient):
        """Export leases6 → KeaClient constructed with dhcp6_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])
        response = self.client.get(url, {"export": "1", "by": "subnet", "q": "2001:db8::/64"})
        self.assertIn(response.status_code, (200, 302))
        _assert_keaclient_url(self, MockKeaClient, _DHCP6_URL)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLOptionViews(_DualURLBase):
    """Option views must use dhcp4_url for v4 and dhcp6_url for v6."""

    @patch("netbox_kea.models.KeaClient")
    def test_option_defs4_uses_dhcp4_url(self, MockKeaClient):
        """GET option-defs4 → KeaClient constructed with dhcp4_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _DHCP4_URL)

    @patch("netbox_kea.models.KeaClient")
    def test_option_defs6_uses_dhcp6_url(self, MockKeaClient):
        """GET option-defs6 → KeaClient constructed with dhcp6_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_option_def6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _DHCP6_URL)

    @patch("netbox_kea.models.KeaClient")
    def test_server_options4_uses_dhcp4_url(self, MockKeaClient):
        """GET server-options4 → KeaClient constructed with dhcp4_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _DHCP4_URL)

    @patch("netbox_kea.models.KeaClient")
    def test_server_options6_uses_dhcp6_url(self, MockKeaClient):
        """GET server-options6 → KeaClient constructed with dhcp6_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_dhcp6_options_edit", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _DHCP6_URL)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLReservationViews(_DualURLBase):
    """Reservation views must use dhcp4_url for v4 and dhcp6_url for v6."""

    @patch("netbox_kea.models.KeaClient")
    def test_reservations4_uses_dhcp4_url(self, MockKeaClient):
        """GET reservations4 → KeaClient constructed with dhcp4_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _DHCP4_URL)

    @patch("netbox_kea.models.KeaClient")
    def test_reservations6_uses_dhcp6_url(self, MockKeaClient):
        """GET reservations6 → KeaClient constructed with dhcp6_url."""
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _DHCP6_URL)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLFallback(_DualURLBase):
    """When protocol-specific URL is not set, fall back to ca_url."""

    @patch("netbox_kea.models.KeaClient")
    def test_v4_falls_back_to_server_url(self, MockKeaClient):
        """When dhcp4_url is empty, v4 views use ca_url."""
        server = _make_dual_url_server(name="v4-fallback", dhcp4_url="", dhcp6_url=_DHCP6_URL)
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_subnets4", args=[server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _SERVER_URL)

    @patch("netbox_kea.models.KeaClient")
    def test_v6_falls_back_to_server_url(self, MockKeaClient):
        """When dhcp6_url is empty, v6 views use ca_url."""
        server = _make_dual_url_server(name="v6-fallback", dhcp4_url=_DHCP4_URL, dhcp6_url="")
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        url = reverse("plugins:netbox_kea:server_subnets6", args=[server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        _assert_keaclient_url(self, MockKeaClient, _SERVER_URL)
