# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for Phase 3: NetBox IPAM sync endpoints.

URL names (all registered in netbox_kea/urls.py):
  server_lease4_sync       — POST /servers/<pk>/leases4/sync/
  server_lease6_sync       — POST /servers/<pk>/leases6/sync/
  server_reservation4_sync — POST /servers/<pk>/reservations4/sync/
  server_reservation6_sync — POST /servers/<pk>/reservations6/sync/

Each endpoint accepts POST with:
  ip_address   — host IP to sync
  hostname     — (optional) hostname / dns_name
  status       — "active" (leases) or "reserved" (reservations)

Returns an HTMX HTML fragment (<td> content) with a link to the new/updated
NetBox IPAddress, or an error message if something went wrong.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress as NbIP

from netbox_kea.models import Server

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_server(**kwargs) -> Server:
    defaults = {
        "name": "sync-test-kea",
        "ca_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _SyncViewBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="sync_testuser",
            email="sync_test@example.com",
            password="sync_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_server()


# ─────────────────────────────────────────────────────────────────────────────
# TestLease4SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease4SyncView(_SyncViewBase):
    """POST to server_lease4_sync creates/updates a NetBox IPAddress."""

    def setUp(self):
        super().setUp()
        self._kea_patcher = patch("netbox_kea.models.KeaClient")
        self._mock_kea = self._kea_patcher.start()
        self._mock_kea.return_value.lease_get_by_ip.side_effect = lambda ver, ip: {
            "ip-address": ip,
            "hostname": "mock-host.local",
            "hw-address": "aa:bb:cc:00:00:01",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }

    def tearDown(self):
        self._kea_patcher.stop()
        super().tearDown()

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(self._url(), {"ip_address": "192.168.10.5", "hostname": "host-a"})
        self.assertEqual(response.status_code, 200)

    def test_creates_netbox_ip_on_post(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.6", "hostname": "host-b"})
        self.assertTrue(NbIP.objects.filter(address__startswith="192.168.10.6/").exists())

    def test_created_ip_has_dhcp_status(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.7", "hostname": "host-c"})
        ip = NbIP.objects.filter(address__startswith="192.168.10.7/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "dhcp")

    def test_created_ip_has_correct_dns_name(self):
        # hostname in POST is ignored; dns_name comes from Kea lease data (mock returns "mock-host.local")
        self.client.post(self._url(), {"ip_address": "192.168.10.8", "hostname": "dns-test.local"})
        ip = NbIP.objects.filter(address__startswith="192.168.10.8/").first()
        self.assertEqual(ip.dns_name, "mock-host.local")

    def test_response_contains_ip_link(self):
        response = self.client.post(self._url(), {"ip_address": "192.168.10.9", "hostname": "link-host"})
        self.assertContains(response, "192.168.10.9")
        # Response must contain a link to the NetBox IP detail page
        self.assertContains(response, "/ipam/ip-addresses/")

    def test_returns_400_when_ip_address_missing(self):
        response = self.client.post(self._url(), {"hostname": "no-ip"})
        self.assertEqual(response.status_code, 400)

    def test_idempotent_second_post_does_not_create_duplicate(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.20", "hostname": "idem-host"})
        self.client.post(self._url(), {"ip_address": "192.168.10.20", "hostname": "idem-host"})
        self.assertEqual(NbIP.objects.filter(address__startswith="192.168.10.20/").count(), 1)

    def test_returns_404_for_nonexistent_server(self):
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[99999])
        response = self.client.post(url, {"ip_address": "192.168.10.30", "hostname": "ghost"})
        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self._url(), {"ip_address": "192.168.10.31", "hostname": "anon"})
        # Should redirect to login (3xx) or return 403
        self.assertIn(response.status_code, [302, 403])


# ─────────────────────────────────────────────────────────────────────────────
# TestLease6SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease6SyncView(_SyncViewBase):
    """POST to server_lease6_sync creates/updates a NetBox IPAddress for IPv6."""

    def setUp(self):
        super().setUp()
        self._kea_patcher = patch("netbox_kea.models.KeaClient")
        self._mock_kea = self._kea_patcher.start()
        self._mock_kea.return_value.lease_get_by_ip.side_effect = lambda ver, ip: {
            "ip-address": ip,
            "hostname": "mock-v6.local",
            "duid": "01:02:03:04",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }

    def tearDown(self):
        self._kea_patcher.stop()
        super().tearDown()

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease6_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(
            self._url(),
            {"ip_address": "2001:db8::1", "hostname": "v6host"},
        )
        self.assertEqual(response.status_code, 200)

    def test_creates_netbox_ip_with_slash128_for_ipv6(self):

        self.client.post(
            self._url(),
            {"ip_address": "2001:db8::2", "hostname": "v6host2"},
        )
        ip = NbIP.objects.filter(address__startswith="2001:db8::2/").first()
        self.assertIsNotNone(ip)
        self.assertTrue(str(ip.address).endswith("/128"))

    def test_created_ip_has_dhcp_status(self):

        self.client.post(
            self._url(),
            {"ip_address": "2001:db8::3", "hostname": "v6host3"},
        )
        ip = NbIP.objects.filter(address__startswith="2001:db8::3/").first()
        self.assertEqual(ip.status, "dhcp")


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation4SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4SyncView(_SyncViewBase):
    """POST to server_reservation4_sync creates/updates NetBox IP with status=reserved."""

    def setUp(self):
        super().setUp()
        self._kea_patcher = patch("netbox_kea.models.KeaClient")
        self._mock_kea = self._kea_patcher.start()
        self._mock_kea.return_value.reservation_get_by_ip.side_effect = lambda ver, ip: {
            "ip-address": ip,
            "hostname": "mock-res.local",
            "hw-address": "aa:bb:cc:00:00:02",
            "subnet-id": 1,
        }

    def tearDown(self):
        self._kea_patcher.stop()
        super().tearDown()

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(self._url(), {"ip_address": "10.0.0.50", "hostname": "res-host"})
        self.assertEqual(response.status_code, 200)

    def test_creates_ip_with_reserved_status(self):

        self.client.post(self._url(), {"ip_address": "10.0.0.51", "hostname": "res-host2"})
        ip = NbIP.objects.filter(address__startswith="10.0.0.51/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_sets_dns_name(self):
        # hostname in POST is ignored; dns_name comes from Kea reservation data (mock returns "mock-res.local")
        self.client.post(self._url(), {"ip_address": "10.0.0.52", "hostname": "dns.local"})
        ip = NbIP.objects.filter(address__startswith="10.0.0.52/").first()
        self.assertEqual(ip.dns_name, "mock-res.local")

    def test_response_contains_ip_link(self):
        response = self.client.post(self._url(), {"ip_address": "10.0.0.53", "hostname": "link-res"})
        self.assertContains(response, "10.0.0.53")
        self.assertContains(response, "/ipam/ip-addresses/")

    def test_returns_400_when_ip_missing(self):
        response = self.client.post(self._url(), {"hostname": "no-ip"})
        self.assertEqual(response.status_code, 400)


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation6SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6SyncView(_SyncViewBase):
    """POST to server_reservation6_sync creates/updates NetBox IP for IPv6 reservation."""

    def setUp(self):
        super().setUp()
        self._kea_patcher = patch("netbox_kea.models.KeaClient")
        self._mock_kea = self._kea_patcher.start()
        self._mock_kea.return_value.reservation_get_by_ip.side_effect = lambda ver, ip: {
            "ip-address": ip,
            "hostname": "mock-v6res.local",
            "duid": "01:02:03:04",
            "subnet-id": 1,
        }

    def tearDown(self):
        self._kea_patcher.stop()
        super().tearDown()

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(
            self._url(),
            {"ip_address": "2001:db8:1::50", "hostname": "v6res"},
        )
        self.assertEqual(response.status_code, 200)

    def test_creates_ip_with_reserved_status(self):

        self.client.post(
            self._url(),
            {"ip_address": "2001:db8:1::51", "hostname": "v6res2"},
        )
        ip = NbIP.objects.filter(address__startswith="2001:db8:1::51/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")


# ─────────────────────────────────────────────────────────────────────────────
# TestReservationBulkSyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4BulkSyncView(_SyncViewBase):
    """POST to server_reservation4_bulk_sync syncs all reservations to NetBox."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def test_redirects_after_success(self):
        mock_client = MagicMock()
        mock_client.reservation_get_page.return_value = (
            [{"ip-address": "10.0.10.1", "hostname": "bulk-host", "subnet-id": 1}],
            0,
            0,
        )
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            response = self.client.post(self._url(), follow=False)
        # Must redirect back to reservations page
        self.assertIn(response.status_code, [302, 303])

    def test_creates_netbox_ips_for_all_reservations(self):

        mock_client = MagicMock()
        mock_client.reservation_get_page.return_value = (
            [
                {"ip-address": "10.0.11.1", "hostname": "bulk-1", "subnet-id": 1},
                {"ip-address": "10.0.11.2", "hostname": "bulk-2", "subnet-id": 1},
            ],
            0,
            0,
        )
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            self.client.post(self._url())
        self.assertTrue(NbIP.objects.filter(address__startswith="10.0.11.1/").exists())
        self.assertTrue(NbIP.objects.filter(address__startswith="10.0.11.2/").exists())

    def test_created_ips_have_reserved_status(self):

        mock_client = MagicMock()
        mock_client.reservation_get_page.return_value = (
            [{"ip-address": "10.0.12.1", "hostname": "bulk-rsv", "subnet-id": 1}],
            0,
            0,
        )
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            self.client.post(self._url())
        ip = NbIP.objects.filter(address__startswith="10.0.12.1/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_returns_404_for_nonexistent_server(self):
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[99999])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, [302, 403])


# ─────────────────────────────────────────────────────────────────────────────
# Issue #9: Authorization checks before IPAM sync mutations
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncViewPermissionChecks(_SyncViewBase):
    """Sync endpoints must reject users without IPAM write permissions."""

    def setUp(self):
        super().setUp()
        # Create a non-privileged user with no IPAM permissions
        self.limited_user = User.objects.create_user(
            username="limited_sync_user",
            email="limited@example.com",
            password="limitedpass",
        )

    def _login_limited(self):
        self.client.logout()
        self.client.force_login(self.limited_user)

    def test_lease4_sync_requires_ipam_add_permission(self):
        self._login_limited()
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        response = self.client.post(url, {"ip_address": "192.168.99.1"})
        self.assertEqual(response.status_code, 403)

    def test_reservation4_sync_requires_ipam_add_permission(self):
        self._login_limited()
        url = reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk])
        response = self.client.post(url, {"ip_address": "192.168.99.2"})
        self.assertEqual(response.status_code, 403)

    @patch("netbox_kea.models.KeaClient")
    def test_superuser_can_still_sync(self, MockKeaClient):
        # self.user is superuser — should succeed as before
        MockKeaClient.return_value.lease_get_by_ip.side_effect = lambda ver, ip: {
            "ip-address": ip,
            "hostname": "mock-host.local",
            "hw-address": "aa:bb:cc:00:00:01",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        response = self.client.post(url, {"ip_address": "192.168.99.3"})
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation6BulkSyncView  (issue #13)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6BulkSyncView(_SyncViewBase):
    """POST to server_reservation6_bulk_sync syncs all v6 reservations to NetBox."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_bulk_syncs_v6_reservations(self, MockKeaClient):
        """Bulk sync v6 reservation creates an IPAddress with /128 prefix and reserved status."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get_page.return_value = (
            [
                {
                    "subnet-id": 1,
                    "duid": "00:01:aa:bb",
                    "ip-addresses": ["2001:db8::1"],
                    "hostname": "host-v6",
                }
            ],
            0,
            0,
        )
        self.client.post(self._url())
        ip = NbIP.objects.filter(address__startswith="2001:db8::1").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")
        self.assertIn("/128", str(ip.address))

    def test_post_unauthenticated_redirects(self):
        self.client.logout()
        response = self.client.post(self._url(), content_type="application/json")
        self.assertEqual(response.status_code, 302)

    def test_post_nonexistent_server_returns_404(self):
        url = reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[99999])
        response = self.client.post(url, content_type="application/json")
        self.assertEqual(response.status_code, 404)
