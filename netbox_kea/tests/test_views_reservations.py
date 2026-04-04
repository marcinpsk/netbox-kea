# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for netbox_kea plugin.

Also contains pure-Python unit tests for helper functions defined in views.py
(e.g. ``_extract_identifier``), which do not require a database but live here
because they are tightly coupled to view logic.

These tests verify correct HTTP responses and redirect behaviour for every view.
All Kea HTTP calls are mocked so no running Kea instance is required.

Test organisation strategy
--------------------------
Each view class gets its own ``TestCase`` subclass so failures are isolated and
clearly named.  Every test that triggers a redirect asserts that the redirect URL
contains an *integer* pk (never the string "None"), which is the pattern that
revealed the original ``POST /plugins/kea/servers/None`` 404 bug.

View tests use ``django.test.TestCase`` because they write to the test database
(user + server fixtures).  Server objects are created via ``Server.objects.create()``
which does **not** call ``Model.clean()`` and therefore does not trigger live Kea
connectivity checks.
"""

import unittest as _unittest  # alias to avoid pytest collection confusion
from unittest.mock import MagicMock, patch

import requests as req
from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse

from netbox_kea.views import _get_reservation_identifier as _extract_identifier

from .utils import _PLUGINS_CONFIG, _ViewTestBase


class TestExtractIdentifier(_unittest.TestCase):
    """Unit tests for the ``_extract_identifier()`` helper in ``views.py``.

    The function walks a Kea reservation dict looking for identifier keys in
    priority order (v4: hw-address > client-id > circuit-id > flex-id;
    v6: duid > hw-address > client-id > flex-id).
    """

    def test_v4_prefers_hw_address(self):
        r = {"hw-address": "aa:bb:cc:dd:ee:ff", "client-id": "01:aa:bb", "subnet-id": 1}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "aa:bb:cc:dd:ee:ff")

    def test_v4_client_id_when_no_hw_address(self):
        r = {"client-id": "01:aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "client-id")
        self.assertEqual(ival, "01:aa:bb:cc:dd:ee:ff")

    def test_v4_circuit_id(self):
        r = {"circuit-id": "0a:1b:2c"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "circuit-id")
        self.assertEqual(ival, "0a:1b:2c")

    def test_v4_flex_id_as_last_resort(self):
        r = {"flex-id": "aabbccdd"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "flex-id")
        self.assertEqual(ival, "aabbccdd")

    def test_v4_hw_address_beats_flex_id(self):
        r = {"flex-id": "aabbccdd", "hw-address": "aa:bb:cc"}
        itype, _ = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")

    def test_v6_prefers_duid_over_hw_address(self):
        r = {"duid": "00:01:02:03:04:05", "hw-address": "aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 6)
        self.assertEqual(itype, "duid")
        self.assertEqual(ival, "00:01:02:03:04:05")

    def test_v6_hw_address_fallback_when_no_duid(self):
        r = {"hw-address": "aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 6)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "aa:bb:cc:dd:ee:ff")

    def test_fallback_returns_hw_address_empty_string(self):
        """When no known identifier key is present return ``("hw-address", "")``.

        This keeps the form pre-population logic from crashing.
        """
        r = {"subnet-id": 1, "ip-address": "10.0.0.1"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "")


# ---------------------------------------------------------------------------
# Reservation list exception paths
# ---------------------------------------------------------------------------

_FORMSET_MGMT = {
    "options-TOTAL_FORMS": "0",
    "options-INITIAL_FORMS": "0",
    "options-MIN_NUM_FORMS": "0",
    "options-MAX_NUM_FORMS": "1000",
}

_VALID_RESERVATION4_POST = {
    "subnet_id": "1",
    "ip_address": "10.0.0.55",
    "identifier_type": "hw-address",
    "identifier": "aa:bb:cc:dd:ee:ff",
    "hostname": "test-host",
    **_FORMSET_MGMT,
}

# Edit-shaped payload — omits disabled fields (subnet_id, ip_address) as a real browser would.
_VALID_RESERVATION4_EDIT_POST = {
    "identifier_type": "hw-address",
    "identifier": "aa:bb:cc:dd:ee:ff",
    "hostname": "test-host",
    **_FORMSET_MGMT,
}

_VALID_RESERVATION6_POST = {
    "subnet_id": "1",
    "ip_addresses": "2001:db8::1",
    "identifier_type": "duid",
    "identifier": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
    "hostname": "test-host6",
    **_FORMSET_MGMT,
}

# Edit form payload — subnet_id and ip_addresses are disabled on the edit form so
# browsers never submit them.  The view reads ip-addresses from reservation_get instead.
_VALID_RESERVATION6_EDIT_POST = {
    "identifier_type": "duid",
    "identifier": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
    "hostname": "test-host6",
    **_FORMSET_MGMT,
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4ListExceptions(_ViewTestBase):
    """Reservation list view — exception path coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_hook_not_available_shows_warning(self, MockKeaClient):
        """KeaException result=2 sets hook_available=False without crashing."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_page.side_effect = KeaException(
            {"result": 2, "text": "hook not loaded"}, index=0
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context.get("hook_available", True))

    @patch("netbox_kea.models.KeaClient")
    def test_network_error_during_fetch_keeps_hook_available(self, MockKeaClient):
        """requests.RequestException during reservation_get_page keeps hook_available=True.

        Transport errors do not indicate the hook is missing — only result==2 does.
        """
        MockKeaClient.return_value.reservation_get_page.side_effect = req.RequestException("connection refused")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("hook_available", False))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_non_result2_keeps_hook_available(self, MockKeaClient):
        """KeaException with result!=2 keeps hook_available=True.

        Only result==2 (unknown command = hook not loaded) should hide the hook UI.
        """
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_page.side_effect = KeaException(
            {"result": 1, "text": "general error"}, index=0
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("hook_available", False))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6ListExceptions(_ViewTestBase):
    """Reservation6 list view — exception path coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_hook_not_available_shows_warning(self, MockKeaClient):
        """KeaException result=2 sets hook_available=False without crashing."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_page.side_effect = KeaException(
            {"result": 2, "text": "hook not loaded"}, index=0
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context.get("hook_available", True))

    @patch("netbox_kea.models.KeaClient")
    def test_network_error_during_fetch_keeps_hook_available(self, MockKeaClient):
        """requests.RequestException during reservation_get_page keeps hook_available=True.

        Transport errors do not indicate the hook is missing — only result==2 does.
        """
        MockKeaClient.return_value.reservation_get_page.side_effect = req.RequestException("timeout")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("hook_available", False))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_non_result2_keeps_hook_available(self, MockKeaClient):
        """KeaException with result!=2 keeps hook_available=True.

        Only result==2 (unknown command = hook not loaded) should hide the hook UI.
        """
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_page.side_effect = KeaException(
            {"result": 1, "text": "general error"}, index=0
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("hook_available", False))


# ---------------------------------------------------------------------------
# Reservation4Add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4AddExceptions(_ViewTestBase):
    """ServerReservation4AddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError must redirect with a warning message."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_with_sync_to_netbox(self, MockKeaClient):
        """PartialPersistError with sync_to_netbox=True must attempt IPAM sync."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), True)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called()

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_sync_failure_shows_warning(self, MockKeaClient):
        """PartialPersistError + sync error must show two warnings."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=ValueError("sync boom")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException must re-render the form with an error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "already exists"}, index=0
        )
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_result1_rerenders_form(self, MockKeaClient):
        """KeaException from reservation_add must re-render the form with an error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "server error"}, index=0
        )
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_success_with_sync_to_netbox(self, MockKeaClient):
        """Successful add with sync_to_netbox=True must call sync and show info message."""
        MockKeaClient.return_value.reservation_add.return_value = None
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), True)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_success_sync_failure_shows_warning(self, MockKeaClient):
        """Successful add where sync raises must show a warning (no 500)."""
        MockKeaClient.return_value.reservation_add.return_value = None
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=ValueError("sync fail")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Reservation6Add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6AddExceptions(_ViewTestBase):
    """ServerReservation6AddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError must redirect with a warning message."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp6", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_with_sync_to_netbox(self, MockKeaClient):
        """PartialPersistError with sync_to_netbox=True must attempt IPAM sync and show recovery message."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp6", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), False)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs), "Expected warning about partial persist")

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_sync_failure_shows_warning(self, MockKeaClient):
        """PartialPersistError + sync exception must show warning about sync failure."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp6", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=ValueError("sync boom")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException must re-render the form with an error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "conflict"}, index=0
        )
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_propagates(self, MockKeaClient):
        """Unexpected exception must propagate (not be silently caught)."""
        MockKeaClient.return_value.reservation_add.side_effect = RuntimeError("bang")
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        with self.assertRaises(RuntimeError):
            self.client.post(self._url(), _VALID_RESERVATION6_POST)


# ---------------------------------------------------------------------------
# Reservation4Edit exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4EditExceptions(_ViewTestBase):
    """ServerReservation4EditView GET and POST exception paths."""

    def _url(self, subnet_id=1, ip="10.0.0.55"):
        return reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, subnet_id, ip])

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_on_kea_exception(self, MockKeaClient):
        """GET that raises KeaException during reservation fetch must redirect."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get.side_effect = KeaException(
            {"result": 1, "text": "server error"}, index=0
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_on_transport_error(self, MockKeaClient):
        """GET that raises RequestException during reservation fetch must redirect."""
        import requests as _req

        MockKeaClient.return_value.reservation_get.side_effect = _req.ConnectionError("down")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_get_404_when_reservation_not_found(self, MockKeaClient):
        """GET must return 404 when reservation_get returns None."""
        MockKeaClient.return_value.reservation_get.return_value = None
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_error_redirects(self, MockKeaClient):
        """PartialPersistError on reservation_update must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        response = self.client.post(self._url(), _VALID_RESERVATION4_EDIT_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync(self, MockKeaClient):
        """PartialPersistError with sync_to_netbox attempts sync."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        post_data = {**_VALID_RESERVATION4_EDIT_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), True)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_sync_failure(self, MockKeaClient):
        """PartialPersistError + sync failure shows warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        post_data = {**_VALID_RESERVATION4_EDIT_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=ValueError("sync")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException on reservation_update must re-render the form with error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_update.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        response = self.client.post(self._url(), _VALID_RESERVATION4_EDIT_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(
            any("Kea reported an error" in str(m) for m in msgs), "Expected KeaException hint in flash message"
        )

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_propagates(self, MockKeaClient):
        """Unexpected exception on reservation_update must propagate."""
        MockKeaClient.return_value.reservation_update.side_effect = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.client.post(self._url(), _VALID_RESERVATION4_EDIT_POST)

    @patch("netbox_kea.models.KeaClient")
    def test_post_success_with_sync(self, MockKeaClient):
        """Successful update with sync_to_netbox calls sync and shows info."""
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {**_VALID_RESERVATION4_EDIT_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), False)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_success_sync_failure_shows_warning(self, MockKeaClient):
        """Successful update where sync raises must show warning."""
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {**_VALID_RESERVATION4_EDIT_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=ValueError("oops")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Reservation6Edit exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6EditExceptions(_ViewTestBase):
    """ServerReservation6EditView GET and POST exception paths."""

    def _url(self, subnet_id=1, ip="2001:db8::1"):
        return reverse("plugins:netbox_kea:server_reservation6_edit", args=[self.server.pk, subnet_id, ip])

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_on_kea_exception(self, MockKeaClient):
        """GET that raises KeaException during reservation fetch must redirect."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_on_transport_error(self, MockKeaClient):
        """GET that raises RequestException during reservation fetch must redirect."""
        import requests as _req

        MockKeaClient.return_value.reservation_get.side_effect = _req.ConnectionError("down")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_get_404_when_reservation_not_found(self, MockKeaClient):
        """GET must return 404 when reservation_get returns None."""
        MockKeaClient.return_value.reservation_get.return_value = None
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_error_redirects(self, MockKeaClient):
        """PartialPersistError on reservation_update must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_get.return_value = {"ip-addresses": ["2001:db8::1"]}
        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        response = self.client.post(self._url(), _VALID_RESERVATION6_EDIT_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException on reservation_update must re-render the form."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get.return_value = {"ip-addresses": ["2001:db8::1"]}
        MockKeaClient.return_value.reservation_update.side_effect = KeaException(
            {"result": 1, "text": "error"}, index=0
        )
        response = self.client.post(self._url(), _VALID_RESERVATION6_EDIT_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_propagates(self, MockKeaClient):
        """Unexpected exception on reservation_update must propagate."""
        MockKeaClient.return_value.reservation_get.return_value = {"ip-addresses": ["2001:db8::1"]}
        MockKeaClient.return_value.reservation_update.side_effect = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.client.post(self._url(), _VALID_RESERVATION6_EDIT_POST)


# ---------------------------------------------------------------------------
# Reservation4Delete exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4DeleteExceptions(_ViewTestBase):
    """ServerReservation4DeleteView POST exception paths."""

    def _url(self, subnet_id=1, ip="10.0.0.55"):
        return reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, subnet_id, ip])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError must redirect with warning and still run side effects."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_del.side_effect = PartialPersistError("dhcp4", Exception("write"))
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_shows_error(self, MockKeaClient):
        """KeaException must show an error message and redirect."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_del.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_propagates(self, MockKeaClient):
        """Unexpected exception must propagate (not show a generic error message)."""
        MockKeaClient.return_value.reservation_del.side_effect = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.client.post(self._url())


# ---------------------------------------------------------------------------
# Reservation6Delete exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6DeleteExceptions(_ViewTestBase):
    """ServerReservation6DeleteView POST exception paths."""

    def _url(self, subnet_id=1, ip="2001:db8::1"):
        return reverse("plugins:netbox_kea:server_reservation6_delete", args=[self.server.pk, subnet_id, ip])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_redirects_with_warning(self, MockKeaClient):
        """PartialPersistError must redirect with warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_del.side_effect = PartialPersistError("dhcp6", Exception("write"))
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_shows_error(self, MockKeaClient):
        """KeaException must show an error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_del.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_propagates(self, MockKeaClient):
        """Unexpected exception must propagate (not show a generic error message)."""
        MockKeaClient.return_value.reservation_del.side_effect = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.client.post(self._url())


# ---------------------------------------------------------------------------
# _get_reservation_options_formset — partial submission path
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetReservationOptionsFormsetPartial(_ViewTestBase):
    """Line 77-79: partial options-* keys but no management form."""

    def test_partial_options_keys_returns_invalid_formset(self):
        """When options-* keys exist without management form, returns (formset, False)."""
        from netbox_kea.views import _build_reservation_options_formset

        post_data = {"options-0-name": "domain-name-servers"}  # no TOTAL_FORMS key
        fs, is_valid = _build_reservation_options_formset(post_data)
        self.assertFalse(is_valid)


# ---------------------------------------------------------------------------
# Reservation list enrichment — thread pool exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationListEnrichmentExceptions(_ViewTestBase):
    """Lines 1641-1663: enrichment thread pool exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_no_reservations_skips_enrichment(self, MockKeaClient):
        """Line 1650: empty reservation list → enrichment returns early."""
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.clone.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_thread_pool_generic_exception_returns_early(self, MockKeaClient):
        """Line 1662-1663: generic exception in thread pool causes enrichment to return."""
        MockKeaClient.return_value.reservation_get_page.return_value = (
            [{"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}],
            0,
            0,
        )
        # lease4-get-all raises an unexpected error — must set on the cloned client
        # since _enrich_reservations_with_lease_status calls client.clone()
        MockKeaClient.return_value.clone.return_value.command.side_effect = RuntimeError("unexpected")
        MockKeaClient.return_value.clone.return_value.__enter__ = MagicMock(
            return_value=MockKeaClient.return_value.clone.return_value
        )
        MockKeaClient.return_value.clone.return_value.__exit__ = MagicMock(return_value=None)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.clone.return_value.command.assert_called()


# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6AddOptionDataAndSync(_ViewTestBase):
    """Lines 2008, 2027-2034: Reservation6 add with option-data and sync."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_option_data_included(self, MockKeaClient):
        """option-data is included in reservation payload when formset has entries."""
        MockKeaClient.return_value.reservation_add.return_value = None
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {
            **_VALID_RESERVATION6_POST,
            "options-TOTAL_FORMS": "1",
            "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            "options-0-name": "dns-servers",
            "options-0-data": "2001:4860:4860::8888",
            "options-0-always_send": "",
            "options-0-DELETE": "",
        }
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(MockKeaClient.return_value.reservation_add.called)
        call_args = MockKeaClient.return_value.reservation_add.call_args
        # Extract the reservation dict (second positional arg to reservation_add)
        reservation_dict = (
            call_args[0][1]
            if call_args[0] and len(call_args[0]) > 1
            else (call_args.kwargs or {}).get("reservation", {})
        )
        option_data = reservation_dict.get("option-data", [])
        dns_entry = next((o for o in option_data if o.get("name") == "dns-servers"), None)
        self.assertIsNotNone(dns_entry, "dns-servers option not found in reservation option-data")
        self.assertEqual(dns_entry["data"], "2001:4860:4860::8888")

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_success(self, MockKeaClient, mock_sync):
        """sync_to_netbox=on → sync called once and success message queued."""
        MockKeaClient.return_value.reservation_add.return_value = None
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        mock_sync.return_value = (MagicMock(), True)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()
        msgs = list(response.context["messages"])
        self.assertTrue(
            any(
                m.level == django_messages.INFO
                and ("synced" in m.message.lower() or "created" in m.message.lower() or "updated" in m.message.lower())
                for m in msgs
            ),
            f"Expected sync success message, got: {[m.message for m in msgs]}",
        )

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_exception_shows_warning(self, MockKeaClient, mock_sync):
        """sync raises exception → warning message queued (reservation still created)."""
        MockKeaClient.return_value.reservation_add.return_value = None
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        mock_sync.side_effect = ValueError("sync failed")
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()
        msgs = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.WARNING for m in msgs),
            f"Expected a WARNING message on sync failure, got: {[(m.level, m.message) for m in msgs]}",
        )


# ---------------------------------------------------------------------------
# Reservation6 Edit — option-data and sync paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6EditOptionDataAndSync(_ViewTestBase):
    """Lines 2292, 2307-2314, 2327-2334: Reservation6 edit with option-data and sync."""

    def _url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation6_edit",
            args=[self.server.pk, 1, "2001:db8::1"],
        )

    def _mock_get(self, MockKeaClient):
        MockKeaClient.return_value.reservation_get.return_value = {
            "subnet-id": 1,
            "ip-addresses": ["2001:db8::1", "2001:db8::2"],
            "duid": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
            "hostname": "v6host",
            "option-data": [],
        }

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_option_data(self, MockKeaClient):
        """option-data is included in reservation_update payload when formset has entries.

        Uses edit-shaped payload (no subnet_id/ip_addresses) and a two-address mock_get
        to verify the multi-IP preserve path runs and both IPs are preserved.
        """
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {
            **_VALID_RESERVATION6_EDIT_POST,
            "options-TOTAL_FORMS": "1",
            "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            "options-0-name": "ntp-servers",
            "options-0-data": "2001:db8::1:1",
            "options-0-always_send": "",
            "options-0-DELETE": "",
        }
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(MockKeaClient.return_value.reservation_update.called)
        call_args = MockKeaClient.return_value.reservation_update.call_args
        reservation_dict = (
            call_args[0][1]
            if call_args[0] and len(call_args[0]) > 1
            else (call_args.kwargs or {}).get("reservation", {})
        )
        # Both original IPs must be preserved (not collapsed to one).
        self.assertEqual(
            reservation_dict.get("ip-addresses"),
            ["2001:db8::1", "2001:db8::2"],
            "Edit must preserve all existing ip-addresses from reservation_get",
        )
        option_data = reservation_dict.get("option-data", [])
        ntp_entry = next((o for o in option_data if o.get("name") == "ntp-servers"), None)
        self.assertIsNotNone(ntp_entry, "ntp-servers option not found in reservation option-data")
        self.assertEqual(ntp_entry["data"], "2001:db8::1:1")

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_success(self, MockKeaClient, mock_sync):
        """sync_to_netbox=on → sync called once and info message queued."""
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        mock_sync.return_value = (MagicMock(), False)
        post_data = {**_VALID_RESERVATION6_EDIT_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()
        msgs = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.INFO for m in msgs),
            f"Expected INFO message on sync success, got: {[(m.level, m.message) for m in msgs]}",
        )

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_exception(self, MockKeaClient, mock_sync):
        """sync exception → warning message queued (reservation still updated)."""
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        mock_sync.side_effect = ValueError("sync fail")
        post_data = {**_VALID_RESERVATION6_EDIT_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()
        msgs = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.WARNING for m in msgs),
            f"Expected WARNING message on sync failure, got: {[(m.level, m.message) for m in msgs]}",
        )

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync(self, MockKeaClient, mock_sync):
        """Lines 2327-2334: PartialPersistError + sync success."""
        from netbox_kea.kea import PartialPersistError

        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        mock_sync.return_value = (MagicMock(), True)
        post_data = {**_VALID_RESERVATION6_EDIT_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync_exception(self, MockKeaClient, mock_sync):
        """Lines 2332-2334: PartialPersistError + sync exception → warning."""
        from netbox_kea.kea import PartialPersistError

        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        mock_sync.side_effect = ValueError("db error")
        post_data = {**_VALID_RESERVATION6_EDIT_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# _enrich_reservations_with_lease_status — edge cases (lines 1641, 1645, 1650, 1662-1663)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichReservationsLeaseStatusCoverage(_ViewTestBase):
    """Direct unit tests for _enrich_reservations_with_lease_status helper."""

    def test_result3_returns_empty_list(self):
        """Line 1641: lease-get-all result=3 → _fetch_leases_for_subnet returns []."""
        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        clone_mock = MagicMock()
        clone_mock.__enter__ = MagicMock(return_value=clone_mock)
        clone_mock.__exit__ = MagicMock(return_value=None)
        clone_mock.command.return_value = [{"result": 3, "arguments": {}}]
        client.clone.return_value = clone_mock
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        # Should not raise; lease_cmds result=3 → empty list → no has_active_lease set
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # result=3 means empty — reservation has no active lease
        self.assertFalse(reservations[0].get("has_active_lease", True))

    def test_kea_exception_non_result2_returns_empty(self):
        """Line 1645: KeaException with result != 2 → _fetch_leases_for_subnet returns []."""
        from netbox_kea.kea import KeaException
        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        clone_mock = MagicMock()
        clone_mock.__enter__ = MagicMock(return_value=clone_mock)
        clone_mock.__exit__ = MagicMock(return_value=None)
        clone_mock.command.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        client.clone.return_value = clone_mock
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # result != 2 → subnet indeterminate → has_active_lease must remain unset
        self.assertNotIn("has_active_lease", reservations[0])

    def test_no_subnet_id_skips_fetch(self):
        """Line 1650: reservations with no subnet-id → unique_subnet_ids empty → early return."""
        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        reservations = [{"ip-address": "10.0.0.1"}]  # no subnet-id
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # client.clone should never be called when there are no valid subnet-ids
        client.clone.assert_not_called()

    def test_as_completed_exception_returns_early(self):
        """Lines 1662-1663: exception from as_completed → outer except fires."""
        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        client.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        with patch(
            "netbox_kea.views.reservations.concurrent.futures.as_completed",
            side_effect=RuntimeError("as_completed failed"),
        ) as mock_as_completed:
            _enrich_reservations_with_lease_status(client, reservations, 4)
        # as_completed must have been reached (executor submitted tasks)
        mock_as_completed.assert_called_once()
        # outer except returns early — has_active_lease stays unset
        self.assertNotIn("has_active_lease", reservations[0])


# ---------------------------------------------------------------------------
# _warn_pool_reservation_overlap — edge cases (lines 2503, 2516, 2522-2523)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWarnPoolReservationOverlapCoverage(_ViewTestBase):
    """Direct unit tests for _warn_pool_reservation_overlap helper."""

    def test_cidr_pool_creates_ipnetwork(self):
        """Line 2503: pool_str without dash (CIDR) → IPNetwork path."""
        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        client.reservation_get_page.return_value = ([], 0, 0)
        request = self._make_request()
        # Should not raise; CIDR pool path
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0/24")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)

    def test_host_with_different_subnet_id_skipped(self):
        """Line 2516: host whose subnet-id != requested subnet_id → continue."""
        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        # Return a host with subnet-id=999 (different from requested subnet_id=1)
        client.reservation_get_page.side_effect = [
            ([{"subnet-id": 999, "ip-address": "10.0.0.5"}], 0, 0),
        ]
        request = self._make_request()
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0-10.0.0.100")
        # host skipped → no warning
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)

    def test_malformed_ip_skipped(self):
        """Lines 2522-2523: malformed IP string → IPAddress raises → inner except fires."""
        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        client.reservation_get_page.side_effect = [
            ([{"subnet-id": 1, "ip-address": "NOT_AN_IP"}], 0, 0),
        ]
        request = self._make_request()
        # Should not raise; malformed IP is silently skipped
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0-10.0.0.100")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)


# ---------------------------------------------------------------------------
# _warn_reservation_pool_overlap — edge cases (lines 2566, 2571, 2579-2580)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWarnReservationPoolOverlapCoverage(_ViewTestBase):
    """Direct unit tests for _warn_reservation_pool_overlap helper."""

    def test_empty_pool_string_skipped(self):
        """Line 2566: pool entry with empty pool string → continue."""
        from netbox_kea.views import _warn_reservation_pool_overlap

        client = MagicMock()
        client.command.return_value = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": ""}]}]},
            }
        ]
        request = self._make_request()
        # Should not raise; empty pool string is skipped
        _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)

    def test_cidr_pool_creates_ipnetwork(self):
        """Line 2571: CIDR pool (no dash) → IPNetwork path."""
        from netbox_kea.views import _warn_reservation_pool_overlap

        client = MagicMock()
        client.command.return_value = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.0/24"}]}]},
            }
        ]
        request = self._make_request()
        # IP is in pool → warning issued; CIDR pool path (line 2571)
        _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].level, django_messages.WARNING)

    def test_client_command_exception_swallowed(self):
        """Lines 2579-2580: client.command raises → outer except fires."""
        from netbox_kea.views import _warn_reservation_pool_overlap

        client = MagicMock()
        client.command.side_effect = req.RequestException("network failure")
        request = self._make_request()
        # Should not raise; exception is swallowed
        _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)


# ---------------------------------------------------------------------------
# Reservation mutation bare except — programming errors must propagate
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationMutationBareExcept(_ViewTestBase):
    """Reservation mutation handlers must not swallow programming errors via bare except Exception."""

    @patch("netbox_kea.models.KeaClient")
    def test_attribute_error_from_add_propagates(self, MockKeaClient):
        """An AttributeError from reservation_add must propagate (not become an error message)."""
        MockKeaClient.return_value.reservation_add.side_effect = AttributeError("programming bug")
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        with self.assertRaises(AttributeError):
            self.client.post(
                url,
                {
                    **_VALID_RESERVATION4_POST,
                },
            )

    @patch("netbox_kea.models.KeaClient")
    def test_attribute_error_from_v6_add_propagates(self, MockKeaClient):
        """An AttributeError from reservation_add (v6) must propagate."""
        MockKeaClient.return_value.reservation_add.side_effect = AttributeError("programming bug")
        url = reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])
        with self.assertRaises(AttributeError):
            self.client.post(url, {**_VALID_RESERVATION6_POST})
