# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for IPAddressKeaPanel.right_page() template extension.

Tests the server filtering, URL generation, and edge cases of the panel
injected onto the NetBox IPAddress detail page.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from netbox_kea.models import Server
from netbox_kea.template_extensions import IPAddressKeaPanel

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


def _make_server(name, dhcp4=True, dhcp6=False):
    return Server.objects.create(
        name=name,
        server_url="http://kea.example.com",
        dhcp4=dhcp4,
        dhcp6=dhcp6,
    )


def _make_nb_ip(ip_str, dns_name="host.example.com", pk=99):
    """Build a mock IPAddress object as IPAddressKeaPanel expects."""
    nb_ip = MagicMock()
    nb_ip.pk = pk
    nb_ip.dns_name = dns_name
    nb_ip.address.ip = ip_str
    return nb_ip


def _make_panel(nb_ip, user=None):
    """Instantiate IPAddressKeaPanel with a minimal fake context."""
    if user is None:
        user = MagicMock()
    context = {
        "object": nb_ip,
        "request": MagicMock(user=user),
    }
    return IPAddressKeaPanel(context)


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases / early returns
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestIPAddressKeaPanelEdgeCases(TestCase):
    """Early-return and degenerate inputs."""

    def test_no_object_in_context_returns_empty_string(self):
        """If context has no 'object', right_page() returns ''."""
        panel = IPAddressKeaPanel({"request": MagicMock()})
        result = panel.right_page()
        self.assertEqual(result, "")

    def test_nb_ip_without_address_returns_empty_string(self):
        """If nb_ip.address is falsy, right_page() returns ''."""
        nb_ip = MagicMock()
        nb_ip.address = None
        panel = _make_panel(nb_ip)
        result = panel.right_page()
        self.assertEqual(result, "")

    def test_nb_ip_address_without_ip_returns_empty_string(self):
        """If nb_ip.address.ip is falsy, right_page() returns ''."""
        nb_ip = MagicMock()
        nb_ip.address.ip = ""
        panel = _make_panel(nb_ip)
        result = panel.right_page()
        self.assertEqual(result, "")


# ─────────────────────────────────────────────────────────────────────────────
# Server filtering
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestIPAddressKeaPanelServerFiltering(TestCase):
    """Server queryset is filtered to the correct DHCP version."""

    def setUp(self):
        self.user = User.objects.create_superuser("tester", password="pass")
        self.v4_server = _make_server("kea-v4", dhcp4=True, dhcp6=False)
        self.v6_only = _make_server("kea-v6", dhcp4=False, dhcp6=True)
        self.dual = _make_server("kea-dual", dhcp4=True, dhcp6=True)

    @patch.object(IPAddressKeaPanel, "render", return_value="")
    def test_v4_ip_only_shows_dhcp4_servers(self, mock_render):
        """An IPv4 address shows only servers with dhcp4=True."""
        nb_ip = _make_nb_ip("10.0.0.1")
        panel = _make_panel(nb_ip, user=self.user)
        panel.right_page()
        extra = mock_render.call_args[1]["extra_context"]
        server_names = [item["server"].name for item in extra["server_links"]]
        self.assertIn("kea-v4", server_names)
        self.assertIn("kea-dual", server_names)
        self.assertNotIn("kea-v6", server_names)

    @patch.object(IPAddressKeaPanel, "render", return_value="")
    def test_v6_ip_only_shows_dhcp6_servers(self, mock_render):
        """An IPv6 address shows only servers with dhcp6=True."""
        nb_ip = _make_nb_ip("2001:db8::1")
        panel = _make_panel(nb_ip, user=self.user)
        panel.right_page()
        extra = mock_render.call_args[1]["extra_context"]
        server_names = [item["server"].name for item in extra["server_links"]]
        self.assertIn("kea-v6", server_names)
        self.assertIn("kea-dual", server_names)
        self.assertNotIn("kea-v4", server_names)

    @patch.object(IPAddressKeaPanel, "render", return_value="")
    def test_no_matching_servers_passes_empty_list(self, mock_render):
        """When no servers match the version, server_links is empty."""
        # Remove all v6-capable servers
        self.v6_only.delete()
        self.dual.delete()
        nb_ip = _make_nb_ip("2001:db8::99")
        panel = _make_panel(nb_ip, user=self.user)
        panel.right_page()
        extra = mock_render.call_args[1]["extra_context"]
        self.assertEqual(extra["server_links"], [])

    @patch.object(IPAddressKeaPanel, "render", return_value="")
    def test_version_in_context_matches_ip_family(self, mock_render):
        """The 'version' key passed to render matches the IP address family."""
        for ip, expected_version in [("10.0.0.1", 4), ("2001:db8::1", 6)]:
            with self.subTest(ip=ip):
                nb_ip = _make_nb_ip(ip)
                panel = _make_panel(nb_ip, user=self.user)
                panel.right_page()
                extra = mock_render.call_args[1]["extra_context"]
                self.assertEqual(extra["version"], expected_version)


# ─────────────────────────────────────────────────────────────────────────────
# URL generation
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestIPAddressKeaPanelUrls(TestCase):
    """Pre-filled URL query parameters are correct for v4 and v6."""

    def setUp(self):
        self.user = User.objects.create_superuser("tester", password="pass")
        _make_server("kea-v4", dhcp4=True, dhcp6=False)
        _make_server("kea-v6", dhcp4=False, dhcp6=True)

    @patch.object(IPAddressKeaPanel, "render", return_value="")
    def test_v4_link_contains_ip_address_param(self, mock_render):
        """v4 links use query param `ip_address=<ip>`."""
        nb_ip = _make_nb_ip("192.168.1.50", dns_name="srv.example.com")
        panel = _make_panel(nb_ip, user=self.user)
        panel.right_page()
        extra = mock_render.call_args[1]["extra_context"]
        url = extra["server_links"][0]["url"]
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(qs["ip_address"], ["192.168.1.50"])
        self.assertEqual(qs["hostname"], ["srv.example.com"])

    @patch.object(IPAddressKeaPanel, "render", return_value="")
    def test_v6_link_contains_ip_addresses_param(self, mock_render):
        """v6 links use query param `ip_addresses=<ip>` (plural)."""
        nb_ip = _make_nb_ip("2001:db8::1", dns_name="v6host.example.com")
        panel = _make_panel(nb_ip, user=self.user)
        panel.right_page()
        extra = mock_render.call_args[1]["extra_context"]
        url = extra["server_links"][0]["url"]
        qs = parse_qs(urlparse(url).query)
        self.assertIn("ip_addresses", qs)
        self.assertNotIn("ip_address", qs)
        self.assertEqual(qs["ip_addresses"], ["2001:db8::1"])
