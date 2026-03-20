# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for Phase 2: Reservation Management.

These tests will FAIL until the reservation views, URL patterns, and form classes
are implemented.  They define the expected HTTP behaviour for all reservation
CRUD operations on both DHCPv4 and DHCPv6 servers.

URL names expected (not yet registered):
  server_reservations4         — GET  /servers/<pk>/reservations4/
  server_reservations6         — GET  /servers/<pk>/reservations6/
  server_reservation4_add      — GET/POST /servers/<pk>/reservations4/add/
  server_reservation6_add      — GET/POST /servers/<pk>/reservations6/add/
  server_reservation4_edit     — GET/POST /servers/<pk>/reservations4/<subnet_id>/<ip>/edit/
  server_reservation6_edit     — GET/POST /servers/<pk>/reservations6/<subnet_id>/<ip>/edit/
  server_reservation4_delete   — GET/POST /servers/<pk>/reservations4/<subnet_id>/<ip>/delete/
  server_reservation6_delete   — GET/POST /servers/<pk>/reservations6/<subnet_id>/<ip>/delete/
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.kea import KeaException
from netbox_kea.models import Server

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
    def test_hook_still_available_on_general_kea_error(self, MockKeaClient):
        """Result code 1 (general Kea error) must NOT hide the reservations UI.

        Only result code 2 (unknown command) means the hook is not loaded.
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
        # POST with bad ip_address
        data = self._valid_post_data()
        data["ip_address"] = "not-an-ip"
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

    def _mock_with_lease(self, MockKeaClient):
        """Reservation + matching active lease for 192.168.1.100."""
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get_page.return_value = ([dict(_SAMPLE_RESERVATION4)], 0, 0)
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
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get_page.return_value = ([dict(_SAMPLE_RESERVATION4)], 0, 0)
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
        mock_client = MockKeaClient.return_value
        mock_client.reservation_get_page.return_value = ([dict(_SAMPLE_RESERVATION4)], 0, 0)
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
        mock.reservation_get_page.return_value = ([], 0, 0)
        mock.command.return_value = [{"result": 0, "arguments": {**self._LEASE}}]
        response = self._htmx_get({"by": "ip", "q": "192.168.1.200"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserve")

    @patch("netbox_kea.models.KeaClient")
    def test_reserved_badge_shown_when_reservation_exists(self, MockKeaClient):
        """A lease WITH a matching reservation must show 'Reserved' link, not '+ Reserve'."""
        mock = MockKeaClient.return_value
        reservation = dict(_SAMPLE_RESERVATION4)
        reservation["ip-address"] = "192.168.1.200"
        mock.reservation_get_page.return_value = ([reservation], 0, 0)
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
        # Synced link shown in netbox_ip column — but NO individual hx-post Sync button
        self.assertNotContains(response, "hx-post")


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
        mock_client.subnet_add.side_effect = Exception("subnet already exists")
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
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "see server logs for details")


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
        mock_client.subnet_del.side_effect = Exception("subnet not found")
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
