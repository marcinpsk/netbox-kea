# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for Phase 3c: IPAddress → Kea Reservation.

A dedicated view is registered at::

    GET /plugins/kea/ip-addresses/<id>/kea-reservations/

It shows available Kea servers for the IP's protocol version and provides
pre-filled "Create reservation" links for each server.

URL name: ``plugins:netbox_kea:ipaddress_kea_reservations``
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import Server

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


def _make_server(name, dhcp4=True, dhcp6=False):
    return Server.objects.create(
        name=name,
        ca_url="http://kea.example.com",
        dhcp4=dhcp4,
        dhcp6=dhcp6,
    )


def _make_ip(address, dns_name=""):
    from ipam.models import IPAddress

    return IPAddress.objects.create(address=address, dns_name=dns_name)


def _url(pk):
    return reverse("plugins:netbox_kea:ipaddress_kea_reservations", args=[pk])


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestIPAddressKeaViewAuth(TestCase):
    """Login and 404 behaviour."""

    def setUp(self):
        self.user = User.objects.create_superuser("tester", password="pass")

    def test_login_required(self):
        nb_ip = _make_ip("10.0.0.1/24")
        response = self.client.get(_url(nb_ip.pk))
        self.assertIn(response.status_code, [302, 403])

    def test_404_for_nonexistent_ip(self):
        self.client.force_login(self.user)
        response = self.client.get(_url(99999))
        self.assertEqual(response.status_code, 404)

    def test_returns_200_for_valid_ip(self):
        self.client.force_login(self.user)
        nb_ip = _make_ip("10.0.0.2/24")
        response = self.client.get(_url(nb_ip.pk))
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# IPv4 address — server visibility
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestIPv4AddressPanel(TestCase):
    """For an IPv4 address only dhcp4-enabled servers appear."""

    def setUp(self):
        self.user = User.objects.create_superuser("tester4", password="pass")
        self.client.force_login(self.user)
        self.v4server = _make_server("kea-v4", dhcp4=True, dhcp6=False)
        self.v6server = _make_server("kea-v6", dhcp4=False, dhcp6=True)
        self.nb_ip = _make_ip("10.0.1.1/24", dns_name="host.example.com")

    def test_shows_dhcp4_server(self):
        response = self.client.get(_url(self.nb_ip.pk))
        self.assertContains(response, self.v4server.name)

    def test_hides_dhcp6_only_server(self):
        response = self.client.get(_url(self.nb_ip.pk))
        self.assertNotContains(response, self.v6server.name)

    def test_create_link_uses_reservation4_add_url(self):
        response = self.client.get(_url(self.nb_ip.pk))
        expected_base = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.v4server.pk])
        self.assertContains(response, expected_base)

    def test_create_link_has_ip_address_param(self):
        response = self.client.get(_url(self.nb_ip.pk))
        # The plain IP without prefix length should be in the query string
        self.assertContains(response, "ip_address=10.0.1.1")

    def test_create_link_has_hostname_param(self):
        response = self.client.get(_url(self.nb_ip.pk))
        self.assertContains(response, "hostname=host.example.com")

    def test_shows_empty_state_when_no_v4_servers(self):
        self.v4server.delete()
        response = self.client.get(_url(self.nb_ip.pk))
        self.assertEqual(response.status_code, 200)
        # Should not error; page shows some kind of "no servers" state
        self.assertNotContains(response, self.v6server.name)


# ─────────────────────────────────────────────────────────────────────────────
# IPv6 address — server visibility
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestIPv6AddressPanel(TestCase):
    """For an IPv6 address only dhcp6-enabled servers appear."""

    def setUp(self):
        self.user = User.objects.create_superuser("tester6", password="pass")
        self.client.force_login(self.user)
        self.v4server = _make_server("kea-v4-only", dhcp4=True, dhcp6=False)
        self.v6server = _make_server("kea-v6-only", dhcp4=False, dhcp6=True)
        self.nb_ip = _make_ip("2001:db8::1/64", dns_name="host6.example.com")

    def test_shows_dhcp6_server(self):
        response = self.client.get(_url(self.nb_ip.pk))
        self.assertContains(response, self.v6server.name)

    def test_hides_dhcp4_only_server(self):
        response = self.client.get(_url(self.nb_ip.pk))
        self.assertNotContains(response, self.v4server.name)

    def test_create_link_uses_reservation6_add_url(self):
        response = self.client.get(_url(self.nb_ip.pk))
        expected_base = reverse("plugins:netbox_kea:server_reservation6_add", args=[self.v6server.pk])
        self.assertContains(response, expected_base)

    def test_create_link_has_ipv6_address_param(self):
        response = self.client.get(_url(self.nb_ip.pk))
        self.assertContains(response, "ip_addresses=2001")

    def test_create_link_has_hostname_param(self):
        response = self.client.get(_url(self.nb_ip.pk))
        self.assertContains(response, "hostname=host6.example.com")


# ─────────────────────────────────────────────────────────────────────────────
# Dual-stack server (dhcp4=True, dhcp6=True)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualStackServer(TestCase):
    """A server with both dhcp4+dhcp6 appears for both address families."""

    def setUp(self):
        self.user = User.objects.create_superuser("tester_dual", password="pass")
        self.client.force_login(self.user)
        self.dual = _make_server("kea-dual", dhcp4=True, dhcp6=True)

    def test_dual_server_shown_for_ipv4(self):
        nb_ip = _make_ip("10.0.2.1/24")
        response = self.client.get(_url(nb_ip.pk))
        self.assertContains(response, self.dual.name)

    def test_dual_server_shown_for_ipv6(self):
        nb_ip = _make_ip("2001:db8::2/64")
        response = self.client.get(_url(nb_ip.pk))
        self.assertContains(response, self.dual.name)


# ─────────────────────────────────────────────────────────────────────────────
# IP without dns_name
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestIPWithoutHostname(TestCase):
    """When dns_name is empty the hostname param is omitted or empty."""

    def setUp(self):
        self.user = User.objects.create_superuser("tester_no_dns", password="pass")
        self.client.force_login(self.user)
        _make_server("kea-nodns", dhcp4=True)

    def test_page_renders_without_hostname(self):
        nb_ip = _make_ip("10.0.3.1/24", dns_name="")
        response = self.client.get(_url(nb_ip.pk))
        self.assertEqual(response.status_code, 200)

    def test_link_still_has_ip_address_param(self):
        nb_ip = _make_ip("10.0.3.2/24", dns_name="")
        response = self.client.get(_url(nb_ip.pk))
        self.assertContains(response, "ip_address=10.0.3.2")
