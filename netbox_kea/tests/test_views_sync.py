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

import io
import re
from unittest.mock import MagicMock, patch

from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.kea import KeaException
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
        """requests.RequestException from _fetch_reservations_from_server must show error and redirect."""
        import requests as req_lib

        with patch(
            "netbox_kea.views.sync_views._fetch_reservations_from_server",
            side_effect=req_lib.ConnectionError("fetch fail"),
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

        MockKeaClient.return_value.reservation_add.return_value = None
        # CSV with missing required columns triggers ValueError in parse_reservation_csv
        bad_csv = io.BytesIO(b"garbage_header\nrow1\n")
        bad_csv.name = "bad.csv"
        response = self.client.post(self._url(), {"csv_file": bad_csv})
        self.assertEqual(response.status_code, 200)
        # Response should include a form error about invalid CSV — message must be generic (no raw exception text)
        self.assertContains(response, "csv_file", msg_prefix="Expected CSV error in form")
        self.assertContains(response, "parsing failed", msg_prefix="Expected generic error message")
        MockKeaClient.return_value.reservation_add.assert_not_called()


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
        """Created and updated counters incremented correctly."""
        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:01"},
            {"ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:dd:ee:02"},
        ]
        mock_sync.side_effect = [(MagicMock(), True), (MagicMock(), False)]
        response = self.client.post(self._url(), follow=True)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertIn("Bulk sync complete: 1 created, 1 updated.", msgs)
        self.assertEqual(mock_sync.call_count, 2)

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_sync_exception_counted_as_error(self, mock_fetch, mock_sync):
        """Sync exception increments errors, warning shown."""
        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1"},
            {"ip-address": "10.0.0.2"},
        ]
        mock_sync.side_effect = [ValueError("db error"), (MagicMock(), True)]
        response = self.client.post(self._url(), follow=True)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertIn("Bulk sync: 1 created, 0 updated, 1 errors.", msgs)
        self.assertEqual(mock_sync.call_count, 2)


# ---------------------------------------------------------------------------
# Reservation import — generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationImportGenericException(_ViewTestBase):
    """Lines 4521-4523: generic exception during reservation_add."""

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_appended_to_errors(self, MockKeaClient):
        """RuntimeError from reservation_add is caught and surfaced as an error row."""
        MockKeaClient.return_value.reservation_add.side_effect = RuntimeError("crash")
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:dd:ee:ff,1"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file, "subnet_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["errors"], 1)
        self.assertEqual(response.context["result"]["error_rows"][0]["error"], "An unexpected error occurred.")


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


# ---------------------------------------------------------------------------
# ServerReservation4/6SyncView._fetch_live_data
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4SyncViewFetchLiveData(_ViewTestBase):
    """ServerReservation4SyncView._fetch_live_data uses reservation_get_by_ip."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_uses_live_reservation_when_found(self, MockKeaClient):
        """When reservation_get_by_ip returns a dict, that dict is passed to _sync."""
        live = {"ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:00:00:01", "hostname": "livehost"}
        MockKeaClient.return_value.reservation_get_by_ip.return_value = live

        with patch("netbox_kea.views.ServerReservation4SyncView._sync") as mock_sync:
            mock_sync.return_value = (MagicMock(), True)
            self.client.post(self._url(), {"ip_address": "10.0.0.5", "hostname": "fallback"})

        mock_sync.assert_called_once()
        data = mock_sync.call_args[0][0]
        self.assertEqual(data["hostname"], "livehost")
        MockKeaClient.return_value.reservation_get_by_ip.assert_called_once_with(4, "10.0.0.5")

    @patch("netbox_kea.models.KeaClient")
    def test_falls_back_to_synthetic_when_reservation_not_found(self, MockKeaClient):
        """When reservation_get_by_ip returns None, response is 400 (no sync)."""
        MockKeaClient.return_value.reservation_get_by_ip.return_value = None

        with patch("netbox_kea.views.ServerReservation4SyncView._sync") as mock_sync:
            response = self.client.post(self._url(), {"ip_address": "10.0.0.5", "hostname": "fallback"})
            self.assertEqual(response.status_code, 400)
            mock_sync.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_falls_back_on_kea_exception(self, MockKeaClient):
        """When reservation_get_by_ip raises KeaException, response is 400."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_by_ip.side_effect = KeaException({"result": 1, "text": "not found"})

        with patch("netbox_kea.views.ServerReservation4SyncView._sync") as mock_sync:
            response = self.client.post(self._url(), {"ip_address": "10.0.0.5", "hostname": "fallback"})
            self.assertEqual(response.status_code, 400)
            mock_sync.assert_not_called()


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6SyncViewFetchLiveData(_ViewTestBase):
    """ServerReservation6SyncView._fetch_live_data uses reservation_get_by_ip for v6."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_calls_reservation_get_by_ip_with_version_6(self, MockKeaClient):
        """Calls reservation_get_by_ip with version=6 for the v6 view."""
        MockKeaClient.return_value.reservation_get_by_ip.return_value = None

        response = self.client.post(self._url(), {"ip_address": "2001:db8::1", "hostname": ""})

        MockKeaClient.return_value.reservation_get_by_ip.assert_called_once_with(6, "2001:db8::1")
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_falls_back_on_request_exception(self, MockKeaClient):
        """When reservation_get_by_ip raises requests.RequestException, response is 400."""
        import requests as req

        MockKeaClient.return_value.reservation_get_by_ip.side_effect = req.RequestException("timeout")

        response = self.client.post(self._url(), {"ip_address": "2001:db8::1", "hostname": "fallback6"})

        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# TestFetchLiveDataNoSyntheticFallback  (F11)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLiveDataNoSyntheticFallback(_ViewTestBase):
    """_fetch_live_data must NOT mutate NetBox when Kea returns None or errors."""

    @patch("netbox_kea.models.KeaClient")
    def test_kea_not_found_returns_400(self, MockKeaClient):
        """When Kea returns no lease (not found), response is 400 (no sync)."""
        MockKeaClient.return_value.lease_get_by_ip.return_value = None
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        response = self.client.post(url, {"ip_address": "10.0.0.99"})
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_returns_400(self, MockKeaClient):
        """When Kea raises an exception, response is 400 (no sync)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.lease_get_by_ip.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        response = self.client.post(url, {"ip_address": "10.0.0.99"})
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_kea_found_calls_sync(self, MockKeaClient):
        """When Kea returns a lease, _sync IS called."""
        MockKeaClient.return_value.lease_get_by_ip.return_value = {
            "ip-address": "10.0.0.1",
            "hw-address": "aa:bb:cc:00:00:01",
            "hostname": "realhost",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }
        with patch("netbox_kea.views.ServerLease4SyncView._sync") as mock_sync:
            mock_sync.return_value = (MagicMock(), True)
            url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
            response = self.client.post(url, {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()


# ---------------------------------------------------------------------------
# TestReservationImportBareExcept  (F10)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationImportBareExcept(_ViewTestBase):
    """Reservation import catches all per-row exceptions and surfaces them as error rows."""

    @patch("netbox_kea.models.KeaClient")
    def test_attribute_error_surfaced_as_error_row(self, MockKeaClient):
        """An AttributeError from reservation_add is caught and surfaced as an error row."""
        MockKeaClient.return_value.reservation_add.side_effect = AttributeError("bug")
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:dd:ee:ff,1"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file, "subnet_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["errors"], 1)
        self.assertEqual(response.context["result"]["error_rows"][0]["error"], "An unexpected error occurred.")


# ---------------------------------------------------------------------------
# TestLeaseImportBareExcept  (F12)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseImportBareExcept(_ViewTestBase):
    """Lease import catches specific per-row exceptions and surfaces them as error rows."""

    @patch("netbox_kea.models.KeaClient")
    def test_attribute_error_is_row_error(self, MockKeaClient):
        """An AttributeError from lease_add is caught per-row and surfaced as an error row."""
        MockKeaClient.return_value.lease_add.side_effect = AttributeError("bug")
        url = reverse("plugins:netbox_kea:server_lease4_bulk_import", args=[self.server.pk])

        csv_content = "ip-address,hw-address,hostname,valid-lft,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,host1,86400,1"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "leases.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["errors"], 1)
        self.assertEqual(
            response.context["result"]["error_rows"][0]["error"],
            "An unexpected error occurred.",
        )


# ---------------------------------------------------------------------------
# TestImportLoopValueError  (F8)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestImportLoopValueError(_ViewTestBase):
    """Import loops must handle ValueError from Kea client."""

    @patch("netbox_kea.models.KeaClient")
    def test_reservation_import_value_error_is_row_error(self, MockKeaClient):
        """ValueError from reservation_add must be treated as a row error, not abort the import."""
        mock_client = MockKeaClient.return_value
        call_count = {"n": 0}

        def side_effect(service, row):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("bad JSON from Kea")

        mock_client.reservation_add.side_effect = side_effect

        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n10.0.0.2,aa:bb:cc:00:00:02,1\n"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(call_count["n"], 2)
        result = response.context["result"]
        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(result["error_rows"]), 1)
        self.assertIn("Invalid response from Kea", result["error_rows"][0]["error"])

    @patch("netbox_kea.models.KeaClient")
    def test_lease_import_value_error_is_row_error(self, MockKeaClient):
        """ValueError from lease_add must be treated as a row error, not abort the import."""
        mock_client = MockKeaClient.return_value
        call_count = {"n": 0}

        def side_effect(version, row):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("bad JSON from Kea")

        mock_client.lease_add.side_effect = side_effect

        url = reverse("plugins:netbox_kea:server_lease4_bulk_import", args=[self.server.pk])

        csv_content = "ip-address\n10.0.0.1\n10.0.0.2\n"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "leases.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(call_count["n"], 2)
        result = response.context["result"]
        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(result["error_rows"]), 1)
        self.assertIn("Invalid response from Kea", result["error_rows"][0]["error"])


# ---------------------------------------------------------------------------
# TestBulkReservationSyncExceptNarrowing  (F9)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncExceptNarrowing(_ViewTestBase):
    """_BaseBulkReservationSyncView must not swallow programming errors."""

    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_attribute_error_is_caught_and_redirects(self, mock_fetch):
        """An AttributeError from _fetch_reservations_from_server is caught and redirects."""
        mock_fetch.side_effect = AttributeError("programming bug")

        url = reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)


# ---------------------------------------------------------------------------
# TestBulkSyncBatchCleanup  (#30)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkSyncBatchCleanup(_ViewTestBase):
    """Bulk sync defers stale-IP cleanup to a single batch pass (#30)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_bulk_sync_calls_sync_with_cleanup_false(self, mock_fetch, mock_sync, mock_batch):
        """Each record is synced with cleanup=False; batch cleanup runs after."""
        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1", "hostname": "h1.example.com"},
            {"ip-address": "10.0.0.2", "hostname": "h1.example.com"},
        ]
        mock_sync.side_effect = [(MagicMock(), True), (MagicMock(), False)]
        self.client.post(self._url(), follow=True)
        # Both calls must use cleanup=False
        for call in mock_sync.call_args_list:
            self.assertEqual(call.kwargs.get("cleanup"), False)
        # Batch cleanup called once with both records
        mock_batch.assert_called_once()
        synced_records = mock_batch.call_args[0][0]
        self.assertEqual(len(synced_records), 2)

    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=3)
    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_stale_cleaned_count_appears_in_message(self, mock_fetch, mock_sync, mock_batch):
        """When batch cleanup removes IPs, the count appears in the success message."""
        mock_fetch.return_value = [{"ip-address": "10.0.0.1", "hostname": "h.example.com"}]
        mock_sync.return_value = (MagicMock(), True)
        response = self.client.post(self._url(), follow=True)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("3 stale cleaned" in m for m in msgs))

    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_batch_cleanup_skipped_when_errors(self, mock_fetch, mock_sync, mock_batch):
        """When sync errors occur, batch cleanup is skipped entirely (incomplete keep-set)."""
        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1", "hostname": "h1"},
            {"ip-address": "10.0.0.2", "hostname": "h2"},
        ]
        mock_sync.side_effect = [ValueError("db error"), (MagicMock(), True)]
        self.client.post(self._url(), follow=True)
        mock_batch.assert_not_called()


# ---------------------------------------------------------------------------
# Coverage: _BaseSyncView.post() DB error during sync (~lines 56-63)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBaseSyncViewDBError(_ViewTestBase):
    """_BaseSyncView.post() handles DB errors from _sync gracefully."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_integrity_error_returns_500(self, MockKeaClient):
        """IntegrityError from _sync returns 500 with generic message."""
        from django.db import IntegrityError

        MockKeaClient.return_value.lease_get_by_ip.return_value = {
            "ip-address": "10.0.0.1",
            "hw-address": "aa:bb:cc:00:00:01",
            "hostname": "host1",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }
        with patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=IntegrityError("duplicate key")):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn("Sync error", body)
        self.assertNotIn("duplicate key", body)

    @patch("netbox_kea.models.KeaClient")
    def test_operational_error_returns_500(self, MockKeaClient):
        """OperationalError from _sync returns 500 with generic message."""
        from django.db.utils import OperationalError

        MockKeaClient.return_value.lease_get_by_ip.return_value = {
            "ip-address": "10.0.0.2",
            "hw-address": "aa:bb:cc:00:00:02",
            "hostname": "host2",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }
        with patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=OperationalError("db conn failed")):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.2"})
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn("Sync error", body)
        self.assertNotIn("db conn failed", body)

    @patch("netbox_kea.models.KeaClient")
    def test_programming_error_returns_500(self, MockKeaClient):
        """ProgrammingError from _sync returns 500."""
        from django.db.utils import ProgrammingError

        MockKeaClient.return_value.lease_get_by_ip.return_value = {
            "ip-address": "10.0.0.3",
            "hw-address": "aa:bb:cc:00:00:03",
            "hostname": "host3",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }
        with patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=ProgrammingError("bad query")):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.3"})
        self.assertEqual(response.status_code, 500)

    @patch("netbox_kea.models.KeaClient")
    def test_validation_error_returns_500(self, MockKeaClient):
        """ValidationError from _sync returns 500."""
        from django.core.exceptions import ValidationError

        MockKeaClient.return_value.lease_get_by_ip.return_value = {
            "ip-address": "10.0.0.4",
            "hw-address": "aa:bb:cc:00:00:04",
            "hostname": "host4",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }
        with patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=ValidationError("invalid data")):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.4"})
        self.assertEqual(response.status_code, 500)


# ---------------------------------------------------------------------------
# Coverage: Bulk reservation sync fetch failure (~lines 161-188)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncFetchFailure(_ViewTestBase):
    """Bulk sync shows error when reservation fetch fails."""

    def _url_v4(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def _url_v6(self):
        return reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_kea_exception_on_fetch_shows_error_and_redirects(self, mock_fetch):
        """KeaException during fetch shows error with hint and redirects."""
        from netbox_kea.kea import KeaException

        mock_fetch.side_effect = KeaException(
            {"result": 1, "text": "hook not loaded"},
            index=0,
        )
        response = self.client.post(self._url_v4(), follow=True)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any(m for m in msgs if "hook not loaded" in m.lower() or "failed" in m.lower()))

    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_type_error_on_fetch_shows_generic_error(self, mock_fetch):
        """TypeError during fetch shows generic error."""
        mock_fetch.side_effect = TypeError("unexpected type")
        response = self.client.post(self._url_v4(), follow=True)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Failed to fetch" in m for m in msgs))
        self.assertFalse(any("unexpected type" in m for m in msgs))

    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_v6_kea_exception_on_fetch_redirects_to_v6_list(self, mock_fetch):
        """v6 KeaException redirects to the v6 reservation list."""
        from netbox_kea.kea import KeaException

        mock_fetch.side_effect = KeaException(
            {"result": 2, "text": "unknown command"},
            index=0,
        )
        response = self.client.post(self._url_v6())
        self.assertEqual(response.status_code, 302)
        self.assertIn("reservations6", response.url)


# ---------------------------------------------------------------------------
# Coverage: Per-row error isolation in bulk import (~lines 354-369)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkImportPerRowErrorIsolation(_ViewTestBase):
    """If one reservation in a batch raises KeaException, remaining items still process."""

    @patch("netbox_kea.models.KeaClient")
    def test_one_row_fails_others_succeed(self, MockKeaClient):
        """First row raises KeaException, second succeeds, third raises ValueError."""
        call_count = {"n": 0}

        def side_effect(service, row):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise KeaException({"result": 1, "text": "conflict"}, index=0)
            if call_count["n"] == 3:
                raise ValueError("bad response")

        MockKeaClient.return_value.reservation_add.side_effect = side_effect
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

        csv_content = (
            "ip-address,hw-address,subnet-id\n"
            "10.0.0.1,aa:bb:cc:00:00:01,1\n"
            "10.0.0.2,aa:bb:cc:00:00:02,1\n"
            "10.0.0.3,aa:bb:cc:00:00:03,1\n"
        )
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], 2)
        self.assertEqual(call_count["n"], 3)

    @patch("netbox_kea.models.KeaClient")
    def test_already_exists_counted_as_skipped(self, MockKeaClient):
        """KeaException with result=1 and 'already exist' text is counted as skipped."""
        MockKeaClient.return_value.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "Host already exists in subnet 1."},
            index=0,
        )
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 0)

    @patch("netbox_kea.models.KeaClient")
    def test_connection_error_per_row(self, MockKeaClient):
        """requests.RequestException per-row is caught and recorded."""
        import requests as req_lib

        MockKeaClient.return_value.reservation_add.side_effect = req_lib.ConnectionError("timeout")
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["errors"], 1)
        self.assertIn("Connection error", result["error_rows"][0]["error"])


# ---------------------------------------------------------------------------
# Coverage: Reservation sync view for v6 with IntegrityError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6SyncDBError(_ViewTestBase):
    """ServerReservation6SyncView handles DB errors from sync."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_integrity_error_returns_500(self, MockKeaClient):
        """IntegrityError from _sync returns 500."""
        from django.db import IntegrityError

        MockKeaClient.return_value.reservation_get_by_ip.return_value = {
            "ip-addresses": ["2001:db8::1"],
            "duid": "00:01:02:03:04:05",
            "hostname": "host6",
            "subnet-id": 1,
        }
        with patch(
            "netbox_kea.views.ServerReservation6SyncView._sync",
            side_effect=IntegrityError("dup"),
        ):
            response = self.client.post(self._url(), {"ip_address": "2001:db8::1"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"dup", response.content)


# ---------------------------------------------------------------------------
# Coverage: _BaseSyncView.post() — OperationalError during sync
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncViewOperationalError(_ViewTestBase):
    """_BaseSyncView.post() handles OperationalError from _sync."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_operational_error_from_sync_returns_500(self, MockKeaClient):
        """OperationalError during sync returns 500 with generic message."""
        from django.db.utils import OperationalError

        MockKeaClient.return_value.lease_get_by_ip.return_value = {
            "ip-address": "10.0.0.10",
            "hw-address": "aa:bb:cc:00:00:10",
            "hostname": "host10",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        }
        with patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=OperationalError("conn lost")):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.10"})
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn("Sync error", body)
        self.assertNotIn("conn lost", body)


# ---------------------------------------------------------------------------
# Coverage: Lease6 sync view — live fetch failure
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease6SyncViewFetchFailure(_ViewTestBase):
    """ServerLease6SyncView._fetch_live_data returns None on failure → 400."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease6_sync", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_returns_400(self, MockKeaClient):
        """KeaException from lease6 fetch returns 400."""
        MockKeaClient.return_value.lease_get_by_ip.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        response = self.client.post(self._url(), {"ip_address": "2001:db8::1"})
        self.assertEqual(response.status_code, 400)

    @patch("netbox_kea.models.KeaClient")
    def test_empty_lease_returns_400(self, MockKeaClient):
        """When lease6 is None (not found), returns 400."""
        MockKeaClient.return_value.lease_get_by_ip.return_value = None
        response = self.client.post(self._url(), {"ip_address": "2001:db8::2"})
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# Coverage: Bulk reservation sync — v6 fetch exception path
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservation6SyncFetchFail(_ViewTestBase):
    """v6 bulk sync shows error when reservation fetch fails."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_v6_kea_exception_shows_error_and_redirects(self, mock_fetch):
        """KeaException during v6 reservation fetch shows error and redirects to v6 list."""
        mock_fetch.side_effect = KeaException(
            {"result": 1, "text": "host_cmds not loaded"},
            index=0,
        )
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("reservations6", response.url)

    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_v6_value_error_shows_generic_error(self, mock_fetch):
        """ValueError during v6 fetch shows generic error."""
        mock_fetch.side_effect = ValueError("bad data")
        response = self.client.post(self._url(), follow=True)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Failed to fetch" in m for m in msgs))
        self.assertFalse(any("bad data" in m for m in msgs))


# ---------------------------------------------------------------------------
# Coverage: Bulk sync per-row error isolation
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkSyncPerRowErrorIsolation(_ViewTestBase):
    """One sync failure in a bulk batch must not prevent other rows from processing."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_middle_row_fails_others_succeed(self, mock_fetch, mock_sync):
        """Row 1 succeeds, row 2 raises IntegrityError, row 3 succeeds."""
        from django.db import IntegrityError

        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:00:00:01"},
            {"ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:00:00:02"},
            {"ip-address": "10.0.0.3", "hw-address": "aa:bb:cc:00:00:03"},
        ]
        mock_sync.side_effect = [
            (MagicMock(), True),
            IntegrityError("duplicate key"),
            (MagicMock(), False),
        ]
        response = self.client.post(self._url(), follow=True)
        msgs = [str(m) for m in response.context["messages"]]
        # 1 created + 1 error + 1 updated
        self.assertTrue(any("1 created" in m and "1 updated" in m and "1 errors" in m for m in msgs))
        self.assertEqual(mock_sync.call_count, 3)

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_validation_error_counted_as_error(self, mock_fetch, mock_sync):
        """ValidationError from sync is counted as error."""
        from django.core.exceptions import ValidationError

        mock_fetch.return_value = [
            {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:00:00:01"},
        ]
        mock_sync.side_effect = ValidationError("invalid prefix")
        response = self.client.post(self._url(), follow=True)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("1 errors" in m for m in msgs))
        self.assertFalse(any("invalid prefix" in m for m in msgs))

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    @patch("netbox_kea.views.sync_views._fetch_reservations_from_server")
    def test_reservations_with_ip_addresses_field_processed(self, mock_fetch, mock_sync):
        """v6-style reservations with ip-addresses list (not ip-address) are processed."""
        mock_fetch.return_value = [
            {"ip-addresses": ["2001:db8::1"], "duid": "00:01:02:03"},
        ]
        mock_sync.return_value = (MagicMock(), True)
        response = self.client.post(self._url(), follow=True)
        mock_sync.assert_called_once()
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("1 created" in m for m in msgs))


# ---------------------------------------------------------------------------
# Coverage: Bulk reservation import — per-row exception types
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkImportPerRowExceptionTypes(_ViewTestBase):
    """Bulk import handles each per-row exception type independently."""

    @patch("netbox_kea.models.KeaClient")
    def test_requests_exception_per_row_recorded(self, MockKeaClient):
        """requests.RequestException per-row surfaced with connection error message."""
        import requests as req_lib

        MockKeaClient.return_value.reservation_add.side_effect = req_lib.Timeout("read timeout")
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["errors"], 1)
        self.assertIn("Connection error", result["error_rows"][0]["error"])
        self.assertNotIn("read timeout", result["error_rows"][0]["error"])

    @patch("netbox_kea.models.KeaClient")
    def test_duplicate_kea_exception_counted_as_skipped(self, MockKeaClient):
        """KeaException with 'already exist' text is counted as skipped, not error."""
        MockKeaClient.return_value.reservation_add.side_effect = KeaException(
            {"result": 1, "text": "Host already exists in subnet 1. Duplicate entry."},
            index=0,
        )
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        csv_content = "ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 0)

    @patch("netbox_kea.models.KeaClient")
    def test_mixed_success_skip_and_error_in_batch(self, MockKeaClient):
        """Multi-row batch: row 1 succeeds, row 2 duplicate skip, row 3 error."""
        call_count = {"n": 0}

        def side_effect(service, row):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise KeaException(
                    {"result": 1, "text": "Host already exists in subnet 1."},
                    index=0,
                )
            if call_count["n"] == 3:
                raise RuntimeError("unexpected")

        MockKeaClient.return_value.reservation_add.side_effect = side_effect
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        csv_content = (
            "ip-address,hw-address,subnet-id\n"
            "10.0.0.1,aa:bb:cc:00:00:01,1\n"
            "10.0.0.2,aa:bb:cc:00:00:02,1\n"
            "10.0.0.3,aa:bb:cc:00:00:03,1\n"
        )
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = "reservations.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["total"], 3)
