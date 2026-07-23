# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Sync / bulk-import view tests for the netbox_kea plugin.

Covers the views in ``netbox_kea/views/sync_views.py``: the per-row lease /
reservation sync endpoints, the bulk reservation sync, and the CSV bulk-import
views for reservations and leases.

These tests drive the **real** ``KeaClient``; only the HTTP boundary is stubbed
via ``kea_stub.stub_kea``, so the request payloads the views actually send to Kea
are exercised. The Kea command chains:

* **single lease sync** (``ServerLease{4,6}SyncView``): ``lease{v}-get`` to fetch
  the live lease, then the NetBox-side ``sync_lease_to_netbox``.
* **single reservation sync** (``ServerReservation{4,6}SyncView``): ``reservation_get_by_ip``
  = ``subnet{v}-list`` then ``reservation-get`` per candidate subnet, then
  ``sync_reservation_to_netbox``.
* **bulk reservation sync**: ``reservation-get-page`` (drained by ``iter_reservations``)
  then ``sync_reservation_to_netbox`` per record.
* **bulk import**: one ``reservation-add`` / ``lease{v}-add`` per CSV row.

The NetBox-side boundary is where mocks legitimately remain (documented per test):
``_sync`` / ``sync_reservation_to_netbox`` / ``sync_lease_to_netbox`` are patched to
inject IPAM outcomes (created vs. updated) and DB errors (IntegrityError,
OperationalError, ValidationError, …) that the real IPAM flow cannot produce
deterministically here; ``cleanup_stale_ips_batch`` is patched only where a specific
stale count is asserted. ``NbIP`` sync returns use ``MagicMock(spec=NbIP)``. No
``KeaClient`` is mocked — the Kea request path is always real.
"""

import io
from unittest.mock import MagicMock, patch

import requests
from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse
from ipam.models import IPAddress as NbIP

from .kea_stub import _res_get, _res_page, queued, stub_kea
from .utils import _PLUGINS_CONFIG, User, _ViewTestBase

# ---------------------------------------------------------------------------
# Kea response fixtures / builders
# ---------------------------------------------------------------------------


def _lease4(ip="10.0.0.1"):
    """A ``lease4-get`` payload (result 0) for a single live lease."""
    return {
        "result": 0,
        "arguments": {
            "ip-address": ip,
            "hw-address": "aa:bb:cc:00:00:01",
            "hostname": "realhost",
            "valid-lft": 86400,
            "cltt": 1700000000,
            "subnet-id": 1,
        },
    }


def _subnet_list(version, subnets):
    """A ``subnet{v}-list`` payload (used by ``reservation_get_by_ip`` to find candidate subnets)."""
    return {"result": 0, "arguments": {"subnets": list(subnets)}}


def _messages(response):
    """Read the messages queued on a (non-followed) response's request."""
    return [str(m) for m in get_messages(response.wsgi_request)]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncViewEdgeCases(_ViewTestBase):
    """ServerLease4SyncView POST edge cases: invalid IP, sync exception."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    def test_post_missing_ip_returns_400(self):
        """POST without ip_address must return 400 (no Kea traffic)."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), [])

    def test_post_invalid_ip_returns_400(self):
        """POST with invalid IP must return 400 (no Kea traffic)."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"ip_address": "not-an-ip"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), [])

    def test_post_sync_exception_returns_500(self):
        """POST where sync raises a concrete error must return 500 with generic message, not raw exception."""
        # Real lease fetch succeeds; the NetBox-side _sync raises (injected error).
        with (
            stub_kea({"lease4-get": _lease4("10.0.0.1")}),
            patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=ValueError("ip parse error")),
        ):
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
        """POST without ipam.add_ipaddress must return 403 (before any Kea traffic)."""
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
    """_BaseBulkReservationSyncView — fetch transport error shows error and redirects."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def test_post_fetch_exception_shows_error(self):
        """A transport error draining reservation-get-page must show error and redirect."""
        with stub_kea({"reservation-get-page": requests.ConnectionError("fetch fail")}):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        msgs = _messages(response)
        self.assertTrue(any("Failed to fetch" in m for m in msgs))
        self.assertFalse(any("fetch fail" in m for m in msgs))


# ---------------------------------------------------------------------------
# BulkReservationImport edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationImportEdgeCases(_ViewTestBase):
    """_BaseBulkReservationImportView POST: invalid form and CSV parse error."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

    def test_post_without_file_rerenders_form(self):
        """POST without a CSV file must re-render the form (200, no Kea)."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_post_invalid_csv_shows_error(self):
        """POST with a CSV that fails parse_reservation_csv raises ValueError → form error (no Kea)."""
        # CSV with missing required columns triggers ValueError in parse_reservation_csv,
        # before any Kea client is built.
        bad_csv = io.BytesIO(b"garbage_header\nrow1\n")
        bad_csv.name = "bad.csv"
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"csv_file": bad_csv})
        self.assertEqual(response.status_code, 200)
        # Response should include a form error about invalid CSV — message must be generic (no raw exception text)
        self.assertContains(response, "csv_file", msg_prefix="Expected CSV error in form")
        self.assertContains(response, "parsing failed", msg_prefix="Expected generic error message")
        self.assertEqual(kea.commands(), [])


# ---------------------------------------------------------------------------
# Bulk reservation sync — edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncEdgeCases(_ViewTestBase):
    """Bulk sync with missing IPs, errors, and count tracking (superuser has IPAM perms)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_reservation_without_ip_is_skipped(self, mock_sync):
        """Reservations without ip-address/ip-addresses are skipped (real fetch, no sync)."""
        with stub_kea({"reservation-get-page": _res_page([{"hw-address": "aa:bb:cc:dd:ee:ff"}])}):
            self.client.post(self._url())
        mock_sync.assert_not_called()

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_sync_creates_and_updates(self, mock_sync):
        """Created and updated counters incremented correctly."""
        hosts = [
            {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:01"},
            {"ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:dd:ee:02"},
        ]
        mock_sync.side_effect = [(MagicMock(spec=NbIP), True, True), (MagicMock(spec=NbIP), False, True)]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            response = self.client.post(self._url())
        msgs = _messages(response)
        self.assertIn("Bulk sync complete: 1 created, 1 updated.", msgs)
        self.assertEqual(mock_sync.call_count, 2)

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_sync_exception_counted_as_error(self, mock_sync):
        """Sync exception increments errors, warning shown."""
        hosts = [{"ip-address": "10.0.0.1"}, {"ip-address": "10.0.0.2"}]
        mock_sync.side_effect = [ValueError("db error"), (MagicMock(spec=NbIP), True, True)]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            response = self.client.post(self._url())
        msgs = _messages(response)
        self.assertIn("Bulk sync: 1 created, 0 updated, 1 errors.", msgs)
        self.assertEqual(mock_sync.call_count, 2)


# ---------------------------------------------------------------------------
# Reservation import — generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationImportGenericException(_ViewTestBase):
    """Generic exception during reservation_add is caught per-row."""

    def test_generic_exception_appended_to_errors(self):
        """A RuntimeError from reservation-add is caught and surfaced as an error row."""
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        csv_file = io.BytesIO(b"ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:dd:ee:ff,1")
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": RuntimeError("crash")}):
            response = self.client.post(url, {"csv_file": csv_file, "subnet_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["errors"], 1)
        self.assertEqual(response.context["result"]["error_rows"][0]["error"], "An unexpected error occurred.")


# ---------------------------------------------------------------------------
# _BaseSyncView._sync — NotImplementedError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBaseSyncViewNotImplemented(_ViewTestBase):
    """_BaseSyncView._sync raises NotImplementedError."""

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
    """ServerReservation4SyncView._fetch_live_data uses reservation_get_by_ip (subnet-list + reservation-get)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk])

    def test_uses_live_reservation_when_found(self):
        """When reservation_get_by_ip finds a reservation, that dict is passed to _sync."""
        live = {"ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:00:00:01", "hostname": "livehost"}
        stub = {
            "subnet4-list": _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}]),
            "reservation-get": _res_get(live),
        }
        with (
            stub_kea(stub) as kea,
            patch("netbox_kea.views.ServerReservation4SyncView._sync") as mock_sync,
        ):
            mock_sync.return_value = (MagicMock(spec=NbIP), True, True)
            self.client.post(self._url(), {"ip_address": "10.0.0.5", "hostname": "fallback"})

        mock_sync.assert_called_once()
        data = mock_sync.call_args[0][0]
        self.assertEqual(data["hostname"], "livehost")
        # reservation_get_by_ip(4, "10.0.0.5") → reservation-get scoped to the matching subnet + IP.
        self.assertEqual(kea.bodies("reservation-get")[0]["arguments"], {"subnet-id": 1, "ip-address": "10.0.0.5"})

    def test_reservation_not_found_returns_400_without_sync(self):
        """When reservation_get_by_ip returns None (reservation-get result 3), response is 400 (no sync)."""
        stub = {
            "subnet4-list": _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}]),
            "reservation-get": {"result": 3},
        }
        with stub_kea(stub), patch("netbox_kea.views.ServerReservation4SyncView._sync") as mock_sync:
            response = self.client.post(self._url(), {"ip_address": "10.0.0.5", "hostname": "fallback"})
        self.assertEqual(response.status_code, 400)
        mock_sync.assert_not_called()

    def test_falls_back_on_kea_exception(self):
        """When the subnet-list call fails (KeaException), response is 400."""
        with (
            stub_kea({"subnet4-list": {"result": 1, "text": "not found"}}),
            patch("netbox_kea.views.ServerReservation4SyncView._sync") as mock_sync,
        ):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.5", "hostname": "fallback"})
        self.assertEqual(response.status_code, 400)
        mock_sync.assert_not_called()


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6SyncViewFetchLiveData(_ViewTestBase):
    """ServerReservation6SyncView._fetch_live_data uses reservation_get_by_ip for v6."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])

    def test_calls_reservation_get_by_ip_with_version_6(self):
        """The v6 view lists subnets via subnet6-list; no match → 400."""
        with stub_kea({"subnet6-list": _subnet_list(6, [])}) as kea:
            response = self.client.post(self._url(), {"ip_address": "2001:db8::1", "hostname": ""})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.bodies("subnet6-list")[0]["service"], ["dhcp6"])

    def test_falls_back_on_request_exception(self):
        """When the subnet-list call raises a transport error, response is 400."""
        with stub_kea({"subnet6-list": requests.RequestException("timeout")}):
            response = self.client.post(self._url(), {"ip_address": "2001:db8::1", "hostname": "fallback6"})
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# TestFetchLiveDataNoSyntheticFallback
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLiveDataNoSyntheticFallback(_ViewTestBase):
    """_fetch_live_data must NOT mutate NetBox when Kea returns None or errors."""

    def test_kea_not_found_returns_400(self):
        """When Kea returns no lease (result 3), response is 400 (no sync)."""
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        with stub_kea({"lease4-get": {"result": 3}}):
            response = self.client.post(url, {"ip_address": "10.0.0.99"})
        self.assertEqual(response.status_code, 400)

    def test_kea_exception_returns_400(self):
        """When Kea raises an error (result 1), response is 400 (no sync)."""
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        with stub_kea({"lease4-get": {"result": 1, "text": "not found"}}):
            response = self.client.post(url, {"ip_address": "10.0.0.99"})
        self.assertEqual(response.status_code, 400)

    def test_kea_found_calls_sync(self):
        """When Kea returns a lease, _sync IS called."""
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        with (
            stub_kea({"lease4-get": _lease4("10.0.0.1")}),
            patch("netbox_kea.views.ServerLease4SyncView._sync") as mock_sync,
        ):
            mock_sync.return_value = (MagicMock(spec=NbIP), True, True)
            response = self.client.post(url, {"ip_address": "10.0.0.1"})
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()


# ---------------------------------------------------------------------------
# TestReservationImportBareExcept
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationImportBareExcept(_ViewTestBase):
    """Reservation import catches all per-row exceptions and surfaces them as error rows."""

    def test_attribute_error_surfaced_as_error_row(self):
        """An AttributeError from reservation-add is caught and surfaced as an error row."""
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        csv_file = io.BytesIO(b"ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:dd:ee:ff,1")
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": AttributeError("bug")}):
            response = self.client.post(url, {"csv_file": csv_file, "subnet_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["errors"], 1)
        self.assertEqual(response.context["result"]["error_rows"][0]["error"], "An unexpected error occurred.")


# ---------------------------------------------------------------------------
# TestLeaseImportBareExcept
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseImportBareExcept(_ViewTestBase):
    """Lease import catches specific per-row exceptions and surfaces them as error rows."""

    def test_attribute_error_is_row_error(self):
        """An AttributeError from lease4-add is caught per-row and surfaced as an error row."""
        url = reverse("plugins:netbox_kea:server_lease4_bulk_import", args=[self.server.pk])
        csv_content = b"ip-address,hw-address,hostname,valid-lft,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,host1,86400,1"
        csv_file = io.BytesIO(csv_content)
        csv_file.name = "leases.csv"
        with stub_kea({"lease4-add": AttributeError("bug")}):
            response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["errors"], 1)
        self.assertEqual(
            response.context["result"]["error_rows"][0]["error"],
            "An unexpected error occurred.",
        )


# ---------------------------------------------------------------------------
# TestImportLoopValueError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestImportLoopValueError(_ViewTestBase):
    """Import loops must handle ValueError from the Kea client as a per-row error."""

    def test_reservation_import_value_error_is_row_error(self):
        """ValueError from reservation-add must be a row error, not abort the import."""
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])
        csv_content = b"ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n10.0.0.2,aa:bb:cc:00:00:02,1\n"
        csv_file = io.BytesIO(csv_content)
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": queued(ValueError("bad JSON from Kea"), {"result": 0})}) as kea:
            response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(kea.bodies("reservation-add")), 2)
        result = response.context["result"]
        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(result["error_rows"]), 1)
        self.assertIn("Invalid response from Kea", result["error_rows"][0]["error"])
        self.assertNotIn("bad JSON from Kea", result["error_rows"][0]["error"])

    def test_lease_import_value_error_is_row_error(self):
        """ValueError from lease4-add must be a row error, not abort the import."""
        url = reverse("plugins:netbox_kea:server_lease4_bulk_import", args=[self.server.pk])
        csv_file = io.BytesIO(b"ip-address\n10.0.0.1\n10.0.0.2\n")
        csv_file.name = "leases.csv"
        with stub_kea({"lease4-add": queued(ValueError("bad JSON from Kea"), {"result": 0})}) as kea:
            response = self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(kea.bodies("lease4-add")), 2)
        result = response.context["result"]
        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(result["error_rows"]), 1)
        self.assertIn("Invalid response from Kea", result["error_rows"][0]["error"])
        self.assertNotIn("bad JSON from Kea", result["error_rows"][0]["error"])


# ---------------------------------------------------------------------------
# TestBulkReservationSyncExceptNarrowing
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncExceptNarrowing(_ViewTestBase):
    """AttributeError while fetching reservations propagates (programming bug, not swallowed)."""

    def test_attribute_error_propagates(self):
        """An AttributeError raised while draining reservation-get-page propagates."""
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])
        with stub_kea({"reservation-get-page": AttributeError("programming bug")}):
            with self.assertRaises(AttributeError):
                self.client.post(url)


# ---------------------------------------------------------------------------
# TestBulkSyncBatchCleanup
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkSyncBatchCleanup(_ViewTestBase):
    """Bulk sync defers stale-IP cleanup to a single batch pass."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_bulk_sync_calls_sync_with_cleanup_false(self, mock_sync, mock_batch):
        """Each record is synced with cleanup=False; batch cleanup runs after."""
        hosts = [
            {"ip-address": "10.0.0.1", "hostname": "h1.example.com"},
            {"ip-address": "10.0.0.2", "hostname": "h1.example.com"},
        ]
        mock_sync.side_effect = [(MagicMock(spec=NbIP), True, True), (MagicMock(spec=NbIP), False, True)]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())
        # Both calls must use cleanup=False
        for call in mock_sync.call_args_list:
            self.assertEqual(call.kwargs.get("cleanup"), False)
        # Batch cleanup called once with both records
        mock_batch.assert_called_once()
        synced_records = mock_batch.call_args[0][0]
        self.assertEqual(len(synced_records), 2)

    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=3)
    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_stale_cleaned_count_appears_in_message(self, mock_sync, mock_batch):
        """When batch cleanup removes IPs, the count appears in the success message."""
        hosts = [{"ip-address": "10.0.0.1", "hostname": "h.example.com"}]
        mock_sync.return_value = (MagicMock(spec=NbIP), True, True)
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            response = self.client.post(self._url())
        self.assertTrue(any("3 stale cleaned" in m for m in _messages(response)))

    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_batch_cleanup_skipped_when_errors(self, mock_sync, mock_batch):
        """When sync errors occur, batch cleanup is skipped entirely (incomplete keep-set)."""
        hosts = [{"ip-address": "10.0.0.1", "hostname": "h1"}, {"ip-address": "10.0.0.2", "hostname": "h2"}]
        mock_sync.side_effect = [ValueError("db error"), (MagicMock(spec=NbIP), True, True)]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())
        mock_batch.assert_not_called()


# ---------------------------------------------------------------------------
# _BaseSyncView.post() — DB error during sync
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBaseSyncViewDBError(_ViewTestBase):
    """_BaseSyncView.post() handles DB errors from _sync gracefully."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    def _post_with_sync_error(self, ip, exc):
        with (
            stub_kea({"lease4-get": _lease4(ip)}),
            patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=exc),
        ):
            return self.client.post(self._url(), {"ip_address": ip})

    def test_integrity_error_returns_500(self):
        """IntegrityError from _sync returns 500 with generic message."""
        from django.db import IntegrityError

        response = self._post_with_sync_error("10.0.0.1", IntegrityError("duplicate key"))
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn("Sync error", body)
        self.assertNotIn("duplicate key", body)

    def test_operational_error_returns_500(self):
        """OperationalError from _sync returns 500 with generic message."""
        from django.db.utils import OperationalError

        response = self._post_with_sync_error("10.0.0.2", OperationalError("db conn failed"))
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn("Sync error", body)
        self.assertNotIn("db conn failed", body)

    def test_programming_error_returns_500(self):
        """ProgrammingError from _sync returns 500."""
        from django.db.utils import ProgrammingError

        response = self._post_with_sync_error("10.0.0.3", ProgrammingError("bad query"))
        self.assertEqual(response.status_code, 500)

    def test_validation_error_returns_500(self):
        """ValidationError from _sync returns 500."""
        from django.core.exceptions import ValidationError

        response = self._post_with_sync_error("10.0.0.4", ValidationError("invalid data"))
        self.assertEqual(response.status_code, 500)


# ---------------------------------------------------------------------------
# Bulk reservation sync fetch failure
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservationSyncFetchFailure(_ViewTestBase):
    """Bulk sync shows error when the reservation fetch fails."""

    def _url_v4(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def _url_v6(self):
        return reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[self.server.pk])

    def test_kea_exception_on_fetch_shows_error_and_redirects(self):
        """KeaException during fetch shows a hint and redirects; raw Kea text must not leak."""
        with stub_kea({"reservation-get-page": {"result": 1, "text": "hook not loaded"}}):
            response = self.client.post(self._url_v4())
        self.assertEqual(response.status_code, 302)
        msgs = _messages(response)
        # kea_error_hint maps result=1 to a generic message — raw Kea text must not leak
        self.assertTrue(any("Kea reported an error" in m for m in msgs))
        self.assertFalse(any("hook not loaded" in m.lower() for m in msgs))

    def test_type_error_on_fetch_shows_generic_error(self):
        """A TypeError raised while draining reservation-get-page shows a generic error."""
        with stub_kea({"reservation-get-page": TypeError("unexpected type")}):
            response = self.client.post(self._url_v4())
        msgs = _messages(response)
        self.assertTrue(any("Failed to fetch" in m for m in msgs))
        self.assertFalse(any("unexpected type" in m for m in msgs))

    def test_v6_kea_exception_on_fetch_redirects_to_v6_list(self):
        """A v6 KeaException (unknown command) redirects to the v6 reservation list."""
        with stub_kea({"reservation-get-page": {"result": 2, "text": "unknown command"}}):
            response = self.client.post(self._url_v6())
        self.assertEqual(response.status_code, 302)
        self.assertIn("reservations6", response.url)


# ---------------------------------------------------------------------------
# Per-row error isolation in bulk import
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkImportPerRowErrorIsolation(_ViewTestBase):
    """If one reservation in a batch raises, remaining items still process."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

    def test_one_row_fails_others_succeed(self):
        """First row raises KeaException, second succeeds, third raises ValueError."""
        responses = queued(
            {"result": 1, "text": "conflict"},  # row 1 → KeaException (not a duplicate) → error
            {"result": 0},  # row 2 → created
            ValueError("bad response"),  # row 3 → error
        )
        csv_content = (
            b"ip-address,hw-address,subnet-id\n"
            b"10.0.0.1,aa:bb:cc:00:00:01,1\n"
            b"10.0.0.2,aa:bb:cc:00:00:02,1\n"
            b"10.0.0.3,aa:bb:cc:00:00:03,1\n"
        )
        csv_file = io.BytesIO(csv_content)
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": responses}) as kea:
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], 2)
        self.assertEqual(len(kea.bodies("reservation-add")), 3)

    def test_already_exists_counted_as_skipped(self):
        """KeaException with result=1 and 'already exist' text is counted as skipped."""
        csv_file = io.BytesIO(b"ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n")
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": {"result": 1, "text": "Host already exists in subnet 1."}}):
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 0)

    def test_connection_error_per_row(self):
        """requests.RequestException per-row is caught and recorded."""
        csv_file = io.BytesIO(b"ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n")
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": requests.ConnectionError("timeout")}):
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["errors"], 1)
        self.assertIn("Connection error", result["error_rows"][0]["error"])


# ---------------------------------------------------------------------------
# Reservation sync view for v6 with IntegrityError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6SyncDBError(_ViewTestBase):
    """ServerReservation6SyncView handles DB errors from sync."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])

    def test_integrity_error_returns_500(self):
        """IntegrityError from _sync returns 500."""
        from django.db import IntegrityError

        stub = {
            "subnet6-list": _subnet_list(6, [{"id": 1, "subnet": "2001:db8::/64"}]),
            "reservation-get": _res_get(
                {"ip-addresses": ["2001:db8::1"], "duid": "00:01:02:03:04:05", "hostname": "host6", "subnet-id": 1}
            ),
        }
        with (
            stub_kea(stub),
            patch("netbox_kea.views.ServerReservation6SyncView._sync", side_effect=IntegrityError("dup")),
        ):
            response = self.client.post(self._url(), {"ip_address": "2001:db8::1"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"dup", response.content)


# ---------------------------------------------------------------------------
# _BaseSyncView.post() — OperationalError during sync
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncViewOperationalError(_ViewTestBase):
    """_BaseSyncView.post() handles OperationalError from _sync."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    def test_operational_error_from_sync_returns_500(self):
        """OperationalError during sync returns 500 with generic message."""
        from django.db.utils import OperationalError

        with (
            stub_kea({"lease4-get": _lease4("10.0.0.10")}),
            patch("netbox_kea.views.ServerLease4SyncView._sync", side_effect=OperationalError("conn lost")),
        ):
            response = self.client.post(self._url(), {"ip_address": "10.0.0.10"})
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn("Sync error", body)
        self.assertNotIn("conn lost", body)


# ---------------------------------------------------------------------------
# Lease6 sync view — live fetch failure
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease6SyncViewFetchFailure(_ViewTestBase):
    """ServerLease6SyncView._fetch_live_data returns None on failure → 400."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease6_sync", args=[self.server.pk])

    def test_kea_exception_returns_400(self):
        """KeaException from the lease6 fetch returns 400."""
        with stub_kea({"lease6-get": {"result": 1, "text": "not found"}}):
            response = self.client.post(self._url(), {"ip_address": "2001:db8::1"})
        self.assertEqual(response.status_code, 400)

    def test_empty_lease_returns_400(self):
        """When lease6 is not found (result 3), returns 400."""
        with stub_kea({"lease6-get": {"result": 3}}):
            response = self.client.post(self._url(), {"ip_address": "2001:db8::2"})
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# Bulk reservation sync — v6 fetch exception path
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkReservation6SyncFetchFail(_ViewTestBase):
    """v6 bulk sync shows error when the reservation fetch fails."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[self.server.pk])

    def test_v6_kea_exception_shows_error_and_redirects(self):
        """KeaException during v6 reservation fetch shows error and redirects to the v6 list."""
        with stub_kea({"reservation-get-page": {"result": 1, "text": "host_cmds not loaded"}}):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("reservations6", response.url)

    def test_v6_value_error_shows_generic_error(self):
        """ValueError during v6 fetch shows a generic error."""
        with stub_kea({"reservation-get-page": ValueError("bad data")}):
            response = self.client.post(self._url())
        msgs = _messages(response)
        self.assertTrue(any("Failed to fetch" in m for m in msgs))
        self.assertFalse(any("bad data" in m for m in msgs))


# ---------------------------------------------------------------------------
# Bulk sync per-row error isolation
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkSyncPerRowErrorIsolation(_ViewTestBase):
    """One sync failure in a bulk batch must not prevent other rows from processing."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_middle_row_fails_others_succeed(self, mock_sync):
        """Row 1 succeeds, row 2 raises IntegrityError, row 3 succeeds."""
        from django.db import IntegrityError

        hosts = [
            {"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:00:00:01"},
            {"ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:00:00:02"},
            {"ip-address": "10.0.0.3", "hw-address": "aa:bb:cc:00:00:03"},
        ]
        mock_sync.side_effect = [
            (MagicMock(spec=NbIP), True, True),
            IntegrityError("duplicate key"),
            (MagicMock(spec=NbIP), False, True),
        ]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            response = self.client.post(self._url())
        msgs = _messages(response)
        # 1 created + 1 error + 1 updated
        self.assertTrue(any("1 created" in m and "1 updated" in m and "1 errors" in m for m in msgs))
        self.assertEqual(mock_sync.call_count, 3)

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_validation_error_counted_as_error(self, mock_sync):
        """ValidationError from sync is counted as an error."""
        from django.core.exceptions import ValidationError

        hosts = [{"ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:00:00:01"}]
        mock_sync.side_effect = ValidationError("invalid prefix")
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            response = self.client.post(self._url())
        msgs = _messages(response)
        self.assertTrue(any("1 errors" in m for m in msgs))
        self.assertFalse(any("invalid prefix" in m for m in msgs))

    @patch("netbox_kea.sync.sync_reservation_to_netbox")
    def test_reservations_with_ip_addresses_field_processed(self, mock_sync):
        """v6-style reservations with ip-addresses list (not ip-address) are processed."""
        hosts = [{"ip-addresses": ["2001:db8::1"], "duid": "00:01:02:03"}]
        mock_sync.return_value = (MagicMock(spec=NbIP), True, True)
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            response = self.client.post(self._url())
        mock_sync.assert_called_once()
        self.assertTrue(any("1 created" in m for m in _messages(response)))


# ---------------------------------------------------------------------------
# Bulk reservation import — per-row exception types
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkImportPerRowExceptionTypes(_ViewTestBase):
    """Bulk import handles each per-row exception type independently."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

    def test_requests_exception_per_row_recorded(self):
        """requests.RequestException per-row surfaced with a connection-error message."""
        csv_file = io.BytesIO(b"ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n")
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": requests.Timeout("read timeout")}):
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["errors"], 1)
        self.assertIn("Connection error", result["error_rows"][0]["error"])
        self.assertNotIn("read timeout", result["error_rows"][0]["error"])

    def test_duplicate_kea_exception_counted_as_skipped(self):
        """KeaException with 'already exist' text is counted as skipped, not error."""
        csv_file = io.BytesIO(b"ip-address,hw-address,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,1\n")
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": {"result": 1, "text": "Host already exists in subnet 1. Duplicate entry."}}):
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 0)

    def test_mixed_success_skip_and_error_in_batch(self):
        """Multi-row batch: row 1 succeeds, row 2 duplicate skip, row 3 error."""
        responses = queued(
            {"result": 0},  # row 1 → created
            {"result": 1, "text": "Host already exists in subnet 1."},  # row 2 → skipped
            RuntimeError("unexpected"),  # row 3 → error
        )
        csv_content = (
            b"ip-address,hw-address,subnet-id\n"
            b"10.0.0.1,aa:bb:cc:00:00:01,1\n"
            b"10.0.0.2,aa:bb:cc:00:00:02,1\n"
            b"10.0.0.3,aa:bb:cc:00:00:03,1\n"
        )
        csv_file = io.BytesIO(csv_content)
        csv_file.name = "reservations.csv"
        with stub_kea({"reservation-add": responses}):
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["total"], 3)
