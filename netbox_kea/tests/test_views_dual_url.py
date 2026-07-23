# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Dual-URL regression tests for ``get_client(version=)`` routing in views.

When a Server has distinct ``dhcp4_url`` and ``dhcp6_url`` fields, every view
that calls ``server.get_client(version=self.dhcp_version)`` must construct
``KeaClient`` with the protocol-specific URL rather than falling back to
``ca_url``.

These tests create a server with all three URLs set to different values, then
hit representative views for each protocol version and assert — via the real
``KeaClient`` and the HTTP-boundary stub (``kea_stub.stub_kea``) — that the
request was POSTed to the expected endpoint. ``KeaHttpStub.urls()`` records every
endpoint hit, so a dual-URL regression (falling back to ``ca_url``) fails the test.

Closes: https://github.com/marcinpsk/netbox-kea/issues/40
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import Server

from .kea_stub import stub_kea
from .utils import _PLUGINS_CONFIG, User

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


def _config_get(body):
    """A benign ``config-get`` payload (empty subnets/networks/options) for the queried service."""
    svc = (body.get("service") or [""])[0]
    version = 6 if svc == "dhcp6" else 4
    key = f"Dhcp{version}"
    block = {f"subnet{version}": [], "shared-networks": [], "option-data": [], "option-def": []}
    return {"result": 0, "arguments": {key: block}}


def _dual_url_stub():
    """Register the union of commands the representative views issue, all benign.

    Enough for each view to build its per-version client and POST at least once, so
    ``kea.urls()`` records the endpoint. Empty/absent results keep the views on their
    happy path (200) or a graceful redirect (302).
    """
    return stub_kea(
        {
            "config-get": _config_get,
            "stat-lease4-get": {"result": 2, "text": "unknown command"},
            "stat-lease6-get": {"result": 2, "text": "unknown command"},
            "lease4-get-page": {"result": 3},
            "lease6-get-page": {"result": 3},
            "reservation-get-page": {"result": 3},
            "lease4-get-all": {"result": 0, "arguments": {"leases": []}},
            "lease6-get-all": {"result": 0, "arguments": {"leases": []}},
        }
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

    def test_subnets4_uses_dhcp4_url(self):
        """GET subnets4 → request POSTed to dhcp4_url."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_DHCP4_URL, kea.urls())

    def test_subnets6_uses_dhcp6_url(self):
        """GET subnets6 → request POSTed to dhcp6_url."""
        url = reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_DHCP6_URL, kea.urls())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLLeaseViews(_DualURLBase):
    """Lease views must use dhcp4_url for v4 and dhcp6_url for v6.

    Lease views only instantiate KeaClient when performing a search via
    the ``export`` GET param, so we trigger an export to exercise
    ``get_client(version=...)``.
    """

    def test_leases4_uses_dhcp4_url(self):
        """Export leases4 → request POSTed to dhcp4_url."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url, {"export": "1", "by": "subnet", "q": "10.0.0.0/24"})
        self.assertIn(response.status_code, (200, 302))
        self.assertIn(_DHCP4_URL, kea.urls())

    def test_leases6_uses_dhcp6_url(self):
        """Export leases6 → request POSTed to dhcp6_url."""
        url = reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url, {"export": "1", "by": "subnet", "q": "2001:db8::/64"})
        self.assertIn(response.status_code, (200, 302))
        self.assertIn(_DHCP6_URL, kea.urls())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLOptionViews(_DualURLBase):
    """Option views must use dhcp4_url for v4 and dhcp6_url for v6."""

    def test_option_defs4_uses_dhcp4_url(self):
        """GET option-defs4 → request POSTed to dhcp4_url."""
        url = reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_DHCP4_URL, kea.urls())

    def test_option_defs6_uses_dhcp6_url(self):
        """GET option-defs6 → request POSTed to dhcp6_url."""
        url = reverse("plugins:netbox_kea:server_option_def6", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_DHCP6_URL, kea.urls())

    def test_server_options4_uses_dhcp4_url(self):
        """GET server-options4 → request POSTed to dhcp4_url."""
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_DHCP4_URL, kea.urls())

    def test_server_options6_uses_dhcp6_url(self):
        """GET server-options6 → request POSTed to dhcp6_url."""
        url = reverse("plugins:netbox_kea:server_dhcp6_options_edit", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_DHCP6_URL, kea.urls())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLReservationViews(_DualURLBase):
    """Reservation views must use dhcp4_url for v4 and dhcp6_url for v6."""

    def test_reservations4_uses_dhcp4_url(self):
        """GET reservations4 → request POSTed to dhcp4_url."""
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_DHCP4_URL, kea.urls())

    def test_reservations6_uses_dhcp6_url(self):
        """GET reservations6 → request POSTed to dhcp6_url."""
        url = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_DHCP6_URL, kea.urls())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDualURLFallback(_DualURLBase):
    """When protocol-specific URL is not set, fall back to ca_url."""

    def test_v4_falls_back_to_server_url(self):
        """When dhcp4_url is empty, v4 views use ca_url."""
        server = _make_dual_url_server(name="v4-fallback", dhcp4_url="", dhcp6_url=_DHCP6_URL)
        url = reverse("plugins:netbox_kea:server_subnets4", args=[server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_SERVER_URL, kea.urls())

    def test_v6_falls_back_to_server_url(self):
        """When dhcp6_url is empty, v6 views use ca_url."""
        server = _make_dual_url_server(name="v6-fallback", dhcp4_url=_DHCP4_URL, dhcp6_url="")
        url = reverse("plugins:netbox_kea:server_subnets6", args=[server.pk])
        with _dual_url_stub() as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(_SERVER_URL, kea.urls())
