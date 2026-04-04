# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for Phase 2: Reservation Management.

URL names tested (all registered and working):
  server_reservations4         — GET  /servers/<pk>/reservations4/
  server_reservations6         — GET  /servers/<pk>/reservations6/
  server_reservation4_add      — GET/POST /servers/<pk>/reservations4/add/
  server_reservation6_add      — GET/POST /servers/<pk>/reservations6/add/
  server_reservation4_edit     — GET/POST /servers/<pk>/reservations4/<subnet_id>/<ip>/edit/
  server_reservation6_edit     — GET/POST /servers/<pk>/reservations6/<subnet_id>/<ip>/edit/
  server_reservation4_delete   — GET/POST /servers/<pk>/reservations4/<subnet_id>/<ip>/delete/
  server_reservation6_delete   — GET/POST /servers/<pk>/reservations6/<subnet_id>/<ip>/delete/
"""

import io
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.messages import WARNING, get_messages
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from netbox_kea.kea import KeaException, PartialPersistError
from netbox_kea.models import Server
from netbox_kea.views import _filter_reservations

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

# ─────────────────────────────────────────────────────────────────────────────
# Sample reservation fixtures
# ─────────────────────────────────────────────────────────────────────────────

_SAMPLE_RESERVATION4 = {
    "subnet-id": 1,
    "hw-address": "aa:bb:cc:dd:ee:ff",
    "ip-address": "192.168.1.100",
    "hostname": "testhost.example.com",
}

_SAMPLE_RESERVATION6 = {
    "subnet-id": 1,
    "duid": "00:01:02:03:04:05",
    "ip-addresses": ["2001:db8::100"],
    "hostname": "testhost6.example.com",
}

_RESERVATION_COMMANDS = {
    "reservation-add",
    "reservation-get-page",
    "reservation-del",
    "reservation-update",
    "reservation-get",
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_db_server(**kwargs) -> Server:
    """Create a Server without triggering live Kea connectivity checks."""
    defaults = {
        "name": "test-kea-reservations",
        "server_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


def _wire_mock_clone(mock_client):
    """Wire clone/context-manager on a mock KeaClient so worker threads see the same instance."""
    mock_client.clone.return_value = mock_client
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = lambda s, *a: None
    return mock_client


# ─────────────────────────────────────────────────────────────────────────────
# Shared base
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _ReservationViewBase(TestCase):
    """Creates a superuser and a dual-stack Server for all reservation view tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="res_testuser",
            email="res_test@example.com",
            password="res_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def _mock_client_with_reservations4(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_get_page.return_value = ([dict(_SAMPLE_RESERVATION4)], 0, 0)
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [], "count": 0}}]
        return mock_client

    def _mock_client_with_reservations6(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_get_page.return_value = ([dict(_SAMPLE_RESERVATION6)], 0, 0)
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [], "count": 0}}]
        return mock_client


# ─────────────────────────────────────────────────────────────────────────────
# TestServerReservations4View
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservations4View(_ReservationViewBase):
    """GET /plugins/kea/servers/<pk>/reservations4/"""

    @patch("netbox_kea.models.KeaClient")
    def test_list_returns_200(self, MockKeaClient):
        self._mock_client_with_reservations4(MockKeaClient)
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_list_shows_reservations_in_table(self, MockKeaClient):
        self._mock_client_with_reservations4(MockKeaClient)
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "192.168.1.100")
        self.assertContains(response, "aa:bb:cc:dd:ee:ff")

    @patch("netbox_kea.models.KeaClient")
    def test_list_when_hook_not_loaded_shows_warning(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get_page.side_effect = KeaException(
            {"result": 2, "text": "unknown command 'reservation-get-page'"},
            index=0,
        )
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        # Must not crash with 500; show the page with a warning indicator
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["hook_available"])

    @patch("netbox_kea.models.KeaClient")
    def test_general_kea_error_keeps_hook_available(self, MockKeaClient):
        """Result code 1 (general Kea error) keeps hook_available=True.

        Only result==2 (unknown command = hook not loaded) should set
        hook_available=False.  Other errors are transient/backend failures.
        """
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get_page.side_effect = KeaException(
            {"result": 1, "text": "missing parameter 'limit'"},
            index=0,
        )
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["hook_available"])

    @patch("netbox_kea.models.KeaClient")
    def test_list_handles_empty_reservations(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_nonexistent_server_returns_404(self):
        url = reverse("plugins:netbox_kea:server_reservations4", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_drains_multiple_pages_from_kea(self, MockKeaClient):
        """View must call reservation_get_page in a loop until all pages are fetched."""
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        page2 = [dict(_SAMPLE_RESERVATION4, **{"ip-address": "10.0.1.1", "subnet-id": 1})]
        # Simulate drain: side_effect returns full page then partial page.
        # The view uses limit=100 internally, so page1 has < 100 items and will
        # be detected as the last page after 1 call — use side_effect to control
        # the 2-call sequence explicitly via a larger page1.
        page1_full = [
            dict(_SAMPLE_RESERVATION4, **{"ip-address": f"10.0.0.{i}", "subnet-id": 1})
            for i in range(1, 101)  # exactly 100 items == limit → loop continues
        ]
        mock_client.reservation_get_page.side_effect = [
            (page1_full, 100, 0),
            (page2, 0, 0),
        ]
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [], "count": 0}}]
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The crucial assertion: view made exactly 2 calls (drain loop worked)
        self.assertEqual(mock_client.reservation_get_page.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_reservation_table_data_has_ip_sort_key(self, MockKeaClient):
        """F1: each reservation dict in the table must have an integer _ip_sort_key."""
        self._mock_client_with_reservations4(MockKeaClient)
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        for row in table.data:
            self.assertIn("_ip_sort_key", row, "Missing _ip_sort_key in reservation row")
            self.assertIsInstance(row["_ip_sort_key"], int)


# ─────────────────────────────────────────────────────────────────────────────
# TestServerReservations6View
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservations6View(_ReservationViewBase):
    """GET /plugins/kea/servers/<pk>/reservations6/"""

    @patch("netbox_kea.models.KeaClient")
    def test_list_returns_200(self, MockKeaClient):
        self._mock_client_with_reservations6(MockKeaClient)
        url = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_list_shows_reservations_in_table(self, MockKeaClient):
        self._mock_client_with_reservations6(MockKeaClient)
        url = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2001:db8::100")

    @patch("netbox_kea.models.KeaClient")
    def test_list_handles_empty_reservations(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_get_page.return_value = ([], 0, 0)
        url = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_nonexistent_server_returns_404(self):
        url = reverse("plugins:netbox_kea:server_reservations6", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    @patch("netbox_kea.models.KeaClient")
    def test_drains_multiple_pages_from_kea(self, MockKeaClient):
        """View must call reservation_get_page in a loop until all pages are fetched."""
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        page2 = [dict(_SAMPLE_RESERVATION6, **{"ip-addresses": ["2001:db8::ff01"], "subnet-id": 1})]
        mock_client.reservation_get_page.side_effect = [
            (
                [
                    dict(_SAMPLE_RESERVATION6, **{"ip-addresses": [f"2001:db8::{i:x}"], "subnet-id": 1})
                    for i in range(100)
                ],
                100,
                0,
            ),
            (page2, 0, 0),
        ]
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [], "count": 0}}]
        url = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_client.reservation_get_page.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_action_hrefs_contain_ipv6_address(self, MockKeaClient):
        """Edit/delete action links must embed the IPv6 address in the URL path (issue #12)."""
        self._mock_client_with_reservations6(MockKeaClient)
        url = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        ip = _SAMPLE_RESERVATION6["ip-addresses"][0]
        subnet_id = _SAMPLE_RESERVATION6["subnet-id"]
        # The rendered action URLs must include the subnet_id and IP in path position
        self.assertContains(response, f"/reservations6/{subnet_id}/{ip}/edit/")
        self.assertContains(response, f"/reservations6/{subnet_id}/{ip}/delete/")


# ─────────────────────────────────────────────────────────────────────────────
# TestServerReservation4AddView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservation4AddView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/reservations4/add/"""

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    def _valid_post_data(self):
        return {
            "subnet_id": 1,
            "ip_address": "192.168.1.100",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
        }

    @patch("netbox_kea.models.KeaClient")
    def test_get_renders_form(self, MockKeaClient):
        response = self.client.get(self._add_url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_creates_reservation_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        response = self.client.post(self._add_url(), self._valid_post_data())
        self.assertEqual(response.status_code, 302)
        # Must redirect to the server's reservations page (not to /None/)
        self.assertNotIn("None", response.url)
        mock_client.reservation_add.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_rerenders_form(self, MockKeaClient):
        # Empty POST — all required fields missing
        response = self.client.post(self._add_url(), {})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_missing_ip_address_rerenders_form(self, MockKeaClient):
        data = self._valid_post_data()
        del data["ip_address"]
        response = self.client.post(self._add_url(), data)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_error_shows_error_message(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "failed to add host: conflicts with existing reservation"},
            index=0,
        )
        response = self.client.post(self._add_url(), self._valid_post_data())
        # Must not crash with 500; either re-render (200) or redirect with error
        self.assertIn(response.status_code, (200, 302))

    # ── F4: reservation-in-pool overlap warning ───────────────────────────────

    @patch("netbox_kea.models.KeaClient")
    def test_post_warns_when_reservation_ip_inside_pool(self, MockKeaClient):
        """F4: POST adding a reservation whose IP is inside an existing pool shows a non-blocking warning."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        # subnet4-get returns subnet with a pool that covers the reservation IP (192.168.1.100)
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 1, "pools": [{"pool": "192.168.1.50-192.168.1.200"}]}]},
            }
        ]
        response = self.client.post(self._add_url(), self._valid_post_data())
        # Non-blocking: still redirects
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_add.assert_called_once()
        storage = list(get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == WARNING for m in storage))

    @patch("netbox_kea.models.KeaClient")
    def test_post_no_warning_when_reservation_ip_outside_pool(self, MockKeaClient):
        """F4: No warning when the reservation IP is not in any existing pool."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        # Pool does NOT cover the reservation IP
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 1, "pools": [{"pool": "192.168.1.10-192.168.1.50"}]}]},
            }
        ]
        response = self.client.post(self._add_url(), self._valid_post_data())
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_add.assert_called_once()
        storage = list(get_messages(response.wsgi_request))
        self.assertFalse(any(m.level == WARNING for m in storage))


# ─────────────────────────────────────────────────────────────────────────────
# TestServerReservation6AddView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservation6AddView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/reservations6/add/"""

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])

    def _valid_post_data(self):
        return {
            "subnet_id": 1,
            "ip_addresses": "2001:db8::100",
            "identifier_type": "duid",
            "identifier": "00:01:02:03:04:05:06:07",
            "hostname": "testhost6.example.com",
        }

    @patch("netbox_kea.models.KeaClient")
    def test_get_renders_form(self, MockKeaClient):
        response = self.client.get(self._add_url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_creates_reservation_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        response = self.client.post(self._add_url(), self._valid_post_data())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)
        mock_client.reservation_add.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_rerenders_form(self, MockKeaClient):
        response = self.client.post(self._add_url(), {})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_error_shows_error_message(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "failed to add host"},
            index=0,
        )
        response = self.client.post(self._add_url(), self._valid_post_data())
        self.assertIn(response.status_code, (200, 302))


# ─────────────────────────────────────────────────────────────────────────────
# TestServerReservation4EditView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservation4EditView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/reservations4/<subnet_id>/<ip>/edit/"""

    _SUBNET_ID = 1
    _IP = "192.168.1.100"

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation4_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    def _valid_post_data(self):
        return {
            "subnet_id": self._SUBNET_ID,
            "ip_address": self._IP,
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "updated-host.example.com",
        }

    @patch("netbox_kea.models.KeaClient")
    def test_get_prepopulates_form_with_reservation_data(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._IP)

    @patch("netbox_kea.models.KeaClient")
    def test_get_404_when_reservation_not_found(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = None  # not found
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 404)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_updates_reservation_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4
        mock_client.reservation_update.return_value = None
        response = self.client.post(self._edit_url(), self._valid_post_data())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)
        mock_client.reservation_update.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_rerenders_form(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4
        # POST with blank identifier — disabled fields (subnet_id, ip_address) use initial values now,
        # so we use a different required field to trigger form invalidity.
        data = self._valid_post_data()
        data["identifier"] = ""
        response = self.client.post(self._edit_url(), data)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_error_shows_error_message(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4
        mock_client.reservation_update.side_effect = KeaException(
            {"result": 1, "text": "failed to update host"},
            index=0,
        )
        response = self.client.post(self._edit_url(), self._valid_post_data())
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_lease_diff_when_hostname_differs(self, MockKeaClient):
        """GET must add lease_diff to context when active lease hostname differs."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4  # hostname: "testhost.example.com"
        mock_client.lease_get_by_ip.return_value = {
            "ip-address": self._IP,
            "hostname": "lease-host.example.com",
        }
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease_diff", response.context)
        self.assertEqual(response.context["lease_diff"]["hostname"], "lease-host.example.com")

    @patch("netbox_kea.models.KeaClient")
    def test_get_no_lease_diff_when_hostname_matches(self, MockKeaClient):
        """GET must not include lease_diff when lease hostname matches reservation."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4  # hostname: "testhost.example.com"
        mock_client.lease_get_by_ip.return_value = {
            "ip-address": self._IP,
            "hostname": "testhost.example.com",
        }
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("lease_diff", response.context)

    @patch("netbox_kea.models.KeaClient")
    def test_get_no_lease_diff_when_lease_fetch_raises(self, MockKeaClient):
        """GET must not crash or add lease_diff when the lease fetch raises KeaException."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4
        mock_client.lease_get_by_ip.side_effect = KeaException({"result": 3, "text": "not found"}, index=0)
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("lease_diff", response.context)


# ─────────────────────────────────────────────────────────────────────────────
# TestServerReservation6EditView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservation6EditView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/reservations6/<subnet_id>/<ip>/edit/"""

    _SUBNET_ID = 1
    _IP = "2001:db8::1"

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation6_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_get_prepopulates_form_with_reservation_data(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION6
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_404_when_reservation_not_found(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = None
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 404)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_updates_reservation_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION6
        mock_client.reservation_update.return_value = None
        response = self.client.post(
            self._edit_url(),
            {
                "subnet_id": self._SUBNET_ID,
                "ip_addresses": "2001:db8::100",
                "identifier_type": "duid",
                "identifier": "00:01:02:03:04:05",
                "hostname": "testhost6.example.com",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)
        mock_client.reservation_update.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# TestServerReservation4DeleteView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservation4DeleteView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/reservations4/<subnet_id>/<ip>/delete/"""

    _SUBNET_ID = 1
    _IP = "192.168.1.100"

    def _delete_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation4_delete",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_confirmation_page(self, MockKeaClient):
        response = self.client.get(self._delete_url())
        self.assertEqual(response.status_code, 200)
        # The confirmation page should mention the IP being deleted
        self.assertContains(response, self._IP)

    @patch("netbox_kea.models.KeaClient")
    def test_post_deletes_reservation_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_del.return_value = None
        response = self.client.post(self._delete_url(), {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)
        mock_client.reservation_del.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_error_shows_message(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_del.side_effect = KeaException(
            {"result": 1, "text": "Host not found."},
            index=0,
        )
        response = self.client.post(self._delete_url(), {"confirm": "true"})
        # Must not crash with 500
        self.assertIn(response.status_code, (200, 302))

    def test_get_nonexistent_server_returns_404(self):
        url = reverse(
            "plugins:netbox_kea:server_reservation4_delete",
            args=[99999, self._SUBNET_ID, self._IP],
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# TestServerReservation6DeleteView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservation6DeleteView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/reservations6/<subnet_id>/<ip>/delete/"""

    _SUBNET_ID = 1
    _IP = "2001:db8::1"

    def _delete_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation6_delete",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_confirmation_page(self, MockKeaClient):
        response = self.client.get(self._delete_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._IP)

    @patch("netbox_kea.models.KeaClient")
    def test_post_deletes_reservation_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_del.return_value = None
        response = self.client.post(self._delete_url(), {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)
        mock_client.reservation_del.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_error_shows_message(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_del.side_effect = KeaException(
            {"result": 1, "text": "Host not found."},
            index=0,
        )
        response = self.client.post(self._delete_url(), {"confirm": "true"})
        self.assertIn(response.status_code, (200, 302))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7b: "Active Lease" badge on reservation pages
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestActiveLeaseStatusOnReservations(_ReservationViewBase):
    """Reservation list must show an 'Active Lease' badge when a live lease exists.

    Lease availability is checked via ``lease4-get-all`` (requires ``lease_cmds``
    hook).  When the command is unavailable the column must stay blank gracefully.
    """

    _LEASE4 = {
        "ip-address": "192.168.1.100",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "subnet-id": 1,
        "cltt": 1700000000,
        "valid-lft": 86400,
    }

    def _prepare_mock_client(self, MockKeaClient, reservation_page=None):
        """Wire the common mock_client context manager and reservation_get_page."""
        mock_client = MockKeaClient.return_value
        _wire_mock_clone(mock_client)
        mock_client.reservation_get_page.return_value = (
            reservation_page if reservation_page is not None else ([dict(_SAMPLE_RESERVATION4)], 0, 0)
        )
        return mock_client

    def _mock_with_lease(self, MockKeaClient):
        """Reservation + matching active lease for 192.168.1.100."""
        mock_client = self._prepare_mock_client(MockKeaClient)
        # lease4-get-all returns a lease matching the reservation IP
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {"leases": [self._LEASE4], "count": 1},
            }
        ]
        return mock_client

    def _mock_with_no_lease(self, MockKeaClient):
        """Reservation present but no active lease."""
        mock_client = self._prepare_mock_client(MockKeaClient)
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [], "count": 0}}]
        return mock_client

    @patch("netbox_kea.models.KeaClient")
    def test_active_lease_badge_shown(self, MockKeaClient):
        """When a matching lease exists the 'Active Lease' badge must be rendered."""
        self._mock_with_lease(MockKeaClient)
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Lease")

    @patch("netbox_kea.models.KeaClient")
    def test_no_active_lease_badge_shown_when_no_lease(self, MockKeaClient):
        """When no lease exists for the reservation IP 'No Lease' must be rendered."""
        self._mock_with_no_lease(MockKeaClient)
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Lease")

    @patch("netbox_kea.models.KeaClient")
    def test_no_crash_when_lease_cmds_unavailable(self, MockKeaClient):
        """When lease_cmds hook is missing the reservation page must still load."""
        mock_client = self._prepare_mock_client(MockKeaClient)
        # lease4-get-all unknown → KeaException result=2
        mock_client.command.side_effect = KeaException(
            {"result": 2, "text": "unknown command 'lease4-get-all'"},
            index=0,
        )
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # No "Active Lease" or "No Lease" badge when hook unavailable
        self.assertNotContains(response, "Active Lease")
        self.assertNotContains(response, "No Lease")


# ─────────────────────────────────────────────────────────────────────────────
# Tests for reservation_get fix (kea.py returns arguments directly not .host)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4EditGetPrefill(_ReservationViewBase):
    """GET reservation edit view must return 200 and pre-populate form fields."""

    _SUBNET_ID = 1
    _IP = "192.168.1.100"

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation4_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_edit_get_returns_200_and_shows_ip(self, MockKeaClient):
        """reservation_get must return the reservation dict so the form is pre-filled."""
        MockKeaClient.return_value.reservation_get.return_value = _SAMPLE_RESERVATION4
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._IP)

    @patch("netbox_kea.models.KeaClient")
    def test_edit_get_shows_hostname_in_form(self, MockKeaClient):
        """Form must be pre-filled with hostname from the existing reservation."""
        MockKeaClient.return_value.reservation_get.return_value = _SAMPLE_RESERVATION4
        response = self.client.get(self._edit_url())
        self.assertContains(response, "testhost.example.com")

    @patch("netbox_kea.models.KeaClient")
    def test_edit_get_404_when_reservation_get_returns_none(self, MockKeaClient):
        """If reservation_get returns None (not found) the view must 404."""
        MockKeaClient.return_value.reservation_get.return_value = None
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# Tests for add view query-param pre-filling
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4AddPrefill(_ReservationViewBase):
    """GET /reservations4/add/?ip_address=...&identifier=... must pre-fill the form."""

    @patch("netbox_kea.models.KeaClient")
    def test_add_get_no_params_renders_empty_form(self, MockKeaClient):
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_add_get_with_ip_and_mac_prefills_form(self, MockKeaClient):
        """Query params must pre-fill the form fields."""
        url = (
            reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
            + "?subnet_id=1&ip_address=10.0.0.5&identifier_type=hw-address"
            "&identifier=aa:bb:cc:dd:ee:ff&hostname=myhost"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # IP and MAC must appear in the rendered form
        self.assertContains(response, "10.0.0.5")
        self.assertContains(response, "aa:bb:cc:dd:ee:ff")
        self.assertContains(response, "myhost")


# ─────────────────────────────────────────────────────────────────────────────
# Tests for + Reserve badge on lease page
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseReserveBadge(_ReservationViewBase):
    """Lease page must show '+ Reserve' link on leases that have no reservation."""

    _LEASE = {
        "ip-address": "192.168.1.200",
        "hw-address": "11:22:33:44:55:66",
        "subnet-id": 1,
        "hostname": "unleased-host",
        "cltt": 1700000000,
        "valid-lft": 3600,
        "state": 0,
    }

    def _htmx_get(self, data):
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    @patch("netbox_kea.models.KeaClient")
    def test_reserve_badge_shown_when_no_reservation(self, MockKeaClient):
        """A lease without a matching reservation must show '+ Reserve' link."""
        mock = MockKeaClient.return_value
        mock.reservation_get.return_value = None
        mock.command.return_value = [{"result": 0, "arguments": {**self._LEASE}}]
        response = self._htmx_get({"by": "ip", "q": "192.168.1.200"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserve")

    @patch("netbox_kea.models.KeaClient")
    def test_reserved_badge_shown_when_reservation_exists(self, MockKeaClient):
        """A lease WITH a matching reservation must show 'Reserved' link, not '+ Reserve'."""
        mock = MockKeaClient.return_value
        _wire_mock_clone(mock)
        reservation = dict(_SAMPLE_RESERVATION4)
        reservation["ip-address"] = "192.168.1.200"
        mock.reservation_get.return_value = reservation
        mock.command.return_value = [{"result": 0, "arguments": {**self._LEASE}}]
        response = self._htmx_get({"by": "ip", "q": "192.168.1.200"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserved")
        self.assertNotContains(response, "+ Reserve")


# ---------------------------------------------------------------------------
# Phase 8: "Active Lease" badge links to lease search view
# ---------------------------------------------------------------------------

_SAMPLE_RESERVATION4_WITH_IP = {
    "subnet-id": 1,
    "hw-address": "bb:cc:dd:ee:ff:01",
    "ip-address": "10.50.0.9",
    "hostname": "lease-link-host",
}


@override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": 30}})
class TestActiveLeaseBadgeLink(TestCase):
    """'Active Lease' badge must be a hyperlink to the per-server lease search."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("lslink_user", password="x"))
        self.server = Server.objects.create(
            name="lease-link-srv",
            server_url="http://kea-test:8000",
            dhcp4=True,
            dhcp6=False,
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_active_lease_badge_is_link_to_lease_search(self, MockKeaClient):
        """When active lease exists the badge must be an <a> linking to lease search by IP."""
        mock_client = MockKeaClient.return_value
        _wire_mock_clone(mock_client)
        mock_client.reservation_get_page.return_value = ([dict(_SAMPLE_RESERVATION4_WITH_IP)], 0, 0)
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [{"ip-address": "10.50.0.9"}]}}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        # Badge must be a link, not a plain span
        self.assertContains(response, "Active Lease</a>")
        # Link must point to the lease search with the reservation IP
        expected_href = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk]) + "?q=10.50.0.9&by=ip"
        self.assertContains(response, expected_href)

    @patch("netbox_kea.models.KeaClient")
    def test_no_lease_badge_is_not_a_link(self, MockKeaClient):
        """'No Lease' badge must remain a plain non-clickable element."""
        mock_client = MockKeaClient.return_value
        _wire_mock_clone(mock_client)
        mock_client.reservation_get_page.return_value = ([dict(_SAMPLE_RESERVATION4_WITH_IP)], 0, 0)
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Lease")
        # Must NOT be a link
        self.assertNotContains(response, "No Lease</a>")


# ---------------------------------------------------------------------------
# Phase 9: "Sync" button shown alongside "Active Lease" badge
# ---------------------------------------------------------------------------

_SAMPLE_RESERVATION4_FOR_SYNC = {
    "subnet-id": 2,
    "hw-address": "cc:dd:ee:ff:00:11",
    "ip-address": "10.60.0.5",
    "hostname": "sync-host",
}

_SAMPLE_RESERVATION6_MULTI_IP = {
    "subnet-id": 3,
    "duid": "aa:bb:cc:dd:ee:01",
    "ip-addresses": ["2001:db8::1", "2001:db8::2", "2001:db8::3"],
    "hostname": "multi-ip6-host",
}


@override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": 30}})
class TestActiveLeaseSyncButton(TestCase):
    """When active lease present and IP not yet in NetBox, show Sync button in lease_status cell."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("sync_btn_user", password="x"))
        self.server = Server.objects.create(
            name="sync-btn-srv",
            server_url="http://kea-test:8000",
            dhcp4=True,
            dhcp6=False,
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_sync_button_shown_when_active_lease_and_no_netbox_ip(self, MockKeaClient, mock_bulk_fetch):
        """When active lease and no NetBox IP: 'Active Lease' badge AND Sync button rendered."""
        mock_client = MockKeaClient.return_value
        _wire_mock_clone(mock_client)
        mock_client.reservation_get_page.return_value = (
            [dict(_SAMPLE_RESERVATION4_FOR_SYNC)],
            0,
            0,
        )
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [{"ip-address": "10.60.0.5"}]}}]
        mock_bulk_fetch.return_value = {}  # no NetBox IPs found
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Lease</a>")
        # Sync button must link to the specific reservation4 sync endpoint
        sync_url = reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk])
        self.assertContains(response, sync_url)

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_sync_button_not_shown_when_active_lease_and_netbox_ip_exists(self, MockKeaClient, mock_bulk_fetch):
        """When active lease AND NetBox IP already synced: no Sync button in lease_status cell."""
        mock_client = MockKeaClient.return_value
        _wire_mock_clone(mock_client)
        mock_client.reservation_get_page.return_value = (
            [dict(_SAMPLE_RESERVATION4_FOR_SYNC)],
            0,
            0,
        )
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [{"ip-address": "10.60.0.5"}]}}]
        nb_ip = MagicMock()
        nb_ip.get_absolute_url.return_value = "/ipam/ip-addresses/42/"
        mock_bulk_fetch.return_value = {"10.60.0.5": nb_ip}
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Lease</a>")
        # Synced link shown in netbox_ip column — but NO individual reservation4 sync button
        sync_url = reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk])
        self.assertNotContains(response, sync_url)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestMultiIPv6ReservationBadgeEnrichment(TestCase):
    """Badge enrichment must check ALL IPv6 addresses (primary + extra_ips)."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("multi_ip6_user", password="x"))
        self.server = Server.objects.create(
            name="multi-ip6-srv",
            server_url="http://kea-test:8000",
            dhcp4=False,
            dhcp6=True,
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])

    def _mock_client(self, MockKeaClient, reservation):
        mock_client = MockKeaClient.return_value
        _wire_mock_clone(mock_client)
        mock_client.reservation_get_page.return_value = ([dict(reservation)], 0, 0)
        # Lease lookup — KeaException so we skip lease enrichment cleanly.
        mock_client.command.side_effect = KeaException(
            {"result": 2, "text": "unknown command 'lease6-get-all'"}, index=0
        )
        return mock_client

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_multi_ip_all_synced_shows_synced_badge(self, MockKeaClient, mock_bulk_fetch):
        """v6 reservation with extra_ips — ALL IPs in NetBox → Synced shown, no sync button."""
        self._mock_client(MockKeaClient, _SAMPLE_RESERVATION6_MULTI_IP)
        nb1, nb2, nb3 = MagicMock(), MagicMock(), MagicMock()
        nb1.get_absolute_url.return_value = "/ipam/ip-addresses/101/"
        nb2.get_absolute_url.return_value = "/ipam/ip-addresses/102/"
        nb3.get_absolute_url.return_value = "/ipam/ip-addresses/103/"
        mock_bulk_fetch.return_value = {"2001:db8::1": nb1, "2001:db8::2": nb2, "2001:db8::3": nb3}

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced</a>")
        sync_url = reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])
        self.assertNotContains(response, sync_url)
        # Verify that ALL reservation IPs were passed to bulk_fetch_netbox_ips
        called_ips = mock_bulk_fetch.call_args[0][0]
        self.assertIn("2001:db8::1", called_ips)
        self.assertIn("2001:db8::2", called_ips)
        self.assertIn("2001:db8::3", called_ips)

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_multi_ip_partial_sync_shows_sync_button(self, MockKeaClient, mock_bulk_fetch):
        """v6 reservation with extra_ips — only primary in NetBox → shows Synced AND sync button."""
        self._mock_client(MockKeaClient, _SAMPLE_RESERVATION6_MULTI_IP)
        nb1 = MagicMock()
        nb1.get_absolute_url.return_value = "/ipam/ip-addresses/101/"
        # Only the first IP is in NetBox; 2001:db8::2 and ::3 are missing.
        mock_bulk_fetch.return_value = {"2001:db8::1": nb1}

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced</a>")
        sync_url = reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])
        self.assertContains(response, sync_url)

    @patch("netbox_kea.sync.bulk_fetch_netbox_ips")
    @patch("netbox_kea.models.KeaClient")
    def test_multi_ip_none_synced_shows_sync_button(self, MockKeaClient, mock_bulk_fetch):
        """v6 reservation with extra_ips — no IPs in NetBox → sync button shown."""
        self._mock_client(MockKeaClient, _SAMPLE_RESERVATION6_MULTI_IP)
        mock_bulk_fetch.return_value = {}

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Synced</a>")
        sync_url = reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])
        self.assertContains(response, sync_url)


# ─────────────────────────────────────────────────────────────────────────────
# Pool Add / Delete views
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4PoolAddView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/subnets4/<subnet_id>/pools/add/"""

    _SUBNET_ID = 1

    def _url(self):
        return reverse(
            "plugins:netbox_kea:server_subnet4_pool_add",
            args=[self.server.pk, self._SUBNET_ID],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_get_renders_form(self, MockKeaClient):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pool")

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_adds_pool_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.pool_add.return_value = None
        response = self.client.post(self._url(), {"pool": "10.0.0.50-10.0.0.99"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)
        mock_client.pool_add.assert_called_once_with(version=4, subnet_id=self._SUBNET_ID, pool="10.0.0.50-10.0.0.99")

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_rerenders_form(self, MockKeaClient):
        response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_error_shows_message(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.pool_add.side_effect = KeaException({"result": 1, "text": "Pool overlap detected."}, index=0)
        response = self.client.post(self._url(), {"pool": "10.0.0.50-10.0.0.99"})
        self.assertIn(response.status_code, (200, 302))

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    # ── F4: pool-reservation overlap warning ─────────────────────────────────

    @patch("netbox_kea.models.KeaClient")
    def test_post_warns_when_new_pool_overlaps_existing_reservation(self, MockKeaClient):
        """F4: POST adding a pool overlapping an existing reservation shows a non-blocking warning."""
        mock_client = MockKeaClient.return_value
        mock_client.pool_add.return_value = None
        # reservation_get_page returns one reservation whose IP is in the new pool range
        mock_client.reservation_get_page.return_value = (
            [{"subnet-id": self._SUBNET_ID, "ip-address": "10.0.0.55"}],
            0,
            0,
        )
        response = self.client.post(self._url(), {"pool": "10.0.0.50-10.0.0.99"})
        # Non-blocking: pool is still added and view redirects
        self.assertEqual(response.status_code, 302)
        mock_client.pool_add.assert_called_once()
        storage = list(get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == WARNING for m in storage))

    @patch("netbox_kea.models.KeaClient")
    def test_post_no_warning_when_no_reservations_in_pool(self, MockKeaClient):
        """F4: No warning when no reservations fall within the new pool range."""
        mock_client = MockKeaClient.return_value
        mock_client.pool_add.return_value = None
        # reservation_get_page returns a reservation OUTSIDE the new pool
        mock_client.reservation_get_page.return_value = (
            [{"subnet-id": self._SUBNET_ID, "ip-address": "10.0.0.10"}],
            0,
            0,
        )
        response = self.client.post(self._url(), {"pool": "10.0.0.50-10.0.0.99"})
        self.assertEqual(response.status_code, 302)
        storage = list(get_messages(response.wsgi_request))
        # Should have success message but no overlap warning
        self.assertFalse(any(m.level == WARNING for m in storage))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4PoolDeleteView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/subnets4/<subnet_id>/pools/<pool>/delete/"""

    _SUBNET_ID = 1
    _POOL = "10.0.0.50-10.0.0.99"

    def _url(self):
        return reverse(
            "plugins:netbox_kea:server_subnet4_pool_delete",
            args=[self.server.pk, self._SUBNET_ID, self._POOL],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_get_renders_confirmation(self, MockKeaClient):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._POOL)

    @patch("netbox_kea.models.KeaClient")
    def test_post_deletes_pool_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.pool_del.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)
        mock_client.pool_del.assert_called_once_with(version=4, subnet_id=self._SUBNET_ID, pool=self._POOL)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_error_redirects_with_message(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.pool_del.side_effect = KeaException({"result": 3, "text": "Pool not found."}, index=0)
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (200, 302))

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet6PoolAddView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/subnets6/<subnet_id>/pools/add/"""

    _SUBNET_ID = 2

    def _url(self):
        return reverse(
            "plugins:netbox_kea:server_subnet6_pool_add",
            args=[self.server.pk, self._SUBNET_ID],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_get_renders_form(self, MockKeaClient):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_adds_pool_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.pool_add.return_value = None
        response = self.client.post(self._url(), {"pool": "2001:db8::10-2001:db8::ff"})
        self.assertEqual(response.status_code, 302)
        mock_client.pool_add.assert_called_once_with(
            version=6, subnet_id=self._SUBNET_ID, pool="2001:db8::10-2001:db8::ff"
        )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet6PoolDeleteView(_ReservationViewBase):
    """GET/POST /plugins/kea/servers/<pk>/subnets6/<subnet_id>/pools/<pool>/delete/"""

    _SUBNET_ID = 2
    _POOL = "2001:db8::10-2001:db8::ff"

    def _url(self):
        return reverse(
            "plugins:netbox_kea:server_subnet6_pool_delete",
            args=[self.server.pk, self._SUBNET_ID, self._POOL],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_get_renders_confirmation(self, MockKeaClient):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_deletes_pool_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.pool_del.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        mock_client.pool_del.assert_called_once_with(version=6, subnet_id=self._SUBNET_ID, pool=self._POOL)


# ---------------------------------------------------------------------------
# Subnet add / delete views
# ---------------------------------------------------------------------------


class TestServerSubnet4AddView(_ReservationViewBase):
    """Tests for ServerSubnet4AddView."""

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    def test_get_renders_form(self):
        resp = self.client.get(self._add_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "id_subnet")

    def test_post_valid_calls_subnet_add_and_redirects(self):
        mock_client = MagicMock()
        mock_client.subnet_add.return_value = None
        mock_client.command.return_value = [{"result": 0, "arguments": {"Dhcp4": {"shared-networks": []}}}]
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            resp = self.client.post(
                self._add_url(),
                data={
                    "subnet": "10.99.0.0/24",
                    "subnet_id": "",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertRedirects(
            resp, reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]), fetch_redirect_response=False
        )
        mock_client.subnet_add.assert_called_once_with(
            version=4,
            subnet_cidr="10.99.0.0/24",
            subnet_id=None,
            pools=[],
            gateway=None,
            dns_servers=[],
            ntp_servers=[],
        )

    def test_post_with_options_passes_them_to_subnet_add(self):
        mock_client = MagicMock()
        mock_client.subnet_add.return_value = None
        mock_client.command.return_value = [{"result": 0, "arguments": {"Dhcp4": {"shared-networks": []}}}]
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            self.client.post(
                self._add_url(),
                data={
                    "subnet": "10.99.0.0/24",
                    "subnet_id": "42",
                    "pools": "10.99.0.100-10.99.0.200",
                    "gateway": "10.99.0.1",
                    "dns_servers": "8.8.8.8",
                    "ntp_servers": "",
                },
            )
        mock_client.subnet_add.assert_called_once_with(
            version=4,
            subnet_cidr="10.99.0.0/24",
            subnet_id=42,
            pools=["10.99.0.100-10.99.0.200"],
            gateway="10.99.0.1",
            dns_servers=["8.8.8.8"],
            ntp_servers=[],
        )

    def test_post_invalid_cidr_rerenders_form(self):
        resp = self.client.post(
            self._add_url(),
            data={
                "subnet": "not-a-cidr",
                "subnet_id": "",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Invalid subnet CIDR")

    def test_post_kea_error_shows_message_and_rerenders(self):
        mock_client = MagicMock()
        mock_client.subnet_add.side_effect = KeaException({"result": 1, "text": "subnet already exists"}, index=0)
        mock_client.command.return_value = [{"result": 0, "arguments": {"Dhcp4": {"shared-networks": []}}}]
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            resp = self.client.post(
                self._add_url(),
                data={
                    "subnet": "10.99.0.0/24",
                    "subnet_id": "",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertRedirects(
            resp, reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]), fetch_redirect_response=False
        )


class TestServerSubnet4DeleteView(_ReservationViewBase):
    """Tests for ServerSubnet4DeleteView."""

    def _delete_url(self, subnet_id=5):
        return reverse("plugins:netbox_kea:server_subnet4_delete", args=[self.server.pk, subnet_id])

    def test_get_renders_confirmation(self):
        mock_client = MagicMock()
        mock_client.command.return_value = [
            {"result": 0, "arguments": {"subnet4": [{"id": 5, "subnet": "10.99.0.0/24", "pools": []}]}}
        ]
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            resp = self.client.get(self._delete_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "10.99.0.0/24")

    def test_post_calls_subnet_del_and_redirects(self):
        mock_client = MagicMock()
        mock_client.subnet_del.return_value = None
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            resp = self.client.post(self._delete_url())
        self.assertRedirects(
            resp, reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]), fetch_redirect_response=False
        )
        mock_client.subnet_del.assert_called_once_with(version=4, subnet_id=5)

    def test_post_kea_error_shows_message(self):
        mock_client = MagicMock()
        mock_client.subnet_del.side_effect = KeaException({"result": 1, "text": "subnet not found"}, index=0)
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            resp = self.client.post(self._delete_url())
        self.assertRedirects(
            resp, reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]), fetch_redirect_response=False
        )


class TestServerSubnet6AddView(_ReservationViewBase):
    """Tests for ServerSubnet6AddView (spot-check version routing)."""

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_subnet6_add", args=[self.server.pk])

    def test_post_valid_uses_version_6(self):
        mock_client = MagicMock()
        mock_client.subnet_add.return_value = None
        mock_client.command.return_value = [{"result": 0, "arguments": {"Dhcp6": {"shared-networks": []}}}]
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            resp = self.client.post(
                self._add_url(),
                data={
                    "subnet": "2001:db8:99::/48",
                    "subnet_id": "",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertRedirects(
            resp, reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk]), fetch_redirect_response=False
        )
        mock_client.subnet_add.assert_called_once_with(
            version=6,
            subnet_cidr="2001:db8:99::/48",
            subnet_id=None,
            pools=[],
            gateway=None,
            dns_servers=[],
            ntp_servers=[],
        )


class TestServerSubnet6DeleteView(_ReservationViewBase):
    """Tests for ServerSubnet6DeleteView (spot-check version routing)."""

    def _delete_url(self, subnet_id=7):
        return reverse("plugins:netbox_kea:server_subnet6_delete", args=[self.server.pk, subnet_id])

    def test_post_calls_subnet_del_v6(self):
        mock_client = MagicMock()
        mock_client.subnet_del.return_value = None
        with patch("netbox_kea.models.KeaClient", return_value=mock_client):
            resp = self.client.post(self._delete_url())
        mock_client.subnet_del.assert_called_once_with(version=6, subnet_id=7)
        self.assertRedirects(
            resp, reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk]), fetch_redirect_response=False
        )


# ─────────────────────────────────────────────────────────────────────────────
# _filter_reservations unit tests (pure function, no DB)
# ─────────────────────────────────────────────────────────────────────────────


class TestFilterReservations(SimpleTestCase):
    """Unit tests for the _filter_reservations() pure-function helper."""

    _R4_A = {
        "subnet-id": 1,
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "ip-address": "192.168.1.100",
        "ip_address": "192.168.1.100",
        "hostname": "alpha.example.com",
    }
    _R4_B = {
        "subnet-id": 2,
        "hw-address": "11:22:33:44:55:66",
        "ip-address": "10.0.0.50",
        "ip_address": "10.0.0.50",
        "hostname": "beta.example.com",
    }
    _R6_A = {
        "subnet-id": 10,
        "duid": "00:01:02:03:04:05",
        "ip-addresses": ["2001:db8::1"],
        "ip_address": "2001:db8::1",
        "hostname": "gamma.example.com",
    }
    _R6_B = {
        "subnet-id": 11,
        "duid": "aa:bb:cc:dd:ee:ff:00:01",
        "ip-addresses": ["2001:db8::2"],
        "ip_address": "2001:db8::2",
        "hostname": "delta.example.com",
    }

    # ── v4 ──────────────────────────────────────────────────────────────────

    def test_no_filter_returns_all_v4(self):
        result = _filter_reservations([self._R4_A, self._R4_B], q="", subnet_id=None, version=4)
        self.assertEqual(result, [self._R4_A, self._R4_B])

    def test_subnet_id_filter_v4(self):
        result = _filter_reservations([self._R4_A, self._R4_B], q="", subnet_id=1, version=4)
        self.assertEqual(result, [self._R4_A])

    def test_subnet_id_no_match_v4(self):
        result = _filter_reservations([self._R4_A, self._R4_B], q="", subnet_id=99, version=4)
        self.assertEqual(result, [])

    def test_q_matches_ip_address_v4(self):
        result = _filter_reservations([self._R4_A, self._R4_B], q="192.168", subnet_id=None, version=4)
        self.assertEqual(result, [self._R4_A])

    def test_q_matches_hostname_v4(self):
        result = _filter_reservations([self._R4_A, self._R4_B], q="beta", subnet_id=None, version=4)
        self.assertEqual(result, [self._R4_B])

    def test_q_matches_hw_address_v4(self):
        result = _filter_reservations([self._R4_A, self._R4_B], q="11:22:33", subnet_id=None, version=4)
        self.assertEqual(result, [self._R4_B])

    def test_q_case_insensitive_v4(self):
        result = _filter_reservations([self._R4_A, self._R4_B], q="ALPHA", subnet_id=None, version=4)
        self.assertEqual(result, [self._R4_A])

    def test_q_and_subnet_id_combined_v4(self):
        result = _filter_reservations([self._R4_A, self._R4_B], q="alpha", subnet_id=1, version=4)
        self.assertEqual(result, [self._R4_A])

    def test_q_and_subnet_id_no_overlap_v4(self):
        # q matches _R4_A but subnet_id=2 matches _R4_B — no overlap
        result = _filter_reservations([self._R4_A, self._R4_B], q="alpha", subnet_id=2, version=4)
        self.assertEqual(result, [])

    def test_empty_list_v4(self):
        result = _filter_reservations([], q="anything", subnet_id=5, version=4)
        self.assertEqual(result, [])

    # ── v6 ──────────────────────────────────────────────────────────────────

    def test_no_filter_returns_all_v6(self):
        result = _filter_reservations([self._R6_A, self._R6_B], q="", subnet_id=None, version=6)
        self.assertEqual(result, [self._R6_A, self._R6_B])

    def test_subnet_id_filter_v6(self):
        result = _filter_reservations([self._R6_A, self._R6_B], q="", subnet_id=10, version=6)
        self.assertEqual(result, [self._R6_A])

    def test_q_matches_ipv6_address_in_list_v6(self):
        result = _filter_reservations([self._R6_A, self._R6_B], q="2001:db8::1", subnet_id=None, version=6)
        self.assertEqual(result, [self._R6_A])

    def test_q_matches_duid_v6(self):
        result = _filter_reservations([self._R6_A, self._R6_B], q="aa:bb:cc", subnet_id=None, version=6)
        self.assertEqual(result, [self._R6_B])

    def test_q_matches_hostname_v6(self):
        result = _filter_reservations([self._R6_A, self._R6_B], q="gamma", subnet_id=None, version=6)
        self.assertEqual(result, [self._R6_A])

    def test_q_case_insensitive_v6(self):
        result = _filter_reservations([self._R6_A, self._R6_B], q="DELTA", subnet_id=None, version=6)
        self.assertEqual(result, [self._R6_B])

    def test_subnet_id_normalised_key_v4(self):
        """Filter also matches reservations that use normalised 'subnet_id' key."""
        r = dict(self._R4_A)
        del r["subnet-id"]
        r["subnet_id"] = 1
        result = _filter_reservations([r], q="", subnet_id=1, version=4)
        self.assertEqual(result, [r])


# ─────────────────────────────────────────────────────────────────────────────
# Reservation search integration tests (view layer)
# ─────────────────────────────────────────────────────────────────────────────

_EXTRA_RESERVATION4 = {
    "subnet-id": 2,
    "hw-address": "de:ad:be:ef:00:01",
    "ip-address": "10.0.0.99",
    "hostname": "other.example.com",
}
_EXTRA_RESERVATION6 = {
    "subnet-id": 20,
    "duid": "ff:ee:dd:cc:bb:aa",
    "ip-addresses": ["2001:db8::ff"],
    "hostname": "other6.example.com",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSearch4View(_ReservationViewBase):
    """Integration tests: search/filter GET params on server_reservations4."""

    def _url(self, **params):
        base = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        if params:
            return f"{base}?{urlencode(params)}"
        return base

    def _mock_two_reservations(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_get_page.return_value = (
            [dict(_SAMPLE_RESERVATION4), dict(_EXTRA_RESERVATION4)],
            0,
            0,
        )
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [], "count": 0}}]
        return mock_client

    @patch("netbox_kea.models.KeaClient")
    def test_no_params_shows_all_reservations(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _SAMPLE_RESERVATION4["ip-address"])
        self.assertContains(response, _EXTRA_RESERVATION4["ip-address"])

    @patch("netbox_kea.models.KeaClient")
    def test_q_filters_by_hostname(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q="testhost"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _SAMPLE_RESERVATION4["ip-address"])
        self.assertNotContains(response, _EXTRA_RESERVATION4["ip-address"])

    @patch("netbox_kea.models.KeaClient")
    def test_q_filters_by_ip(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q="10.0.0.99"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _SAMPLE_RESERVATION4["ip-address"])
        self.assertContains(response, _EXTRA_RESERVATION4["ip-address"])

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_id_filter(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(subnet_id=1))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _SAMPLE_RESERVATION4["ip-address"])
        self.assertNotContains(response, _EXTRA_RESERVATION4["ip-address"])

    @patch("netbox_kea.models.KeaClient")
    def test_search_form_in_context(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q="testhost"))
        self.assertIn("search_form", response.context)

    @patch("netbox_kea.models.KeaClient")
    def test_empty_q_shows_all(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q=""))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _SAMPLE_RESERVATION4["ip-address"])
        self.assertContains(response, _EXTRA_RESERVATION4["ip-address"])

    @patch("netbox_kea.models.KeaClient")
    def test_no_match_shows_no_ips(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q="zzz-no-match-zzz"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _SAMPLE_RESERVATION4["ip-address"])
        self.assertNotContains(response, _EXTRA_RESERVATION4["ip-address"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSearch6View(_ReservationViewBase):
    """Integration tests: search/filter GET params on server_reservations6."""

    def _url(self, **params):
        base = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        if params:
            return f"{base}?{urlencode(params)}"
        return base

    def _mock_two_reservations(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_get_page.return_value = (
            [dict(_SAMPLE_RESERVATION6), dict(_EXTRA_RESERVATION6)],
            0,
            0,
        )
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": [], "count": 0}}]
        return mock_client

    @patch("netbox_kea.models.KeaClient")
    def test_no_params_shows_all_reservations(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _SAMPLE_RESERVATION6["ip-addresses"][0])
        self.assertContains(response, _EXTRA_RESERVATION6["ip-addresses"][0])

    @patch("netbox_kea.models.KeaClient")
    def test_q_filters_by_hostname(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q="testhost6"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _SAMPLE_RESERVATION6["ip-addresses"][0])
        self.assertNotContains(response, _EXTRA_RESERVATION6["ip-addresses"][0])

    @patch("netbox_kea.models.KeaClient")
    def test_q_filters_by_duid(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q="ff:ee:dd"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _SAMPLE_RESERVATION6["ip-addresses"][0])
        self.assertContains(response, _EXTRA_RESERVATION6["ip-addresses"][0])

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_id_filter(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(subnet_id=1))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _SAMPLE_RESERVATION6["ip-addresses"][0])
        self.assertNotContains(response, _EXTRA_RESERVATION6["ip-addresses"][0])

    @patch("netbox_kea.models.KeaClient")
    def test_search_form_in_context(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q="testhost6"))
        self.assertIn("search_form", response.context)

    @patch("netbox_kea.models.KeaClient")
    def test_no_match_shows_no_ips(self, MockKeaClient):
        self._mock_two_reservations(MockKeaClient)
        response = self.client.get(self._url(q="zzz-no-match-zzz"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _SAMPLE_RESERVATION6["ip-addresses"][0])
        self.assertNotContains(response, _EXTRA_RESERVATION6["ip-addresses"][0])


# ---------------------------------------------------------------------------
# TestBulkReservationImport
# ---------------------------------------------------------------------------

_BULK_IMPORT_V4_CSV = (
    "ip-address,hw-address,hostname,subnet-id\n"
    "10.99.0.1,aa:bb:cc:00:00:01,host1.example.com,1\n"
    "10.99.0.2,aa:bb:cc:00:00:02,host2.example.com,1\n"
)

_BULK_IMPORT_V6_CSV = "ip-addresses,duid,hostname,subnet-id\n2001:db8::1,00:01:02:03:04:05,v6host1.example.com,10\n"


def _import_url(server_pk: int, version: int) -> str:
    return reverse(f"plugins:netbox_kea:server_reservation{version}_bulk_import", args=[server_pk])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationImport(_ReservationViewBase):
    """GET + POST /plugins/kea/servers/<pk>/reservations4/import/ and v6 variant."""

    def test_url_registered_v4(self):
        """URL server_reservation4_bulk_import is registered."""
        url = _import_url(self.server.pk, 4)
        self.assertIn("import", url)

    def test_url_registered_v6(self):
        """URL server_reservation6_bulk_import is registered."""
        url = _import_url(self.server.pk, 6)
        self.assertIn("import", url)

    @patch("netbox_kea.models.KeaClient")
    def test_get_renders_form(self, MockKeaClient):
        """GET renders the import form (200 OK)."""
        MockKeaClient.return_value.get_available_commands.return_value = _RESERVATION_COMMANDS
        response = self.client.get(_import_url(self.server.pk, 4))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "import", msg_prefix="", html=False)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_v4_csv_creates_reservations(self, MockKeaClient):
        """POST valid v4 CSV creates two reservations and shows created count."""
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_add.return_value = None
        csv_file = io.BytesIO(_BULK_IMPORT_V4_CSV.encode())
        csv_file.name = "import.csv"
        response = self.client.post(
            _import_url(self.server.pk, 4),
            {"csv_file": csv_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_client.reservation_add.call_count, 2)
        self.assertContains(response, "Created")  # result summary shown

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_v6_csv_creates_reservation(self, MockKeaClient):
        """POST valid v6 CSV creates one reservation."""
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_add.return_value = None
        csv_file = io.BytesIO(_BULK_IMPORT_V6_CSV.encode())
        csv_file.name = "import.csv"
        response = self.client.post(
            _import_url(self.server.pk, 6),
            {"csv_file": csv_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_client.reservation_add.call_count, 1)

    @patch("netbox_kea.models.KeaClient")
    def test_post_skips_duplicate_reservations(self, MockKeaClient):
        """result=1 with 'already exists' text is counted as skipped, not error."""
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        dup_exc = KeaException({"result": 1, "text": "Host already exists."}, index=0)
        mock_client.reservation_add.side_effect = dup_exc
        csv_file = io.BytesIO(_BULK_IMPORT_V4_CSV.encode())
        csv_file.name = "import.csv"
        response = self.client.post(
            _import_url(self.server.pk, 4),
            {"csv_file": csv_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        # No hard error — page still 200 with skipped count shown
        self.assertContains(response, "Skipped (already exist)")  # skipped summary shown

    @patch("netbox_kea.models.KeaClient")
    def test_post_shows_errors_on_kea_failure(self, MockKeaClient):
        """KeaException (non-duplicate) is counted as error and shown on page."""
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS
        mock_client.reservation_add.side_effect = KeaException({"result": 1, "text": "subnet not found"})
        csv_file = io.BytesIO(_BULK_IMPORT_V4_CSV.encode())
        csv_file.name = "import.csv"
        response = self.client.post(
            _import_url(self.server.pk, 4),
            {"csv_file": csv_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error", msg_prefix="", html=False)

    @patch("netbox_kea.models.KeaClient")
    def test_post_requires_file(self, MockKeaClient):
        """POST without csv_file shows form with error."""
        MockKeaClient.return_value.get_available_commands.return_value = _RESERVATION_COMMANDS
        response = self.client.post(_import_url(self.server.pk, 4), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "required", msg_prefix="", html=False)

    def test_get_requires_login(self):
        """Unauthenticated users are redirected."""
        self.client.logout()
        response = self.client.get(_import_url(self.server.pk, 4))
        self.assertIn(response.status_code, (302, 403))

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_csv_shows_error(self, MockKeaClient):
        """Uploading a CSV with missing required column shows error without crashing."""
        MockKeaClient.return_value.get_available_commands.return_value = _RESERVATION_COMMANDS
        bad_csv = b"some-random-column,another\nvalue1,value2\n"
        csv_file = io.BytesIO(bad_csv)
        csv_file.name = "bad.csv"
        response = self.client.post(
            _import_url(self.server.pk, 4),
            {"csv_file": csv_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error", msg_prefix="", html=False)

    @patch("netbox_kea.models.KeaClient")
    def test_summary_shows_created_skipped_errors_counts(self, MockKeaClient):
        """Result page shows three distinct count values: created, skipped, errors."""
        mock_client = MockKeaClient.return_value
        mock_client.get_available_commands.return_value = _RESERVATION_COMMANDS

        # row 1 → success, row 2 → already exists (skip)
        dup_exc = KeaException({"result": 1, "text": "Host already exists."}, index=0)
        mock_client.reservation_add.side_effect = [None, dup_exc]
        csv_file = io.BytesIO(_BULK_IMPORT_V4_CSV.encode())
        csv_file.name = "import.csv"
        response = self.client.post(
            _import_url(self.server.pk, 4),
            {"csv_file": csv_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("created", response.content.decode().lower())
        self.assertIn("skipped", response.content.decode().lower())


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3b: Reservation auto-sync to NetBox IPAM
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSyncToNetBox(_ReservationViewBase):
    """Test the 'Sync to NetBox IPAM' checkbox on reservation add/edit forms."""

    def _add4_url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    def _edit4_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation4_edit",
            args=[self.server.pk, 1, "192.168.1.100"],
        )

    def _valid_post_data(self, sync=False):
        data = {
            "subnet_id": 1,
            "ip_address": "192.168.1.100",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
        }
        if sync:
            data["sync_to_netbox"] = "on"
        return data

    @patch("netbox_kea.models.KeaClient")
    def test_add_form_has_sync_to_netbox_field(self, MockKeaClient):
        """GET reservation add renders a sync_to_netbox checkbox."""
        MockKeaClient.return_value.get_available_commands.return_value = _RESERVATION_COMMANDS
        response = self.client.get(self._add4_url())
        self.assertEqual(response.status_code, 200)
        self.assertIn("sync_to_netbox", response.content.decode())

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_add_with_sync_checked_calls_sync(self, MockKeaClient, mock_sync):
        """POSTing with sync_to_netbox=on calls sync_reservation_to_netbox()."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        mock_sync.return_value = (MagicMock(), True)
        response = self.client.post(self._add4_url(), self._valid_post_data(sync=True))
        self.assertEqual(response.status_code, 302)
        mock_sync.assert_called_once()
        called_reservation = mock_sync.call_args[0][0]
        self.assertEqual(called_reservation["ip-address"], "192.168.1.100")

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_add_without_sync_does_not_call_sync(self, MockKeaClient, mock_sync):
        """POSTing without sync_to_netbox does NOT call sync_reservation_to_netbox()."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        response = self.client.post(self._add4_url(), self._valid_post_data(sync=False))
        self.assertEqual(response.status_code, 302)
        mock_sync.assert_not_called()

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_add_sync_failure_still_redirects(self, MockKeaClient, mock_sync):
        """Sync failure is a warning; Kea reservation creation still succeeds."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        mock_sync.side_effect = ValueError("Reservation has no ip-address or ip-addresses field.")
        response = self.client.post(self._add4_url(), self._valid_post_data(sync=True))
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_add.assert_called_once()

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_edit_with_sync_checked_calls_sync(self, MockKeaClient, mock_sync):
        """POSTing reservation edit with sync_to_netbox=on calls sync_reservation_to_netbox()."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_update.return_value = None
        mock_sync.return_value = (MagicMock(), False)
        response = self.client.post(self._edit4_url(), self._valid_post_data(sync=True))
        self.assertEqual(response.status_code, 302)
        mock_sync.assert_called_once()
        called_reservation = mock_sync.call_args[0][0]
        self.assertEqual(called_reservation["ip-address"], "192.168.1.100")


# ─────────────────────────────────────────────────────────────────────────────
# PartialPersistError regression tests — Issue #18
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPartialPersistErrorOnReservationAdd(_ReservationViewBase):
    """PartialPersistError on reservation4 add shows warning and redirects (not 500)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_shows_warning_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.side_effect = PartialPersistError("dhcp4", Exception("config-write failed"))
        response = self.client.post(
            self._url(),
            {
                "subnet_id": 1,
                "ip_address": "192.168.1.100",
                "identifier_type": "hw-address",
                "identifier": "aa:bb:cc:dd:ee:ff",
                "hostname": "testhost.example.com",
            },
        )
        # PartialPersistError should redirect (302) with a warning message, not crash (500)
        self.assertEqual(response.status_code, 302)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPartialPersistErrorOnPoolAdd(_ReservationViewBase):
    """PartialPersistError on pool_add shows warning and redirects (not 500)."""

    _SUBNET_ID = 1

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, self._SUBNET_ID])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_shows_warning_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.pool_add.side_effect = PartialPersistError("dhcp4", Exception("config-write failed"))
        response = self.client.post(self._url(), {"pool": "10.0.0.50-10.0.0.99"})
        self.assertEqual(response.status_code, 302)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPartialPersistErrorOnSubnetAdd(_ReservationViewBase):
    """PartialPersistError on subnet4 add shows warning and redirects (not 500)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_shows_warning_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.subnet_add.side_effect = PartialPersistError("dhcp4", Exception("config-write failed"))
        mock_client.command.return_value = [{"result": 0, "arguments": {"Dhcp4": {"shared-networks": []}}}]
        response = self.client.post(
            self._url(),
            {
                "subnet": "10.10.0.0/24",
            },
        )
        mock_client.subnet_add.assert_called_once()
        self.assertEqual(response.status_code, 302)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPartialPersistErrorOnSubnetDelete(_ReservationViewBase):
    """PartialPersistError on subnet4 delete shows warning and redirects (not 500)."""

    _SUBNET_ID = 1

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_delete", args=[self.server.pk, self._SUBNET_ID])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_shows_warning_and_redirects(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.subnet_del.side_effect = PartialPersistError("dhcp4", Exception("config-write failed"))
        response = self.client.post(self._url(), {"confirm": True})
        self.assertEqual(response.status_code, 302)


# ─────────────────────────────────────────────────────────────────────────────
# F3: Reservation edit lease diff — DHCPv6 version
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerReservation6EditLeaseDiff(_ReservationViewBase):
    """F3: GET edit for DHCPv6 shows lease_diff when active lease hostname differs."""

    _SUBNET_ID = 1
    _IP = "2001:db8::1"

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation6_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_lease_diff_when_hostname_differs(self, MockKeaClient):
        """GET must add lease_diff to context when DHCPv6 active lease hostname differs."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = {**_SAMPLE_RESERVATION6, "ip-addresses": [self._IP]}
        mock_client.lease_get_by_ip.return_value = {
            "ip-address": self._IP,
            "hostname": "lease-host6.example.com",
        }
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease_diff", response.context)
        self.assertEqual(response.context["lease_diff"]["hostname"], "lease-host6.example.com")

    @patch("netbox_kea.models.KeaClient")
    def test_get_no_lease_diff_when_lease_fetch_raises(self, MockKeaClient):
        """GET must not crash when DHCPv6 lease fetch raises KeaException."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = {**_SAMPLE_RESERVATION6, "ip-addresses": [self._IP]}
        mock_client.lease_get_by_ip.side_effect = KeaException({"result": 3, "text": "not found"}, index=0)
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("lease_diff", response.context)

    @patch("netbox_kea.models.KeaClient")
    def test_get_no_lease_diff_when_hostname_matches(self, MockKeaClient):
        """GET must not add lease_diff when lease hostname matches the reservation hostname."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = {**_SAMPLE_RESERVATION6, "ip-addresses": [self._IP]}
        mock_client.lease_get_by_ip.return_value = {
            "ip-address": self._IP,
            "hostname": "testhost6.example.com",  # matches reservation
        }
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("lease_diff", response.context)


# ─────────────────────────────────────────────────────────────────────────────
# F7: Reservation journal entries
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationJournalEntries(_ReservationViewBase):
    """F7: Successful reservation add/edit/delete must create a JournalEntry on the Server."""

    _SUBNET_ID = 1
    _IP = "192.168.1.100"

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation4_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    def _delete_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation4_delete",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    def _journal_count(self):
        from django.contrib.contenttypes.models import ContentType
        from extras.models import JournalEntry

        ct = ContentType.objects.get_for_model(self.server)
        return JournalEntry.objects.filter(
            assigned_object_type=ct,
            assigned_object_id=self.server.pk,
        ).count()

    @patch("netbox_kea.models.KeaClient")
    def test_add_creates_journal_entry(self, MockKeaClient):
        """Successful reservation-add must create a JournalEntry on the Server."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        before = self._journal_count()
        self.client.post(
            self._add_url(),
            {
                "subnet_id": self._SUBNET_ID,
                "ip_address": self._IP,
                "identifier_type": "hw-address",
                "identifier": "aa:bb:cc:dd:ee:ff",
                "hostname": "testhost.example.com",
            },
        )
        self.assertEqual(self._journal_count(), before + 1)

    @patch("netbox_kea.models.KeaClient")
    def test_edit_creates_journal_entry(self, MockKeaClient):
        """Successful reservation-update must create a JournalEntry on the Server."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4
        mock_client.reservation_update.return_value = None
        before = self._journal_count()
        self.client.post(
            self._edit_url(),
            {
                "subnet_id": self._SUBNET_ID,
                "ip_address": self._IP,
                "identifier_type": "hw-address",
                "identifier": "aa:bb:cc:dd:ee:ff",
                "hostname": "updated-host.example.com",
            },
        )
        self.assertEqual(self._journal_count(), before + 1)

    @patch("netbox_kea.models.KeaClient")
    def test_delete_creates_journal_entry(self, MockKeaClient):
        """Successful reservation-del must create a JournalEntry on the Server."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_del.return_value = None
        before = self._journal_count()
        self.client.post(self._delete_url(), {"confirm": "true"})
        self.assertEqual(self._journal_count(), before + 1)

    @patch("netbox_kea.models.KeaClient")
    def test_add_kea_error_does_not_create_journal_entry(self, MockKeaClient):
        """Failed reservation-add must NOT create a JournalEntry."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        before = self._journal_count()
        response = self.client.post(
            self._add_url(),
            {
                "subnet_id": self._SUBNET_ID,
                "ip_address": self._IP,
                "identifier_type": "hw-address",
                "identifier": "aa:bb:cc:dd:ee:ff",
                "hostname": "testhost.example.com",
            },
        )
        self.assertIn(response.status_code, (200, 302))
        mock_client.reservation_add.assert_called_once()
        self.assertEqual(self._journal_count(), before)


# ─────────────────────────────────────────────────────────────────────────────
# Gap R1: Reservation option-data support
# ─────────────────────────────────────────────────────────────────────────────


def _options_formset_data(options=None, prefix="options"):
    """Build POST data for ReservationOptionsFormSet with given option rows."""
    opts = options or []
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(opts)),
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, opt in enumerate(opts):
        data[f"{prefix}-{i}-name"] = opt.get("name", "")
        data[f"{prefix}-{i}-data"] = opt.get("data", "")
        data[f"{prefix}-{i}-always_send"] = "on" if opt.get("always_send") else ""
        data[f"{prefix}-{i}-DELETE"] = "on" if opt.get("DELETE") else ""
    return data


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4OptionData(_ReservationViewBase):
    """Reservation add/edit views must support option-data formset (Gap R1)."""

    _SUBNET_ID = 1
    _IP = "192.168.1.100"

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation4_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    def _base_post(self, extra=None):
        data = {
            "subnet_id": self._SUBNET_ID,
            "ip_address": self._IP,
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
        }
        if extra:
            data.update(extra)
        return data

    @patch("netbox_kea.models.KeaClient")
    def test_post_add_with_options_includes_option_data(self, MockKeaClient):
        """POST add with options formset must include option-data in the reservation_add call."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None

        post_data = self._base_post()
        post_data.update(
            _options_formset_data(
                [
                    {"name": "boot-file-name", "data": "http://10.0.0.1/ztp.py"},
                ]
            )
        )
        response = self.client.post(self._add_url(), post_data)
        self.assertEqual(response.status_code, 302)

        mock_client.reservation_add.assert_called_once()
        call_args = mock_client.reservation_add.call_args
        args, kwargs = call_args or ((), {})
        reservation = kwargs.get("reservation") or (args[1] if len(args) > 1 else (args[0] if len(args) > 0 else {}))
        self.assertIn("option-data", reservation)
        self.assertEqual(len(reservation["option-data"]), 1)
        self.assertEqual(reservation["option-data"][0]["name"], "boot-file-name")
        self.assertEqual(reservation["option-data"][0]["data"], "http://10.0.0.1/ztp.py")

    @patch("netbox_kea.models.KeaClient")
    def test_post_add_without_options_omits_option_data(self, MockKeaClient):
        """POST add with empty formset must NOT include option-data in the reservation dict."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None

        post_data = self._base_post()
        post_data.update(_options_formset_data([]))
        response = self.client.post(self._add_url(), post_data)
        self.assertEqual(response.status_code, 302)

        mock_client.reservation_add.assert_called_once()
        call_args = mock_client.reservation_add.call_args
        args, kwargs = call_args or ((), {})
        reservation = kwargs.get("reservation") or (args[1] if len(args) > 1 else (args[0] if len(args) > 0 else {}))
        self.assertNotIn("option-data", reservation)

    @patch("netbox_kea.models.KeaClient")
    def test_get_edit_prepopulates_options_formset(self, MockKeaClient):
        """GET edit must pre-populate the options formset from existing reservation option-data."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = {
            **_SAMPLE_RESERVATION4,
            "option-data": [{"name": "boot-file-name", "data": "http://10.0.0.1/ztp.py"}],
        }
        mock_client.lease_get_by_ip.return_value = None
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "boot-file-name")
        self.assertContains(response, "http://10.0.0.1/ztp.py")
        formset = response.context["options_formset"]
        self.assertEqual(formset.initial[0]["name"], "boot-file-name")
        self.assertEqual(formset.initial[0]["data"], "http://10.0.0.1/ztp.py")

    @patch("netbox_kea.models.KeaClient")
    def test_post_edit_with_options_includes_option_data(self, MockKeaClient):
        """POST edit with options formset must include option-data in the reservation_update call."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = _SAMPLE_RESERVATION4
        mock_client.reservation_update.return_value = None

        post_data = self._base_post()
        post_data.update(
            _options_formset_data(
                [
                    {"name": "tftp-server-name", "data": "10.0.0.1"},
                ]
            )
        )
        response = self.client.post(self._edit_url(), post_data)
        self.assertEqual(response.status_code, 302)

        mock_client.reservation_update.assert_called_once()
        call_args = mock_client.reservation_update.call_args
        args, kwargs = call_args or ((), {})
        reservation = kwargs.get("reservation") or (args[1] if len(args) > 1 else (args[0] if len(args) > 0 else {}))
        self.assertIn("option-data", reservation)
        self.assertEqual(reservation["option-data"][0]["name"], "tftp-server-name")
        self.assertEqual(reservation["option-data"][0]["data"], "10.0.0.1")

    @patch("netbox_kea.models.KeaClient")
    def test_get_add_shows_ztp_help_text(self, MockKeaClient):
        """GET add form must contain ZTP reference text in the response."""
        response = self.client.get(self._add_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "boot-file-name")


# ─────────────────────────────────────────────────────────────────────────────
# F4 coverage: sync_reservation_to_netbox called with cleanup=False
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncReservationCleanupFalse(_ReservationViewBase):
    """After F4 fix, sync_reservation_to_netbox must be called with cleanup=False."""

    def _add4_url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_add_post_sync_calls_with_cleanup_false(self, MockKeaClient, mock_sync):
        """POSTing reservation add with sync_to_netbox=on passes cleanup=False."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        mock_sync.return_value = (MagicMock(), True)
        data = {
            "subnet_id": 1,
            "ip_address": "192.168.1.100",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
            "sync_to_netbox": "on",
        }
        response = self.client.post(self._add4_url(), data)
        self.assertEqual(response.status_code, 302)
        mock_sync.assert_called_once()
        _, kwargs = mock_sync.call_args
        self.assertFalse(kwargs["cleanup"])


# ─────────────────────────────────────────────────────────────────────────────
# F5 coverage: KeaException with result != 2 during reservation fetch
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaExceptionResult1OnFetch(_ReservationViewBase):
    """KeaException with result=1 returns 200 with error message, not a crash."""

    @patch("netbox_kea.models.KeaClient")
    def test_v4_fetch_kea_error_result1_returns_200(self, MockKeaClient):
        """Result=1 shows error message and keeps hook_available=True."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get_page.side_effect = KeaException(
            {"result": 1, "text": "error"},
            index=0,
        )
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["hook_available"])
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Failed to load" in m for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_v6_fetch_kea_error_result1_returns_200(self, MockKeaClient):
        """Result=1 shows error message and keeps hook_available=True for DHCPv6."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get_page.side_effect = KeaException(
            {"result": 1, "text": "error"},
            index=0,
        )
        url = reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["hook_available"])
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Failed to load" in m for m in msgs))


# ─────────────────────────────────────────────────────────────────────────────
# F9 coverage: V6 edit POST preserves multi-address from form
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestV6EditPostPreservesFormIPs(_ReservationViewBase):
    """After F9 fix, v6 edit POST uses the form ip_addresses to preserve multi-address reservations."""

    _SUBNET_ID = 1
    _IP = "2001:db8::100"

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation6_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_post_preserves_existing_ip_addresses(self, MockKeaClient):
        """POST ignores posted ip_addresses (disabled field) and preserves existing IPs from reservation_get."""
        mock_client = MockKeaClient.return_value
        existing = {**_SAMPLE_RESERVATION6, "ip-addresses": ["2001:db8::100", "2001:db8::200"]}
        mock_client.reservation_get.return_value = existing
        mock_client.reservation_update.return_value = None
        response = self.client.post(
            self._edit_url(),
            {
                "subnet_id": self._SUBNET_ID,
                "ip_addresses": "2001:db8::dead,2001:db8::beef",  # different — should be ignored
                "identifier_type": "duid",
                "identifier": "00:01:02:03:04:05",
                "hostname": "testhost6.example.com",
            },
        )
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_update.assert_called_once()
        call_args = mock_client.reservation_update.call_args
        args, kwargs = call_args or ((), {})
        reservation = kwargs.get("reservation") or (args[1] if len(args) > 1 else {})
        self.assertEqual(reservation["ip-addresses"], existing["ip-addresses"])

    @patch("netbox_kea.models.KeaClient")
    def test_post_aborts_when_reservation_get_fails(self, MockKeaClient):
        """POST aborts with error redirect when reservation_get raises, preventing silent IP truncation."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        response = self.client.post(
            self._edit_url(),
            {
                "subnet_id": self._SUBNET_ID,
                "ip_addresses": "2001:db8::100",
                "identifier_type": "duid",
                "identifier": "00:01:02:03:04:05",
                "hostname": "testhost6.example.com",
            },
        )
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_update.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_post_aborts_when_reservation_get_returns_none(self, MockKeaClient):
        """POST aborts when reservation_get returns None (reservation disappeared)."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get.return_value = None
        response = self.client.post(
            self._edit_url(),
            {
                "subnet_id": self._SUBNET_ID,
                "ip_addresses": "2001:db8::100",
                "identifier_type": "duid",
                "identifier": "00:01:02:03:04:05",
                "hostname": "testhost6.example.com",
            },
        )
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_update.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Coverage: _enrich_reservations_with_lease_status error paths (~lines 180-212)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichReservationsLeaseStatusErrors(_ReservationViewBase):
    """Error paths in _enrich_reservations_with_lease_status for malformed responses."""

    def _prepare_mock_client(self, MockKeaClient, reservations=None):
        mock_client = MockKeaClient.return_value
        _wire_mock_clone(mock_client)
        mock_client.reservation_get_page.return_value = (
            reservations if reservations is not None else ([dict(_SAMPLE_RESERVATION4)], 0, 0)
        )
        return mock_client

    @patch("netbox_kea.models.KeaClient")
    def test_malformed_args_not_dict_sets_indeterminate(self, MockKeaClient):
        """When lease-get-all returns args that is not a dict, has_active_lease stays None."""
        mock_client = self._prepare_mock_client(MockKeaClient)
        # Response where arguments is a string instead of dict
        mock_client.command.return_value = [{"result": 0, "arguments": "not-a-dict"}]
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        for row in table.data:
            # Indeterminate: has_active_lease should not be set to True or False
            self.assertIsNone(row.get("has_active_lease"))

    @patch("netbox_kea.models.KeaClient")
    def test_malformed_leases_not_list_sets_indeterminate(self, MockKeaClient):
        """When lease-get-all returns leases as a string instead of list, has_active_lease stays None."""
        mock_client = self._prepare_mock_client(MockKeaClient)
        mock_client.command.return_value = [{"result": 0, "arguments": {"leases": "not-a-list"}}]
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        for row in table.data:
            self.assertIsNone(row.get("has_active_lease"))

    @patch("netbox_kea.models.KeaClient")
    def test_non_dict_lease_entries_are_skipped(self, MockKeaClient):
        """When lease entries contain non-dict items, they are skipped without crashing."""
        mock_client = self._prepare_mock_client(MockKeaClient)
        # Mix of valid dict and non-dict entries
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "leases": [
                        "not-a-dict",
                        42,
                        {"ip-address": "192.168.1.100"},
                    ]
                },
            }
        ]
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        # The valid lease entry should match, so has_active_lease = True
        for row in table.data:
            self.assertTrue(row.get("has_active_lease"))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_result_not_2_sets_indeterminate(self, MockKeaClient):
        """KeaException with result!=2 (not hook-unavailable) leaves has_active_lease=None."""
        mock_client = self._prepare_mock_client(MockKeaClient)
        mock_client.command.side_effect = KeaException(
            {"result": 1, "text": "internal error"},
            index=0,
        )
        url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        for row in table.data:
            self.assertIsNone(row.get("has_active_lease"))


# ─────────────────────────────────────────────────────────────────────────────
# Coverage: _run_reservation_success_side_effects sync exception (~lines 132-137)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSyncExceptionOnSuccess(_ReservationViewBase):
    """When sync_reservation_to_netbox raises, reservation is still saved and warning shown."""

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    def _valid_post_data(self):
        return {
            "subnet_id": 1,
            "ip_address": "192.168.1.50",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "sync-fail-host",
            "sync_to_netbox": "on",
        }

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_sync_failure_shows_warning_but_reservation_saved(self, MockKeaClient, mock_sync):
        """If sync_reservation_to_netbox raises, reservation add still redirects with warning."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        mock_sync.side_effect = ValueError("DB sync failed")
        response = self.client.post(self._add_url(), self._valid_post_data())
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_add.assert_called_once()
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("sync failed" in m.lower() for m in msgs))
        self.assertFalse(any("DB sync failed" in m for m in msgs))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_sync_success_shows_info_message(self, MockKeaClient, mock_sync):
        """If sync succeeds, info message shown with created/updated status."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        mock_sync.return_value = (MagicMock(), True)
        response = self.client.post(self._add_url(), self._valid_post_data())
        self.assertEqual(response.status_code, 302)
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("created" in m.lower() for m in msgs))


# ─────────────────────────────────────────────────────────────────────────────
# Coverage: Reservation add with invalid option formset (~lines 466-475)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4AddInvalidOptionsFormset(_ReservationViewBase):
    """POST with valid main form but invalid option formset re-renders form."""

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_invalid_options_formset_rerenders_form(self, MockKeaClient):
        """When option formset is invalid, form is re-rendered (not submitted to Kea)."""
        mock_client = MockKeaClient.return_value
        data = {
            "subnet_id": 1,
            "ip_address": "192.168.1.100",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
            # Include management form with correct prefix but invalid option data
            "options-TOTAL_FORMS": "1",
            "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            # Option row 0: name is required but data is empty
            "options-0-name": "routers",
            "options-0-data": "",  # required field — empty triggers validation error
        }
        response = self.client.post(self._add_url(), data)
        self.assertEqual(response.status_code, 200)
        mock_client.reservation_add.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_partial_options_submission_without_management_form(self, MockKeaClient):
        """Partial options submission (options-* keys but no TOTAL_FORMS) re-renders form."""
        mock_client = MockKeaClient.return_value
        data = {
            "subnet_id": 1,
            "ip_address": "192.168.1.100",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
            # options keys present but no management form fields
            "options-0-name": "routers",
            "options-0-data": "10.0.0.1",
        }
        response = self.client.post(self._add_url(), data)
        self.assertEqual(response.status_code, 200)
        mock_client.reservation_add.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Coverage: _filter_reservations with non-string fields (~lines 259-265)
# ─────────────────────────────────────────────────────────────────────────────


class TestFilterReservationsNonStringFields(SimpleTestCase):
    """_filter_reservations must not crash when fields contain non-string values."""

    def test_v4_hw_address_as_int_does_not_crash(self):
        """If hw-address is an int (malformed), filter should skip it gracefully."""
        reservations = [
            {"ip-address": "10.0.0.1", "hostname": "host1", "hw-address": 12345},
        ]
        result = _filter_reservations(reservations, q="host1", subnet_id=None, version=4)
        self.assertEqual(len(result), 1)

    def test_v4_hw_address_as_list_does_not_crash(self):
        """If hw-address is a list (malformed), filter should skip it gracefully."""
        reservations = [
            {"ip-address": "10.0.0.1", "hostname": "host1", "hw-address": ["aa:bb:cc"]},
        ]
        result = _filter_reservations(reservations, q="host1", subnet_id=None, version=4)
        self.assertEqual(len(result), 1)

    def test_v4_int_hw_address_matches_by_hostname(self):
        """When hw-address is int but hostname matches, the row is still found."""
        reservations = [
            {"ip-address": "10.0.0.1", "hostname": "findme", "hw-address": 12345},
        ]
        # hostname match is evaluated before hw-address in the or-chain,
        # so the row is returned without hitting .lower() on the int.
        result = _filter_reservations(reservations, q="findme", subnet_id=None, version=4)
        self.assertEqual(len(result), 1)

    def test_v6_duid_as_int_does_not_crash(self):
        """If duid is an int (malformed), filter should not crash."""
        reservations = [
            {"ip-addresses": ["2001:db8::1"], "hostname": "host6", "duid": 999},
        ]
        result = _filter_reservations(reservations, q="host6", subnet_id=None, version=6)
        self.assertEqual(len(result), 1)

    def test_v4_int_hw_address_no_hostname_match(self):
        """When hw-address is int and query does NOT match hostname, row is excluded gracefully."""
        reservations = [
            {"ip-address": "10.0.0.1", "hostname": "host1", "hw-address": 12345},
        ]
        result = _filter_reservations(reservations, q="nomatch", subnet_id=None, version=4)
        self.assertEqual(len(result), 0)

    def test_v4_list_hw_address_no_hostname_match(self):
        """When hw-address is a list and query does NOT match hostname, row is excluded gracefully."""
        reservations = [
            {"ip-address": "10.0.0.1", "hostname": "host1", "hw-address": ["aa:bb:cc"]},
        ]
        result = _filter_reservations(reservations, q="nomatch", subnet_id=None, version=4)
        self.assertEqual(len(result), 0)

    def test_v6_int_duid_no_hostname_match(self):
        """When duid is int and query does NOT match hostname, row is excluded gracefully."""
        reservations = [
            {"ip-addresses": ["2001:db8::1"], "hostname": "host6", "duid": 999},
        ]
        result = _filter_reservations(reservations, q="nomatch", subnet_id=None, version=6)
        self.assertEqual(len(result), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Coverage: Reservation edit GET with option-data not a list (~lines 680-684)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4EditOptionDataNotList(_ReservationViewBase):
    """Edit GET when reservation has option-data as a non-list value."""

    _SUBNET_ID = 1
    _IP = "192.168.1.100"

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation4_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_option_data_as_string_handled_gracefully(self, MockKeaClient):
        """When option-data is a string instead of list, view must not crash."""
        reservation = dict(_SAMPLE_RESERVATION4, **{"option-data": "not-a-list"})
        MockKeaClient.return_value.reservation_get.return_value = reservation
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        # options_formset should have no initial data since string was rejected
        formset = response.context["options_formset"]
        self.assertEqual(len(formset.initial_forms), 0)

    @patch("netbox_kea.models.KeaClient")
    def test_option_data_as_dict_handled_gracefully(self, MockKeaClient):
        """When option-data is a dict instead of list, view must not crash."""
        reservation = dict(_SAMPLE_RESERVATION4, **{"option-data": {"name": "routers", "data": "10.0.0.1"}})
        MockKeaClient.return_value.reservation_get.return_value = reservation
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        formset = response.context["options_formset"]
        self.assertEqual(len(formset.initial_forms), 0)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6EditOptionDataNotList(_ReservationViewBase):
    """Edit GET when DHCPv6 reservation has option-data as a non-list value."""

    _SUBNET_ID = 1
    _IP = "2001:db8::100"

    def _edit_url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation6_edit",
            args=[self.server.pk, self._SUBNET_ID, self._IP],
        )

    @patch("netbox_kea.models.KeaClient")
    def test_option_data_as_int_handled_gracefully(self, MockKeaClient):
        """When option-data is an int instead of list, view must not crash."""
        reservation = dict(_SAMPLE_RESERVATION6, **{"option-data": 42})
        MockKeaClient.return_value.reservation_get.return_value = reservation
        response = self.client.get(self._edit_url())
        self.assertEqual(response.status_code, 200)
        formset = response.context["options_formset"]
        self.assertEqual(len(formset.initial_forms), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Coverage: _enrich_reservations_with_lease_status error paths
# ─────────────────────────────────────────────────────────────────────────────


class TestEnrichReservationsWithLeaseStatus(SimpleTestCase):
    """_enrich_reservations_with_lease_status gracefully handles error paths."""

    def _make_mock_client(self, command_side_effect=None, command_return=None):
        """Create a mock KeaClient with clone() context-manager wired up."""
        mock_client = MagicMock()
        _wire_mock_clone(mock_client)
        if command_side_effect is not None:
            mock_client.command.side_effect = command_side_effect
        elif command_return is not None:
            mock_client.command.return_value = command_return
        return mock_client

    def test_malformed_arguments_not_dict_sets_indeterminate(self):
        """When Kea returns arguments as a string (not dict), has_active_lease stays unset."""
        from netbox_kea.views.reservations import _enrich_reservations_with_lease_status

        mock_client = self._make_mock_client(
            command_return=[{"result": 0, "arguments": "bad-string"}],
        )
        reservations = [{"subnet-id": 1, "ip-address": "10.0.0.1"}]
        _enrich_reservations_with_lease_status(mock_client, reservations, version=4)
        # indeterminate — has_active_lease should not be set
        self.assertNotIn("has_active_lease", reservations[0])

    def test_malformed_leases_not_list_sets_indeterminate(self):
        """When arguments.leases is not a list, has_active_lease stays unset."""
        from netbox_kea.views.reservations import _enrich_reservations_with_lease_status

        mock_client = self._make_mock_client(
            command_return=[{"result": 0, "arguments": {"leases": "not-a-list"}}],
        )
        reservations = [{"subnet-id": 1, "ip-address": "10.0.0.1"}]
        _enrich_reservations_with_lease_status(mock_client, reservations, version=4)
        self.assertNotIn("has_active_lease", reservations[0])

    def test_kea_exception_non_result_2_sets_indeterminate(self):
        """KeaException with result!=2 (not hook-missing) marks subnet as indeterminate."""
        from netbox_kea.views.reservations import _enrich_reservations_with_lease_status

        mock_client = self._make_mock_client(
            command_side_effect=KeaException({"result": 1, "text": "internal error"}, index=0),
        )
        reservations = [{"subnet-id": 1, "ip-address": "10.0.0.1"}]
        _enrich_reservations_with_lease_status(mock_client, reservations, version=4)
        self.assertNotIn("has_active_lease", reservations[0])

    def test_requests_exception_sets_indeterminate(self):
        """requests.RequestException in worker thread marks subnet as indeterminate."""
        import requests as req_lib

        from netbox_kea.views.reservations import _enrich_reservations_with_lease_status

        mock_client = self._make_mock_client(
            command_side_effect=req_lib.ConnectionError("timeout"),
        )
        reservations = [{"subnet-id": 1, "ip-address": "10.0.0.1"}]
        _enrich_reservations_with_lease_status(mock_client, reservations, version=4)
        self.assertNotIn("has_active_lease", reservations[0])

    def test_empty_reservations_returns_early(self):
        """Empty reservations list returns immediately without any API calls."""
        from netbox_kea.views.reservations import _enrich_reservations_with_lease_status

        mock_client = self._make_mock_client()
        _enrich_reservations_with_lease_status(mock_client, [], version=4)
        mock_client.clone.assert_not_called()

    def test_no_subnet_ids_returns_early(self):
        """Reservations without valid subnet-id return early without API calls."""
        from netbox_kea.views.reservations import _enrich_reservations_with_lease_status

        mock_client = self._make_mock_client()
        reservations = [{"ip-address": "10.0.0.1"}]  # no subnet-id
        _enrich_reservations_with_lease_status(mock_client, reservations, version=4)
        mock_client.clone.assert_not_called()

    def test_result_3_empty_subnet_sets_false(self):
        """When Kea returns result=3 (empty), has_active_lease should be False."""
        from netbox_kea.views.reservations import _enrich_reservations_with_lease_status

        mock_client = self._make_mock_client(
            command_return=[{"result": 3, "text": "no leases found", "arguments": {}}],
        )
        reservations = [{"subnet-id": 1, "ip-address": "10.0.0.1"}]
        _enrich_reservations_with_lease_status(mock_client, reservations, version=4)
        self.assertFalse(reservations[0]["has_active_lease"])

    def test_v6_enrichment_checks_ip_addresses_list(self):
        """DHCPv6 enrichment checks ip-addresses list for active lease match."""
        from netbox_kea.views.reservations import _enrich_reservations_with_lease_status

        mock_client = self._make_mock_client(
            command_return=[
                {
                    "result": 0,
                    "arguments": {
                        "leases": [{"ip-address": "2001:db8::100"}],
                    },
                }
            ],
        )
        reservations = [{"subnet-id": 1, "ip-addresses": ["2001:db8::100"]}]
        _enrich_reservations_with_lease_status(mock_client, reservations, version=6)
        self.assertTrue(reservations[0]["has_active_lease"])


# ─────────────────────────────────────────────────────────────────────────────
# Coverage: _run_reservation_success_side_effects sync exception
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestRunReservationSuccessSideEffectsSyncFail(_ReservationViewBase):
    """Reservation add succeeds in Kea but sync_reservation_to_netbox raises."""

    def _add_url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    def _valid_post_data(self):
        return {
            "subnet_id": 1,
            "ip_address": "192.168.1.100",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
            "sync_to_netbox": "on",
        }

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_sync_db_error_shows_warning_reservation_still_created(self, MockKeaClient, mock_sync):
        """Reservation created in Kea; sync raises DatabaseError → warning shown, no 500."""
        from django.db import DatabaseError

        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        mock_sync.side_effect = DatabaseError("db constraint violation")
        response = self.client.post(self._add_url(), self._valid_post_data())
        # Kea reservation created → redirect
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_add.assert_called_once()
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("sync failed" in m.lower() for m in msgs))
        self.assertFalse(any("db constraint violation" in m for m in msgs))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_sync_validation_error_shows_warning(self, MockKeaClient, mock_sync):
        """sync raises ValidationError → warning shown, reservation still created."""
        from django.core.exceptions import ValidationError

        mock_client = MockKeaClient.return_value
        mock_client.reservation_add.return_value = None
        mock_sync.side_effect = ValidationError("bad data")
        response = self.client.post(self._add_url(), self._valid_post_data())
        self.assertEqual(response.status_code, 302)
        mock_client.reservation_add.assert_called_once()
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("sync" in m.lower() or "warning" in m.lower() or "failed" in m.lower() for m in msgs))
        self.assertFalse(any("bad data" in m for m in msgs))


# ─────────────────────────────────────────────────────────────────────────────
# Coverage: _filter_reservations with non-string fields — search by identifier
# ─────────────────────────────────────────────────────────────────────────────


class TestFilterReservationsSearchByIdentifier(SimpleTestCase):
    """_filter_reservations search via identifier value (hw-address for v4, duid for v6)."""

    def test_v4_search_by_hw_address_value(self):
        """Searching for hw-address hex string finds the matching reservation."""
        reservations = [
            {"ip-address": "10.0.0.1", "hostname": "host1", "hw-address": "aa:bb:cc:dd:ee:ff"},
            {"ip-address": "10.0.0.2", "hostname": "host2", "hw-address": "11:22:33:44:55:66"},
        ]
        result = _filter_reservations(reservations, q="aa:bb", subnet_id=None, version=4)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ip-address"], "10.0.0.1")

    def test_v6_search_by_duid_value(self):
        """Searching for duid hex string finds the matching v6 reservation."""
        reservations = [
            {"ip-addresses": ["2001:db8::1"], "hostname": "host6a", "duid": "00:01:02:03"},
            {"ip-addresses": ["2001:db8::2"], "hostname": "host6b", "duid": "ff:ee:dd:cc"},
        ]
        result = _filter_reservations(reservations, q="ff:ee", subnet_id=None, version=6)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["hostname"], "host6b")

    def test_v4_search_by_client_id(self):
        """Searching for client-id finds the reservation."""
        reservations = [
            {"ip-address": "10.0.0.1", "hostname": "h1", "hw-address": "", "client-id": "01:aa:bb:cc:dd:ee:ff"},
        ]
        result = _filter_reservations(reservations, q="01:aa:bb", subnet_id=None, version=4)
        self.assertEqual(len(result), 1)

    def test_v6_search_by_ip_addresses_element(self):
        """Searching matches individual items in the ip-addresses list."""
        reservations = [
            {"ip-addresses": ["2001:db8::1", "2001:db8::2"], "hostname": "multi", "duid": "00:01"},
        ]
        result = _filter_reservations(reservations, q="db8::2", subnet_id=None, version=6)
        self.assertEqual(len(result), 1)

    def test_subnet_filter_only(self):
        """Filtering by subnet_id without q returns only matching subnet."""
        reservations = [
            {"subnet-id": 1, "ip-address": "10.0.0.1"},
            {"subnet-id": 2, "ip-address": "10.0.1.1"},
        ]
        result = _filter_reservations(reservations, q="", subnet_id=1, version=4)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["subnet-id"], 1)

    def test_empty_query_and_no_subnet_returns_all(self):
        """No filter applied returns all reservations."""
        reservations = [
            {"subnet-id": 1, "ip-address": "10.0.0.1"},
            {"subnet-id": 2, "ip-address": "10.0.1.1"},
        ]
        result = _filter_reservations(reservations, q="", subnet_id=None, version=4)
        self.assertEqual(len(result), 2)
