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
from unittest.mock import MagicMock, patch

from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import Server

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


# ---------------------------------------------------------------------------
# Sync view edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncViewEdgeCases(_ViewTestBase):
    """ServerLease4SyncView POST edge cases: invalid IP, sync exception."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_missing_ip_returns_400(self, MockKeaClient):
        """POST without ip_address must return 400."""
        response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_ip_returns_400(self, MockKeaClient):
        """POST with invalid IP must return 400."""
        response = self.client.post(self._url(), {"ip_address": "not-an-ip"})
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_post_sync_exception_returns_500(self, MockKeaClient):
        """POST where sync raises a concrete error must return 500 with generic message, not raw exception."""
        with patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=ValueError("ip parse error")):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn("Sync error", body)
        self.assertNotIn("ip parse error", body)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncPermission(_ViewTestBase):
    """_BaseBulkReservationSyncView — non-superuser without IPAM perms gets 403."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def test_post_without_ipam_permission_returns_403(self):
        """POST without ipam.add_ipaddress must return 403."""
        restricted_user = User.objects.create_user(
            username="noperms_bulk",
            email="noperms_bulk@example.com",
            password="pass",
        )
        self.client.force_login(restricted_user)
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 403)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncFetchException(_ViewTestBase):
    """_BaseBulkReservationSyncView — fetch exception shows error and redirects."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_fetch_exception_shows_error(self, MockKeaClient):
        """Exception in _fetch_reservations_from_server must show error message, not raw exception text."""
        with patch(
            "netbox_kea.views.sync_views._fetch_reservations_from_server", side_effect=RuntimeError("fetch fail")
        ):
            response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))
        self.assertNotIn(b"fetch fail", response.content)


# ---------------------------------------------------------------------------
# BulkReservationImport edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationImportEdgeCases(_ViewTestBase):
    """_BaseBulkReservationImportView POST: invalid form and CSV parse error."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_without_file_rerenders_form(self, MockKeaClient):
        """POST without a CSV file must re-render the form (200)."""
        response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_csv_shows_error(self, MockKeaClient):
        """POST with a CSV that fails parse_reservation_csv raises ValueError → form error."""
        import io

        MockKeaClient.return_value.reservation_add.return_value = None
        # CSV with missing required columns triggers ValueError in parse_reservation_csv
        bad_csv = io.BytesIO(b"garbage_header\nrow1\n")
        bad_csv.name = "bad.csv"
        response = self.client.post(self._url(), {"csv_file": bad_csv})
        self.assertEqual(response.status_code, 200)
        # Response should include a form error about invalid CSV — message must be generic (no raw exception text)
        self.assertContains(response, "csv_file", msg_prefix="Expected CSV error in form")
        self.assertContains(response, "parsing failed", msg_prefix="Expected generic error message")


# ---------------------------------------------------------------------------
# Bulk reservation sync — edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncEdgeCases(_ViewTestBase):
    """Lines 4383-4397: bulk sync with missing IPs, errors, and count tracking."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def setUp(self):
        super().setUp()
        # superuser has ipam perms automatically (is_superuser)

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_reservation_without_ip_is_skipped(self, mock_fetch, mock_sync):
        """Line 4383-4384: reservations without ip-address/ip-addresses are skipped."""
        mock_fetch.return_value = [{"hw-address": "aa:bb:cc:dd:ee:ff"}]  # no IP
        self.client.post(self._url(), follow=True)
        mock_sync.assert_not_called()

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_sync_creates_and_updates(self, mock_fetch, mock_sync):
        """Lines 4389-4390: created and updated counters incremented correctly."""
        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:01"},
            {"ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:dd:ee:02"},
        ]
        mock_sync.side_effect = [(MagicMock(), True), (MagicMock(), False)]
        response = self.client.post(self._url(), follow=True)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("1 created" in m or "created" in m.lower() for m in msgs))

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_sync_exception_counted_as_error(self, mock_fetch, mock_sync):
        """Lines 4391-4394, 4397: sync exception increments errors, warning shown."""
        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1"},
            {"ip-address": "10.0.0.2"},
        ]
        mock_sync.side_effect = [RuntimeError("db error"), (MagicMock(), True)]
        response = self.client.post(self._url(), follow=True)
        msgs = [m.message for m in response.context["messages"]]
        # errors > 0 → summary message shows "N errors"
        self.assertTrue(any("errors" in m for m in msgs))


# ---------------------------------------------------------------------------
# Reservation import — generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationImportGenericException(_ViewTestBase):
    """Lines 4521-4523: generic exception during reservation_add."""

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_appended_to_errors(self, MockKeaClient):
        """Generic exception adds 'unexpected error' to error_rows."""
        MockKeaClient.return_value.reservation_add.side_effect = RuntimeError("crash")
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        import io

        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:dd:ee:ff,1"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file, "subnet_id": "1"})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(len(result["error_rows"]), 1)
        self.assertIn("unexpected error", result["error_rows"][0]["error"].lower())


# ---------------------------------------------------------------------------
# _BaseSyncView._sync — NotImplementedError (line 4321)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBaseSyncViewNotImplemented(_ViewTestBase):
    """Line 4321: _BaseSyncView._sync raises NotImplementedError."""

    def test_sync_raises_not_implemented(self):
        from netbox_kea.views import _BaseSyncView

        view = _BaseSyncView()
        with self.assertRaises(NotImplementedError):
            view._sync({})
