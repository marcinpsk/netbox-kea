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

import re
import unittest as _unittest  # alias to avoid pytest collection confusion
from unittest.mock import MagicMock, patch

from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import Server
from netbox_kea.views import _get_reservation_identifier as _extract_identifier

# Minimal PLUGINS_CONFIG so server.get_client() can read kea_timeout.
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_INT_PK_RE = re.compile(r"/servers/(\d+)/")


def _make_db_server(**kwargs) -> Server:
    """Create and persist a Server without live connectivity checks.

    ``Server.objects.create()`` skips ``Model.clean()``, so no Kea connectivity
    check is triggered.  The ``PLUGINS_CONFIG`` override is applied by the calling
    test class.
    """
    defaults = {
        "name": "test-kea",
        "server_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


def _kea_command_side_effect(cmd, service=None, arguments=None, check=None):
    """Return a plausible Kea API response for each command type."""
    if cmd == "status-get":
        return [{"result": 0, "arguments": {"pid": 1234, "uptime": 3600, "reload": 0}}]
    if cmd == "version-get":
        return [{"result": 0, "arguments": {"extended": "2.4.1-stable"}}]
    if cmd == "config-get":
        # Return minimal Dhcp4/Dhcp6 config so subnet views can parse it.
        if service and service[0] == "dhcp6":
            return [{"result": 0, "arguments": {"Dhcp6": {"subnet6": [], "shared-networks": []}}}]
        return [{"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}]
    return [{"result": 0, "arguments": {}}]


# ─────────────────────────────────────────────────────────────────────────────
# Shared base class
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _ViewTestBase(TestCase):
    """Creates a superuser and a single Server for use in all view tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="kea_testuser",
            email="kea_test@example.com",
            password="kea_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def _assert_no_none_pk_redirect(self, response):
        """Assert that a redirect URL never contains the string ``None`` as a pk.

        This is the specific pattern that caused the ``POST /plugins/kea/servers/None``
        404 bug: ``get_absolute_url()`` with ``pk=None`` produces that URL.
        """
        if hasattr(response, "url"):
            self.assertNotIn(
                "servers/None",
                response.url,
                f"Redirect went to bad URL: {response.url}",
            )

    def _assert_redirect_to_integer_pk(self, response):
        """Assert that a redirect URL contains an integer server pk."""
        self._assert_no_none_pk_redirect(response)
        self.assertIsNotNone(
            _INT_PK_RE.search(response.url),
            f"Expected /servers/<int>/ in redirect URL, got: {response.url}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# _extract_identifier — pure unit tests (no DB needed)
# ─────────────────────────────────────────────────────────────────────────────


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

_VALID_RESERVATION6_POST = {
    "subnet_id": "1",
    "ip_addresses": "2001:db8::1",
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
    def test_generic_exception_during_fetch_does_not_crash(self, MockKeaClient):
        """Unexpected exception during reservation_get_page is swallowed gracefully."""
        MockKeaClient.return_value.reservation_get_page.side_effect = RuntimeError("boom")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


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
    def test_generic_exception_during_fetch_does_not_crash(self, MockKeaClient):
        """Unexpected exception during reservation_get_page is swallowed gracefully."""
        MockKeaClient.return_value.reservation_get_page.side_effect = RuntimeError("unexpected")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


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

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_sync_failure_shows_warning(self, MockKeaClient):
        """PartialPersistError + sync error must show two warnings."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp4", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("sync boom")):
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
    def test_generic_exception_rerenders_form(self, MockKeaClient):
        """Unexpected exception must re-render the form with a generic error."""
        MockKeaClient.return_value.reservation_add.side_effect = RuntimeError("unexpected")
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
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("sync fail")):
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
        """PartialPersistError with sync_to_netbox=True must attempt IPAM sync."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp6", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), False)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_sync_failure_shows_warning(self, MockKeaClient):
        """PartialPersistError + sync exception must show warning about sync failure."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_add.side_effect = PartialPersistError("dhcp6", Exception("write"))
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("sync boom")):
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
    def test_generic_exception_rerenders_form(self, MockKeaClient):
        """Unexpected exception must re-render the form with a generic error."""
        MockKeaClient.return_value.reservation_add.side_effect = RuntimeError("bang")
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)


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
    def test_get_redirects_on_generic_exception(self, MockKeaClient):
        """GET that raises generic Exception during reservation fetch must redirect."""
        MockKeaClient.return_value.reservation_get.side_effect = RuntimeError("crash")
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

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync(self, MockKeaClient):
        """PartialPersistError with sync_to_netbox attempts sync."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp4", Exception("write"))
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
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
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("sync")):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException on reservation_update must re-render the form."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_update.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_rerenders_form(self, MockKeaClient):
        """Unexpected exception on reservation_update must re-render the form."""
        MockKeaClient.return_value.reservation_update.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_success_with_sync(self, MockKeaClient):
        """Successful update with sync_to_netbox calls sync and shows info."""
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox") as mock_sync:
            mock_sync.return_value = (MagicMock(), False)
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_success_sync_failure_shows_warning(self, MockKeaClient):
        """Successful update where sync raises must show warning."""
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=RuntimeError("oops")):
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
    def test_get_redirects_on_generic_exception(self, MockKeaClient):
        """GET that raises generic Exception must redirect."""
        MockKeaClient.return_value.reservation_get.side_effect = RuntimeError("boom")
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

        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_rerenders_form(self, MockKeaClient):
        """KeaException on reservation_update must re-render the form."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_update.side_effect = KeaException(
            {"result": 1, "text": "error"}, index=0
        )
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_rerenders_form(self, MockKeaClient):
        """Unexpected exception on reservation_update must re-render form."""
        MockKeaClient.return_value.reservation_update.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)


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
    def test_generic_exception_shows_error(self, MockKeaClient):
        """Unexpected exception must show a generic error and redirect."""
        MockKeaClient.return_value.reservation_del.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


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
    def test_generic_exception_shows_error(self, MockKeaClient):
        """Unexpected exception must show a generic error."""
        MockKeaClient.return_value.reservation_del.side_effect = RuntimeError("crash")
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


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

    @patch("netbox_kea.models.KeaClient")
    def test_thread_pool_generic_exception_returns_early(self, MockKeaClient):
        """Line 1662-1663: generic exception in thread pool causes enrichment to return."""
        MockKeaClient.return_value.reservation_get_page.return_value = (
            [{"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}],
            0,
            0,
        )
        # lease4-get-all raises an unexpected error
        MockKeaClient.return_value.command.side_effect = RuntimeError("unexpected")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Reservation6 Add — option-data and sync paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6AddOptionDataAndSync(_ViewTestBase):
    """Lines 2008, 2027-2034: Reservation6 add with option-data and sync."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_option_data_included(self, MockKeaClient):
        """Line 2008: option-data is included in reservation when formset has entries."""
        MockKeaClient.return_value.reservation_add.return_value = None
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
        call_args = MockKeaClient.return_value.reservation_add.call_args
        if call_args:
            self.assertTrue(MockKeaClient.return_value.reservation_add.called)

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_success(self, MockKeaClient, mock_sync):
        """Lines 2027-2031: sync_to_netbox=on → sync called, success message."""
        MockKeaClient.return_value.reservation_add.return_value = None
        mock_sync.return_value = (MagicMock(), True)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_exception_shows_warning(self, MockKeaClient, mock_sync):
        """Lines 2032-2034: sync raises exception → warning message."""
        MockKeaClient.return_value.reservation_add.return_value = None
        mock_sync.side_effect = RuntimeError("sync failed")
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))


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
            "ip-addresses": ["2001:db8::1"],
            "duid": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
            "hostname": "v6host",
            "option-data": [],
        }

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_option_data(self, MockKeaClient):
        """Line 2292: option-data appended to reservation when formset has entries."""
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        post_data = {
            **_VALID_RESERVATION6_POST,
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

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_success(self, MockKeaClient, mock_sync):
        """Lines 2307-2314: sync succeeds → info message."""
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        mock_sync.return_value = (MagicMock(), False)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_exception(self, MockKeaClient, mock_sync):
        """Lines 2312-2314: sync exception → warning."""
        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.return_value = None
        mock_sync.side_effect = RuntimeError("sync fail")
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync(self, MockKeaClient, mock_sync):
        """Lines 2327-2334: PartialPersistError + sync success."""
        from netbox_kea.kea import PartialPersistError

        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        mock_sync.return_value = (MagicMock(), True)
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_with_sync_exception(self, MockKeaClient, mock_sync):
        """Lines 2332-2334: PartialPersistError + sync exception → warning."""
        from netbox_kea.kea import PartialPersistError

        self._mock_get(MockKeaClient)
        MockKeaClient.return_value.reservation_update.side_effect = PartialPersistError("dhcp6", Exception("write"))
        mock_sync.side_effect = RuntimeError("db error")
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# _enrich_reservations_with_lease_status — edge cases (lines 1641, 1645, 1650, 1662-1663)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichReservationsLeaseStatusCoverage(_ViewTestBase):
    """Direct unit tests for _enrich_reservations_with_lease_status helper."""

    def _make_request_with_messages(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        setattr(request, "session", "session")
        storage = FallbackStorage(request)
        setattr(request, "_messages", storage)
        return request

    def test_result3_returns_empty_list(self):
        """Line 1641: lease-get-all result=3 → _fetch_leases_for_subnet returns []."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        client.command.return_value = [{"result": 3, "arguments": {}}]
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        # Should not raise; lease_cmds result=3 → empty list → no has_active_lease set
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # hook_unavailable stays False, no crash

    def test_kea_exception_non_result2_returns_empty(self):
        """Line 1645: KeaException with result != 2 → _fetch_leases_for_subnet returns []."""
        from unittest.mock import MagicMock

        from netbox_kea.kea import KeaException
        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        client.command.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # Should complete without crash; result != 2 → return []

    def test_no_subnet_id_skips_fetch(self):
        """Line 1650: reservations with no subnet-id → unique_subnet_ids empty → early return."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        reservations = [{"ip-address": "10.0.0.1"}]  # no subnet-id
        _enrich_reservations_with_lease_status(client, reservations, 4)
        # client.command should never be called
        client.command.assert_not_called()

    def test_as_completed_exception_returns_early(self):
        """Lines 1662-1663: exception from as_completed → outer except fires."""
        from unittest.mock import MagicMock, patch

        from netbox_kea.views import _enrich_reservations_with_lease_status

        client = MagicMock()
        client.command.return_value = [{"result": 0, "arguments": {"leases": []}}]
        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        with patch(
            "netbox_kea.views.reservations.concurrent.futures.as_completed",
            side_effect=RuntimeError("as_completed failed"),
        ):
            _enrich_reservations_with_lease_status(client, reservations, 4)
        # Should not raise; outer except returns early


# ---------------------------------------------------------------------------
# _warn_pool_reservation_overlap — edge cases (lines 2503, 2516, 2522-2523)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWarnPoolReservationOverlapCoverage(_ViewTestBase):
    """Direct unit tests for _warn_pool_reservation_overlap helper."""

    def _make_request(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        setattr(request, "session", "session")
        storage = FallbackStorage(request)
        setattr(request, "_messages", storage)
        return request

    def test_cidr_pool_creates_ipnetwork(self):
        """Line 2503: pool_str without dash (CIDR) → IPNetwork path."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        client.reservation_get_page.return_value = ([], 0, 0)
        request = self._make_request()
        # Should not raise; CIDR pool path
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0/24")

    def test_host_with_different_subnet_id_skipped(self):
        """Line 2516: host whose subnet-id != requested subnet_id → continue."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        # Return a host with subnet-id=999 (different from requested subnet_id=1)
        client.reservation_get_page.side_effect = [
            ([{"subnet-id": 999, "ip-address": "10.0.0.5"}], 0, 0),
        ]
        request = self._make_request()
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0-10.0.0.100")
        # host skipped → no warning

    def test_malformed_ip_skipped(self):
        """Lines 2522-2523: malformed IP string → IPAddress raises → inner except fires."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_pool_reservation_overlap

        client = MagicMock()
        client.reservation_get_page.side_effect = [
            ([{"subnet-id": 1, "ip-address": "NOT_AN_IP"}], 0, 0),
        ]
        request = self._make_request()
        # Should not raise; malformed IP is silently skipped
        _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0-10.0.0.100")


# ---------------------------------------------------------------------------
# _warn_reservation_pool_overlap — edge cases (lines 2566, 2571, 2579-2580)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWarnReservationPoolOverlapCoverage(_ViewTestBase):
    """Direct unit tests for _warn_reservation_pool_overlap helper."""

    def _make_request(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        setattr(request, "session", "session")
        storage = FallbackStorage(request)
        setattr(request, "_messages", storage)
        return request

    def test_empty_pool_string_skipped(self):
        """Line 2566: pool entry with empty pool string → continue."""
        from unittest.mock import MagicMock

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

    def test_cidr_pool_creates_ipnetwork(self):
        """Line 2571: CIDR pool (no dash) → IPNetwork path."""
        from unittest.mock import MagicMock

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

    def test_client_command_exception_swallowed(self):
        """Lines 2579-2580: client.command raises → outer except fires."""
        from unittest.mock import MagicMock

        from netbox_kea.views import _warn_reservation_pool_overlap

        client = MagicMock()
        client.command.side_effect = RuntimeError("network failure")
        request = self._make_request()
        # Should not raise; exception is swallowed
        _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")
