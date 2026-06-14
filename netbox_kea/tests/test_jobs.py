# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for netbox_kea/jobs.py — KeaIpamSyncJob.

Design principle
----------------
``TestKeaIpamSyncJobRun`` and ``TestKeaIpamSyncJobKillSwitches`` use the *real*
Django ORM and the real ``sync_*`` helpers.  Only the three Kea HTTP methods
(``command``, ``lease_get_all``, ``reservation_get_page``) are patched — those
are the true external boundary (a third-party network service we cannot run in
unit tests).  Asserting against real ``IPAddress`` rows means a
``MagicMock``-green test cannot mask a broken production code path.

Helper/dispatcher tests (``TestSyncSubnetEntry``, ``TestFetchKeaSubnets``, …)
remain as ``SimpleTestCase`` because they test a single function's stats
accounting or JSON-parsing logic, and patching their immediate callees is the
appropriate seam.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from core.exceptions import JobFailed
from django.test import SimpleTestCase, TestCase, override_settings

from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.kea import KeaClient, KeaException

_PLUGINS_CONFIG = {
    "netbox_kea": {
        "kea_timeout": 30,
        "stale_ip_cleanup": "none",
        "sync_interval_minutes": 5,
        "sync_leases_enabled": True,
        "sync_reservations_enabled": True,
        "sync_max_leases_per_server": 50000,
    }
}

# Variant used by tests that exercise the stale-IP cleanup path.
_PLUGINS_CONFIG_CLEANUP = {"netbox_kea": {**_PLUGINS_CONFIG["netbox_kea"], "stale_ip_cleanup": "remove"}}

_LEASE4 = {
    "ip-address": "10.0.0.1",
    "hw-address": "aa:bb:cc:dd:ee:ff",
    "hostname": "host1",
    "cltt": 0,
    "valid-lft": 3600,
    "subnet-id": 1,
    "state": 0,
}
_LEASE6 = {
    "ip-address": "2001:db8::1",
    "duid": "00:01:02:03",
    "hostname": "host2",
    "cltt": 0,
    "valid-lft": 3600,
    "subnet-id": 1,
    "state": 0,
}
_RESV4 = {"ip-address": "10.0.0.100", "hw-address": "11:22:33:44:55:66", "hostname": "reserved1", "subnet-id": 1}


def _make_job() -> MagicMock:
    """Create a minimal mock Job object for JobRunner.__init__."""
    mock_job = MagicMock()
    mock_job.data = {}
    mock_job.log = MagicMock()
    return mock_job


def _make_server(name: str = "kea1", dhcp4: bool = True, dhcp6: bool = False, pk: int = 1) -> MagicMock:
    """Return a MagicMock Server — used only by SimpleTestCase helper tests."""
    server = MagicMock()
    server.name = name
    server.dhcp4 = dhcp4
    server.dhcp6 = dhcp6
    server.pk = pk
    server.sync_enabled = True
    server.sync_leases_enabled = True
    server.sync_reservations_enabled = True
    server.sync_prefixes_enabled = True
    server.sync_ip_ranges_enabled = True
    server.sync_vrf = None
    return server


@contextmanager
def _patch_kea(
    *,
    leases4: list[dict] | None = None,
    leases6: list[dict] | None = None,
    reservations: list[dict] | None = None,
    truncated: bool = False,
):
    """Patch only the Kea HTTP layer; all real ORM and sync code runs normally.

    Replaces three ``KeaClient`` instance methods with deterministic fakes:

    * ``command``              — returns an empty-subnet ``config-get`` response.
    * ``lease_get_all``        — returns *leases4* / *leases6* with *truncated* flag.
    * ``reservation_get_page`` — returns *reservations* in a single page (no pagination).

    Because the patches target the *class*, every ``KeaClient`` instance created
    inside ``server.get_client()`` during the test uses the same fakes.
    """

    def _fake_command(self, cmd, service=None, arguments=None, check=None):
        svc = (service or ["dhcp4"])[0]
        if svc == "dhcp6":
            return [{"result": 0, "arguments": {"Dhcp6": {"subnet6": [], "shared-networks": []}}}]
        return [{"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}]

    def _fake_lease_get_all(self, version=4, *, per_page=250, max_leases=None):
        if version == 6:
            return (list(leases6 or []), truncated)
        return (list(leases4 or []), truncated)

    def _fake_reservation_get_page(self, service, source_index=0, from_index=0, limit=100):
        return (list(reservations or []), 0, 0)

    with (
        patch.object(KeaClient, "command", _fake_command),
        patch.object(KeaClient, "lease_get_all", _fake_lease_get_all),
        patch.object(KeaClient, "reservation_get_page", _fake_reservation_get_page),
    ):
        yield


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaIpamSyncJobRun(TestCase):
    """Integration tests for KeaIpamSyncJob.run() against the real ORM.

    Only the three Kea HTTP methods are mocked (``command``, ``lease_get_all``,
    ``reservation_get_page``).  Everything else — ``sync_lease_to_netbox``,
    ``sync_reservation_to_netbox``, ``cleanup_stale_ips_batch``, ORM queries —
    executes against the real test database.

    Why this matters: a ``MagicMock`` auto-synthesises every attribute access,
    so mock-heavy tests stay green while the real code path is broken.  Asserting
    against actual ``IPAddress`` rows catches real bugs.
    """

    # ── scaffolding ──────────────────────────────────────────────────────────

    def _run(self) -> MagicMock:
        """Run the job, swallowing JobFailed; return the mock job object."""
        job = _make_job()
        try:
            KeaIpamSyncJob(job).run()
        except JobFailed:
            pass
        return job

    def _run_raises(self) -> None:
        """Run the job and assert that JobFailed is raised."""
        with self.assertRaises(JobFailed):
            KeaIpamSyncJob(_make_job()).run()

    def _make_db_server(self, **kwargs):
        from netbox_kea.tests.utils import _make_db_server

        return _make_db_server(**kwargs)

    # ── basic lease sync ──────────────────────────────────────────────────────

    def test_creates_ip_from_lease(self):
        """Lease sync creates an IPAddress row in the real DB."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())

    def test_lease_ip_status_is_dhcp(self):
        """Synced lease IP has status='dhcp'."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        ip = IPAddress.objects.filter(address__startswith="10.0.0.1/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "dhcp")

    def test_creates_v6_ip_from_lease(self):
        """DHCPv6 lease creates an IPv6 IPAddress row."""
        self._make_db_server(dhcp4=False, dhcp6=True)
        with _patch_kea(leases6=[_LEASE6]):
            self._run()
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="2001:db8::1/").exists())

    def test_dual_protocol_creates_both_v4_and_v6(self):
        """Server with dhcp4=True and dhcp6=True creates both address families."""
        self._make_db_server(dhcp4=True, dhcp6=True)
        with _patch_kea(leases4=[_LEASE4], leases6=[_LEASE6]):
            self._run()
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())
        self.assertTrue(IPAddress.objects.filter(address__startswith="2001:db8::1/").exists())

    # ── reservation sync ──────────────────────────────────────────────────────

    def test_creates_reserved_ip_from_reservation(self):
        """Reservation sync creates an IPAddress with status='reserved'."""
        from netbox_kea.models import SyncConfig

        SyncConfig.objects.create(
            pk=1,
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=True,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
            backfill_applied=True,
        )
        self._make_db_server(sync_leases_enabled=False)
        with _patch_kea(reservations=[_RESV4]):
            self._run()
        from ipam.models import IPAddress

        ip = IPAddress.objects.filter(address__startswith="10.0.0.100/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_both_lease_and_reservation_synced(self):
        """Both lease and reservation IPs are persisted in the same run."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4], reservations=[_RESV4]):
            self._run()
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())
        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.100/").exists())

    # ── disabled flags → no DB writes ────────────────────────────────────────

    def test_skips_leases_when_sync_leases_disabled(self):
        """sync_leases_enabled=False → no IPAddress from lease created."""
        from netbox_kea.models import SyncConfig

        SyncConfig.objects.create(
            pk=1,
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
            backfill_applied=True,
        )
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertFalse(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())

    def test_skips_reservations_when_sync_reservations_disabled(self):
        """sync_reservations_enabled=False → no reserved IPAddress created."""
        from netbox_kea.models import SyncConfig

        SyncConfig.objects.create(
            pk=1,
            interval_minutes=5,
            sync_leases_enabled=True,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
            backfill_applied=True,
        )
        self._make_db_server()
        with _patch_kea(leases4=[], reservations=[_RESV4]):
            self._run()
        from ipam.models import IPAddress

        self.assertFalse(IPAddress.objects.filter(address__startswith="10.0.0.100/").exists())

    def test_no_servers_is_no_op(self):
        """No servers in DB → no IPAddress rows created."""
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertEqual(IPAddress.objects.count(), 0)

    # ── error isolation ────────────────────────────────────────────────────

    def test_isolates_per_server_errors(self):
        """ValueError on server1.get_client() does not block server2 from syncing.

        A cert path supplied without a matching key causes ``KeaClient.__init__``
        to raise ``ValueError``.  The job catches it for server1, increments the
        error counter (triggering ``JobFailed``), and continues with server2.
        """
        # cert_path without key_path → KeaClient.__init__ raises ValueError
        self._make_db_server(name="server1", client_cert_path="/cert.pem")
        self._make_db_server(name="server2")
        with _patch_kea(leases4=[_LEASE4]):
            self._run_raises()  # errors > 0 → JobFailed
        from ipam.models import IPAddress

        # server2's lease was still synced despite server1 failing.
        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())

    def test_per_lease_error_does_not_abort_batch(self):
        """An invalid lease does not stop the rest of the batch from syncing.

        "not-an-ip" causes ``netaddr.AddrFormatError`` inside
        ``sync_lease_to_netbox``.  The job catches it, increments errors, and
        continues.  The valid lease's IP must still be created.
        """
        self._make_db_server()
        bad_lease = {**_LEASE4, "ip-address": "not-an-ip"}
        good_lease = {**_LEASE4, "ip-address": "10.0.0.2"}
        with _patch_kea(leases4=[bad_lease, good_lease]):
            self._run_raises()  # errors > 0 → JobFailed
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.2/").exists())

    # ── idempotency ────────────────────────────────────────────────────────

    def test_second_sync_does_not_create_duplicate(self):
        """Syncing the same lease twice creates exactly one IPAddress row."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertEqual(IPAddress.objects.filter(address__startswith="10.0.0.1/").count(), 1)

    def test_second_sync_updates_existing_ip_dns_name(self):
        """A subsequent sync with a new hostname updates dns_name in place."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        updated_lease = {**_LEASE4, "hostname": "updated-hostname.example.com"}
        with _patch_kea(leases4=[updated_lease]):
            self._run()
        from ipam.models import IPAddress

        ip = IPAddress.objects.filter(address__startswith="10.0.0.1/").first()
        self.assertEqual(ip.dns_name, "updated-hostname.example.com")

    # ── truncation ─────────────────────────────────────────────────────────

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
    def test_truncation_warning_skips_cleanup(self):
        """Truncated lease fetch → warning logged and stale IP not removed."""
        from ipam.models import IPAddress

        self._make_db_server()
        stale = IPAddress.objects.create(
            address="10.0.0.99/32",
            status="dhcp",
            dns_name="host1",
            description="Synced from Kea DHCP (dhcp)",
        )
        with _patch_kea(leases4=[_LEASE4], truncated=True):
            with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
                self._run()
        self.assertTrue(any("truncated" in msg for msg in cm.output))
        # Cleanup must be skipped when fetch was truncated.
        self.assertTrue(IPAddress.objects.filter(pk=stale.pk).exists())

    # ── host_cmds hook absent ─────────────────────────────────────────────

    def test_host_cmds_absent_warning_logged(self):
        """KeaException(result=2) from reservation_get_page → WARNING about host_cmds."""
        self._make_db_server()

        def _absent(self, service, source_index=0, from_index=0, limit=100):
            raise KeaException({"result": 2, "text": "unknown command"})

        with _patch_kea(leases4=[_LEASE4]):
            with patch.object(KeaClient, "reservation_get_page", _absent):
                with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
                    self._run()
        self.assertTrue(any("host_cmds" in msg for msg in cm.output))

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
    def test_cleanup_skipped_when_host_cmds_absent(self):
        """Reservation phase skipped (host_cmds absent) → cleanup_safe=False → stale IP preserved."""
        from ipam.models import IPAddress

        self._make_db_server()
        stale = IPAddress.objects.create(
            address="10.0.0.99/32",
            status="dhcp",
            dns_name="host1",
            description="Synced from Kea DHCP (dhcp)",
        )

        def _absent(self, service, source_index=0, from_index=0, limit=100):
            raise KeaException({"result": 2, "text": "unknown command"})

        with _patch_kea(leases4=[_LEASE4]):
            with patch.object(KeaClient, "reservation_get_page", _absent):
                with self.assertLogs("netbox_kea.jobs", level="WARNING"):
                    self._run()
        self.assertTrue(IPAddress.objects.filter(pk=stale.pk).exists())

    # ── stale-IP cleanup ──────────────────────────────────────────────────

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
    def test_stale_ip_removed_after_successful_sync(self):
        """A Kea-managed IP with the same hostname but a different address is deleted."""
        from ipam.models import IPAddress

        self._make_db_server()
        stale = IPAddress.objects.create(
            address="10.0.0.99/32",
            status="dhcp",
            dns_name="host1",
            description="Synced from Kea DHCP (dhcp)",
        )
        with _patch_kea(leases4=[_LEASE4], reservations=[]):
            self._run()
        self.assertFalse(IPAddress.objects.filter(pk=stale.pk).exists())

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
    def test_stale_ip_preserved_when_sync_errors_occurred(self):
        """Partial lease-sync failure → cleanup skipped → stale IP not touched."""
        from ipam.models import IPAddress

        self._make_db_server()
        stale = IPAddress.objects.create(
            address="10.0.0.99/32",
            status="dhcp",
            dns_name="host1",
            description="Synced from Kea DHCP (dhcp)",
        )
        bad_lease = {**_LEASE4, "ip-address": "not-an-ip"}
        with _patch_kea(leases4=[bad_lease]):
            self._run_raises()
        self.assertTrue(IPAddress.objects.filter(pk=stale.pk).exists())

    # ── per-server sync_enabled toggle ────────────────────────────────────

    def test_per_server_sync_enabled_false_skips_server(self):
        """Server with sync_enabled=False is not synced in a scheduled run."""
        self._make_db_server(sync_enabled=False)
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertFalse(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())

    # ── reservation pagination ────────────────────────────────────────────

    def test_reservation_pagination_fetches_all_pages(self):
        """Multi-page reservation responses are fully iterated and all rows synced."""
        from ipam.models import IPAddress

        self._make_db_server()
        resv1 = {**_RESV4, "ip-address": "10.0.0.100"}
        resv2 = {**_RESV4, "ip-address": "10.0.0.101", "hw-address": "22:33:44:55:66:77"}

        call_count = {"n": 0}

        def _paged(self, service, source_index=0, from_index=0, limit=100):
            call_count["n"] += 1
            # First two calls are pre-fetch (page 1 then page 2).
            # Next two calls are main sync (same pages again).
            if call_count["n"] in (1, 3):
                return ([resv1], 1, 0)  # non-zero next_from → continue
            return ([resv2], 0, 0)  # done

        with _patch_kea(leases4=[]):
            with patch.object(KeaClient, "reservation_get_page", _paged):
                self._run()

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.100/").exists())
        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.101/").exists())

    # ── max_leases config validation ──────────────────────────────────────

    @override_settings(
        PLUGINS_CONFIG={
            **_PLUGINS_CONFIG,
            "netbox_kea": {**_PLUGINS_CONFIG["netbox_kea"], "sync_max_leases_per_server": "not-a-number"},
        }
    )
    def test_invalid_max_leases_string_falls_back_to_default(self):
        """Non-integer sync_max_leases_per_server → warning logged, sync continues."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            with self.assertLogs("netbox.jobs", level="WARNING") as cm:
                self._run()
        self.assertTrue(any("Invalid sync_max_leases_per_server" in msg for msg in cm.output))
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())

    @override_settings(
        PLUGINS_CONFIG={
            **_PLUGINS_CONFIG,
            "netbox_kea": {**_PLUGINS_CONFIG["netbox_kea"], "sync_max_leases_per_server": -1},
        }
    )
    def test_negative_max_leases_resets_to_zero(self):
        """Negative sync_max_leases_per_server → warning logged, sync continues."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            with self.assertLogs("netbox.jobs", level="WARNING") as cm:
                self._run()
        self.assertTrue(any("Negative sync_max_leases_per_server" in msg for msg in cm.output))
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())

    # ── reservation KeaException (non-result-2) ───────────────────────────

    def test_reservation_kea_error_increments_errors_and_raises_job_failed(self):
        """KeaException(result=1) from reservation_get_page → errors++ → JobFailed."""
        self._make_db_server()

        def _err(self, service, source_index=0, from_index=0, limit=100):
            raise KeaException({"result": 1, "text": "internal error"})

        with _patch_kea(leases4=[_LEASE4]):
            with patch.object(KeaClient, "reservation_get_page", _err):
                with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
                    self._run_raises()
        self.assertTrue(any("Failed to fetch reservations" in msg for msg in cm.output))

    # ── per-reservation sync exception ───────────────────────────────────

    def test_per_reservation_error_does_not_abort_batch(self):
        """An unparseable reservation does not stop other reservations from syncing."""
        from ipam.models import IPAddress

        self._make_db_server()
        bad_resv = {**_RESV4, "ip-address": "not-an-ip"}
        good_resv = {**_RESV4, "ip-address": "10.0.0.102", "hw-address": "33:44:55:66:77:88"}
        with _patch_kea(leases4=[], reservations=[bad_resv, good_resv]):
            self._run_raises()  # errors > 0 → JobFailed
        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.102/").exists())

    # ── job metadata ─────────────────────────────────────────────────────

    def test_job_data_summary_written_after_run(self):
        """Per-server stats are persisted to job.data['summary'] after a run."""
        self._make_db_server(name="kea-prod")
        with _patch_kea(leases4=[_LEASE4]):
            mock_job = _make_job()
            KeaIpamSyncJob(mock_job).run()
        self.assertIn("summary", mock_job.data)
        entry = mock_job.data["summary"][0]
        self.assertEqual(entry["name"], "kea-prod")
        self.assertEqual(entry["created"], 1)
        self.assertEqual(entry["errors"], 0)
        mock_job.save.assert_called_once_with(update_fields=["data"])

    # ── reservation generic exception ─────────────────────────────────────

    def test_reservation_generic_exception_increments_errors(self):
        """RuntimeError from reservation_get_page → warning logged → JobFailed."""
        self._make_db_server()

        def _runtime_err(self, service, source_index=0, from_index=0, limit=100):
            raise RuntimeError("unexpected")

        with _patch_kea(leases4=[_LEASE4]):
            with patch.object(KeaClient, "reservation_get_page", _runtime_err):
                with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
                    self._run_raises()
        self.assertTrue(any("Unexpected error fetching reservations" in msg for msg in cm.output))

    # ── unhandled exception in _sync_one_server ────────────────────────

    def test_unhandled_exception_in_sync_one_server_is_caught(self):
        """An unhandled exception inside _sync_one_server is caught by the outer loop.

        Patching ``cleanup_stale_ips_batch`` to raise is the only way to
        trigger this path: the real function returns early when
        ``stale_ip_cleanup='none'``, so we use ``stale_ip_cleanup='remove'``
        and inject a RuntimeError there.
        """
        self._make_db_server()
        with override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP):
            with _patch_kea(leases4=[_LEASE4], reservations=[]):
                with patch("netbox_kea.sync.cleanup_stale_ips_batch", side_effect=RuntimeError("db gone")):
                    with self.assertLogs("netbox.jobs", level="ERROR") as cm:
                        self._run_raises()
        self.assertTrue(any("Unhandled error syncing server" in msg for msg in cm.output))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaIpamSyncJobKillSwitches(TestCase):
    """Tests for SyncConfig global kill-switch, per-server sync_enabled, and job metadata.

    Uses the real ORM — Server rows and SyncConfig are created in the test DB.
    Only Kea HTTP calls are mocked via ``_patch_kea``.
    """

    def _run(self) -> MagicMock:
        job = _make_job()
        try:
            KeaIpamSyncJob(job).run()
        except JobFailed:
            pass
        return job

    def _make_db_server(self, **kwargs):
        from netbox_kea.tests.utils import _make_db_server

        return _make_db_server(**kwargs)

    # ── global kill-switch ────────────────────────────────────────────────

    def test_global_kill_switch_creates_no_ips(self):
        """SyncConfig.sync_enabled=False → no IPs synced, no Kea calls made."""
        from netbox_kea.models import SyncConfig

        SyncConfig.objects.create(pk=1, interval_minutes=5, sync_enabled=False, backfill_applied=True)
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertEqual(IPAddress.objects.count(), 0)

    # ── per-server sync_enabled toggle ────────────────────────────────────

    def test_per_server_disabled_skips_that_server(self):
        """Server with sync_enabled=False is not synced; an enabled server is."""
        # Two servers: one enabled, one disabled.
        self._make_db_server(name="enabled", sync_enabled=True)
        self._make_db_server(name="disabled", sync_enabled=False)
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        # Only one IP (from "enabled" server) because both share the same mock
        # which always returns _LEASE4; the disabled server is skipped entirely.
        self.assertEqual(IPAddress.objects.count(), 1)

    # ── server_pk run-now targeting ────────────────────────────────────────

    def test_server_pk_kwarg_targets_single_server(self):
        """run(server_pk=X) syncs only server X, not other servers."""
        self._make_db_server(name="server1")
        server2 = self._make_db_server(name="server2")
        lease_s2 = {**_LEASE4, "ip-address": "10.0.0.20"}
        with _patch_kea(leases4=[lease_s2]):
            KeaIpamSyncJob(_make_job()).run(server_pk=server2.pk)
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.20/").exists())
        self.assertFalse(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())

    def test_server_pk_bypasses_per_server_sync_enabled(self):
        """run(server_pk=X) syncs server X even when its sync_enabled=False."""
        server = self._make_db_server(sync_enabled=False)
        with _patch_kea(leases4=[_LEASE4]):
            KeaIpamSyncJob(_make_job()).run(server_pk=server.pk)
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__startswith="10.0.0.1/").exists())

    # ── job metadata (job.data) ────────────────────────────────────────────

    def test_summary_written_on_global_kill_switch(self):
        """job.data['summary'] is an empty list even when kill-switch aborts the run."""
        from netbox_kea.models import SyncConfig

        SyncConfig.objects.create(pk=1, interval_minutes=5, sync_enabled=False, backfill_applied=True)
        mock_job = _make_job()
        KeaIpamSyncJob(mock_job).run()
        self.assertIn("summary", mock_job.data)
        self.assertEqual(mock_job.data["summary"], [])
        mock_job.save.assert_called_once_with(update_fields=["data"])

    def test_job_data_summary_written_when_data_is_none(self):
        """job.data['summary'] is written even when job.data starts as None."""
        from netbox_kea.models import SyncConfig

        SyncConfig.objects.create(
            pk=1,
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
            backfill_applied=True,
        )
        mock_job = _make_job()
        mock_job.data = None
        KeaIpamSyncJob(mock_job).run()
        self.assertIsInstance(mock_job.data, dict)
        self.assertIn("summary", mock_job.data)
        mock_job.save.assert_called_once_with(update_fields=["data"])


class TestConfigureSyncJobInterval(SimpleTestCase):
    """Tests for NetBoxKeaConfig._configure_sync_job_interval()."""

    def test_interval_override_logs_warning_on_failure(self):
        """When any exception occurs inside _configure_sync_job_interval, a WARNING is logged."""

        from django.apps import apps

        cfg = apps.get_app_config("netbox_kea")

        # Removing netbox_kea.jobs from sys.modules causes 'from .jobs import KeaIpamSyncJob'
        # to raise ImportError, which triggers the except block and the logger.warning call.
        with patch.dict("sys.modules", {"netbox_kea.jobs": None}):
            with self.assertLogs("netbox_kea", level="WARNING") as cm:
                cfg._configure_sync_job_interval()

        self.assertTrue(any("Failed to apply netbox_kea sync interval override" in msg for msg in cm.output))

    def test_interval_set_from_plugins_config_no_db_query(self):
        """PLUGINS_CONFIG.sync_interval_minutes seeds the registry without hitting the DB."""
        from django.apps import apps
        from netbox.registry import registry

        from netbox_kea.jobs import KeaIpamSyncJob

        cfg = apps.get_app_config("netbox_kea")

        # Ensure the job is in the registry so we can check the interval update.
        registry["system_jobs"].setdefault(KeaIpamSyncJob, {"interval": 999})
        original_interval = registry["system_jobs"][KeaIpamSyncJob]["interval"]

        try:
            with override_settings(PLUGINS_CONFIG={"netbox_kea": {"sync_interval_minutes": 17}}):
                # No DB access should occur — if it does, it raises OperationalError in the
                # SimpleTestCase (no DB) and the test would fail with a DB error rather than pass.
                cfg._configure_sync_job_interval()

            self.assertEqual(registry["system_jobs"][KeaIpamSyncJob]["interval"], 17)
        finally:
            registry["system_jobs"][KeaIpamSyncJob]["interval"] = original_interval


class TestGetPluginConfig(SimpleTestCase):
    """Tests for _get_plugin_config() defensive type-checking."""

    def test_returns_dict_when_plugins_config_missing(self):
        """PLUGINS_CONFIG not set → empty dict returned, no exception."""
        from netbox_kea.jobs import _get_plugin_config

        with override_settings(PLUGINS_CONFIG={}):
            result = _get_plugin_config()
        self.assertIsInstance(result, dict)

    def test_returns_dict_when_plugins_config_is_none(self):
        """PLUGINS_CONFIG=None → WARNING logged, empty dict returned."""
        from netbox_kea.jobs import _get_plugin_config

        with override_settings(PLUGINS_CONFIG=None):
            with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
                result = _get_plugin_config()

        self.assertEqual(result, {})
        self.assertTrue(any("PLUGINS_CONFIG" in msg for msg in cm.output))

    def test_returns_dict_when_netbox_kea_section_is_not_dict(self):
        """PLUGINS_CONFIG['netbox_kea'] is a string → WARNING logged, empty dict returned."""
        from netbox_kea.jobs import _get_plugin_config

        with override_settings(PLUGINS_CONFIG={"netbox_kea": "bad-value"}):
            with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
                result = _get_plugin_config()

        self.assertEqual(result, {})
        self.assertTrue(any("netbox_kea" in msg for msg in cm.output))


# ---------------------------------------------------------------------------
# Tests for _sync_subnet_entry (unit — no DB)
# ---------------------------------------------------------------------------


class TestSyncSubnetEntry(SimpleTestCase):
    """Tests for the _sync_subnet_entry helper in jobs.py."""

    def _make_stats(self):
        return {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}

    def test_no_subnet_cidr_returns_early(self):
        """Subnet dict without 'subnet' key → no sync called, stats unchanged."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        with patch("netbox_kea.sync.sync_subnet_to_netbox_prefix") as mock_prefix:
            _sync_subnet_entry({}, sync_prefixes=True, sync_ip_ranges=True, vrf=None, stats=stats, server_name="s")
        mock_prefix.assert_not_called()
        self.assertEqual(stats, {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0})

    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix", return_value=(MagicMock(), True, False))
    def test_prefix_created_increments_created(self, mock_prefix):
        """New prefix (created=True) increments stats['created']."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        _sync_subnet_entry(
            {"subnet": "10.0.0.0/24"},
            sync_prefixes=True,
            sync_ip_ranges=False,
            vrf=None,
            stats=stats,
            server_name="s",
        )
        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["updated"], 0)

    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix", return_value=(MagicMock(), False, True))
    def test_prefix_updated_increments_updated(self, mock_prefix):
        """Updated prefix (created=False, did_update=True) increments stats['updated']."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        _sync_subnet_entry(
            {"subnet": "10.0.0.0/24"},
            sync_prefixes=True,
            sync_ip_ranges=False,
            vrf=None,
            stats=stats,
            server_name="s",
        )
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["created"], 0)

    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix", return_value=(MagicMock(), False, False))
    def test_prefix_idempotent_does_not_increment(self, mock_prefix):
        """Unchanged prefix (created=False, did_update=False) leaves stats at 0."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        _sync_subnet_entry(
            {"subnet": "10.0.0.0/24"},
            sync_prefixes=True,
            sync_ip_ranges=False,
            vrf=None,
            stats=stats,
            server_name="s",
        )
        self.assertEqual(stats, {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0})

    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix")
    def test_sync_prefixes_false_skips_prefix_call(self, mock_prefix):
        """sync_prefixes=False → prefix sync function never called."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        _sync_subnet_entry(
            {"subnet": "10.0.0.0/24"},
            sync_prefixes=False,
            sync_ip_ranges=False,
            vrf=None,
            stats=stats,
            server_name="s",
        )
        mock_prefix.assert_not_called()

    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix", side_effect=Exception("db down"))
    def test_prefix_exception_increments_errors(self, mock_prefix):
        """Exception in prefix sync → stats['prefix_errors'] incremented, no re-raise."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        _sync_subnet_entry(
            {"subnet": "10.0.0.0/24"},
            sync_prefixes=True,
            sync_ip_ranges=False,
            vrf=None,
            stats=stats,
            server_name="s",
        )
        self.assertEqual(stats["prefix_errors"], 1)
        self.assertEqual(stats["errors"], 0)

    @patch("netbox_kea.sync.sync_pool_to_netbox_ip_range", return_value=(MagicMock(), True, False))
    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix", return_value=(MagicMock(), False, False))
    def test_pool_created_increments_created(self, mock_prefix, mock_range):
        """New IP range (created=True) increments stats['created']."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        subnet = {"subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10-10.0.0.50"}]}
        _sync_subnet_entry(subnet, sync_prefixes=True, sync_ip_ranges=True, vrf=None, stats=stats, server_name="s")
        self.assertEqual(stats["created"], 1)

    @patch("netbox_kea.sync.sync_pool_to_netbox_ip_range", side_effect=Exception("overflow"))
    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix", return_value=(MagicMock(), False, False))
    def test_pool_exception_increments_errors(self, mock_prefix, mock_range):
        """Exception in pool sync → stats['prefix_errors'] incremented, no re-raise."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        subnet = {"subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10-10.0.0.50"}]}
        _sync_subnet_entry(subnet, sync_prefixes=False, sync_ip_ranges=True, vrf=None, stats=stats, server_name="s")
        self.assertEqual(stats["prefix_errors"], 1)
        self.assertEqual(stats["errors"], 0)

    @patch("netbox_kea.sync.sync_pool_to_netbox_ip_range", return_value=None)
    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix", return_value=(MagicMock(), False, False))
    def test_pool_none_result_counts_as_error(self, mock_prefix, mock_range):
        """sync_pool_to_netbox_ip_range returning None (unparseable pool) increments prefix_errors."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        subnet = {"subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10-10.0.0.50"}]}
        _sync_subnet_entry(subnet, sync_prefixes=False, sync_ip_ranges=True, vrf=None, stats=stats, server_name="s")
        self.assertEqual(stats, {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 1})

    @patch("netbox_kea.sync.sync_subnet_to_netbox_prefix", return_value=(MagicMock(), True, False))
    def test_vrf_forwarded_to_prefix_sync(self, mock_prefix):
        """vrf value is forwarded to sync_subnet_to_netbox_prefix."""
        from netbox_kea.jobs import _sync_subnet_entry

        fake_vrf = MagicMock()
        _sync_subnet_entry(
            {"subnet": "10.0.0.0/24"},
            sync_prefixes=True,
            sync_ip_ranges=False,
            vrf=fake_vrf,
            stats=self._make_stats(),
            server_name="s",
        )
        _, call_kwargs = mock_prefix.call_args
        self.assertEqual(call_kwargs["vrf"], fake_vrf)

    @patch("netbox_kea.sync.sync_pool_to_netbox_ip_range", return_value=(MagicMock(), True, False))
    def test_vrf_forwarded_to_pool_sync(self, mock_range):
        """vrf value is forwarded to sync_pool_to_netbox_ip_range."""
        from netbox_kea.jobs import _sync_subnet_entry

        fake_vrf = MagicMock()
        subnet = {"subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10-10.0.0.50"}]}
        _sync_subnet_entry(
            subnet,
            sync_prefixes=False,
            sync_ip_ranges=True,
            vrf=fake_vrf,
            stats=self._make_stats(),
            server_name="s",
        )
        _, call_kwargs = mock_range.call_args
        self.assertEqual(call_kwargs["vrf"], fake_vrf)

    @patch("netbox_kea.sync.sync_pool_to_netbox_ip_range")
    def test_pool_entry_missing_pool_key_skipped(self, mock_range):
        """Pool entry without 'pool' key is silently skipped."""
        from netbox_kea.jobs import _sync_subnet_entry

        subnet = {"subnet": "10.0.0.0/24", "pools": [{"some-other-key": "value"}]}
        _sync_subnet_entry(
            subnet, sync_prefixes=False, sync_ip_ranges=True, vrf=None, stats=self._make_stats(), server_name="s"
        )
        mock_range.assert_not_called()

    def test_pool_too_large_is_silently_skipped(self):
        """_POOL_TOO_LARGE sentinel from sync_pool_to_netbox_ip_range → stats unchanged (line 302)."""
        from netbox_kea.jobs import _sync_subnet_entry
        from netbox_kea.sync import _POOL_TOO_LARGE

        stats = self._make_stats()
        subnet = {"subnet": "10.0.0.0/8", "pools": [{"pool": "10.0.0.0-10.255.255.255"}]}
        with patch("netbox_kea.sync.sync_pool_to_netbox_ip_range", return_value=_POOL_TOO_LARGE):
            _sync_subnet_entry(subnet, sync_prefixes=False, sync_ip_ranges=True, vrf=None, stats=stats, server_name="s")
        self.assertEqual(stats, {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0})

    @patch("netbox_kea.sync.sync_pool_to_netbox_ip_range", return_value=(MagicMock(), False, True))
    def test_pool_updated_increments_updated(self, mock_range):
        """Updated IP range (created=False, did_update=True) increments stats['updated'] (lines 315-316)."""
        from netbox_kea.jobs import _sync_subnet_entry

        stats = self._make_stats()
        subnet = {"subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10-10.0.0.50"}]}
        _sync_subnet_entry(subnet, sync_prefixes=False, sync_ip_ranges=True, vrf=None, stats=stats, server_name="s")
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["created"], 0)


# ---------------------------------------------------------------------------
# Tests for _sync_server_prefixes_and_ranges (unit — no DB)
# ---------------------------------------------------------------------------

_DHCP4_CONFIG_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [
                    {"subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10-10.0.0.50"}]},
                    {"subnet": "10.0.1.0/24", "pools": []},
                ],
                "shared-networks": [],
            }
        },
    }
]

_DHCP4_CONFIG_WITH_SHARED = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [{"subnet": "10.0.0.0/24", "pools": []}],
                "shared-networks": [
                    {
                        "name": "net-a",
                        "subnet4": [{"subnet": "192.168.1.0/24", "pools": [{"pool": "192.168.1.10-192.168.1.100"}]}],
                    }
                ],
            }
        },
    }
]

_DHCP6_CONFIG_WITH_SHARED = [
    {
        "result": 0,
        "arguments": {
            "Dhcp6": {
                "subnet6": [{"subnet": "2001:db8::/48", "pools": []}],
                "shared-networks": [
                    {
                        "name": "net-b",
                        "subnet6": [
                            {
                                "subnet": "2001:db8:1::/64",
                                "pools": [{"pool": "2001:db8:1::10-2001:db8:1::ff"}],
                            }
                        ],
                    }
                ],
            }
        },
    }
]


class TestFetchKeaSubnets(SimpleTestCase):
    """_fetch_kea_subnets fetches + parses the Kea subnet list, returning None on failure."""

    def _make_server(self, name="kea1"):
        server = MagicMock()
        server.name = name
        return server

    def _server_returning(self, command_return=None, command_side_effect=None, get_client_side_effect=None):
        server = self._make_server()
        if get_client_side_effect is not None:
            server.get_client.side_effect = get_client_side_effect
            return server
        client = MagicMock()
        if command_side_effect is not None:
            client.command.side_effect = command_side_effect
        else:
            client.command.return_value = command_return
        server.get_client.return_value = client
        return server

    def test_returns_subnet_list(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning(_DHCP4_CONFIG_RESPONSE)
        self.assertEqual(len(_fetch_kea_subnets(server, 4)), 2)

    def test_shared_network_subnets_included(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning(_DHCP4_CONFIG_WITH_SHARED)
        # 1 top-level subnet + 1 in shared-network = 2
        self.assertEqual(len(_fetch_kea_subnets(server, 4)), 2)

    def test_shared_network_subnets_v6_included(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning(_DHCP6_CONFIG_WITH_SHARED)
        self.assertEqual(len(_fetch_kea_subnets(server, 6)), 2)

    def test_get_client_exception_returns_none(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning(get_client_side_effect=ValueError("no url"))
        self.assertIsNone(_fetch_kea_subnets(server, 4))

    def test_command_exception_returns_none(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning(command_side_effect=Exception("timeout"))
        self.assertIsNone(_fetch_kea_subnets(server, 4))

    def test_malformed_non_list_returns_none(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning("not-a-list")
        self.assertIsNone(_fetch_kea_subnets(server, 4))

    def test_result_nonzero_returns_none(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning([{"result": 1, "text": "internal error"}])
        self.assertIsNone(_fetch_kea_subnets(server, 4))

    def test_arguments_not_dict_returns_none(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning([{"result": 0, "arguments": "not-a-dict"}])
        self.assertIsNone(_fetch_kea_subnets(server, 4))

    def test_arguments_none_returns_empty_list(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning([{"result": 0, "arguments": None}])
        self.assertEqual(_fetch_kea_subnets(server, 4), [])

    def test_dhcp_config_not_dict_returns_none(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning([{"result": 0, "arguments": {"Dhcp4": "not-a-dict"}}])
        self.assertIsNone(_fetch_kea_subnets(server, 4))

    def test_empty_subnet_list_returns_empty(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning([{"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}])
        self.assertEqual(_fetch_kea_subnets(server, 4), [])

    def test_non_dict_shared_network_entry_is_skipped(self):
        from netbox_kea.jobs import _fetch_kea_subnets

        server = self._server_returning(
            [
                {
                    "result": 0,
                    "arguments": {
                        "Dhcp4": {
                            "subnet4": [],
                            "shared-networks": [
                                "not-a-dict",
                                {"name": "net-a", "subnet4": [{"subnet": "10.1.0.0/24", "pools": []}]},
                            ],
                        }
                    },
                }
            ]
        )
        subnets = _fetch_kea_subnets(server, 4)
        self.assertEqual(len(subnets), 1)
        self.assertEqual(subnets[0]["subnet"], "10.1.0.0/24")


class TestBuildSubnetPrefixMap(SimpleTestCase):
    """_build_subnet_prefix_map maps Kea subnet-id → prefix length."""

    def test_builds_map_from_subnet_cidrs(self):
        from netbox_kea.jobs import _build_subnet_prefix_map

        subnets = [{"id": 1, "subnet": "10.0.0.0/24"}, {"id": 10, "subnet": "2001:db8::/64"}]
        self.assertEqual(_build_subnet_prefix_map(subnets), {1: 24, 10: 64})

    def test_skips_entries_missing_id_or_cidr_or_unparseable(self):
        from netbox_kea.jobs import _build_subnet_prefix_map

        subnets = [{"subnet": "10.0.0.0/24"}, {"id": 2}, {"id": 3, "subnet": "bad"}]
        self.assertEqual(_build_subnet_prefix_map(subnets), {})

    def test_none_returns_empty_map(self):
        from netbox_kea.jobs import _build_subnet_prefix_map

        self.assertEqual(_build_subnet_prefix_map(None), {})


class TestSyncServerPrefixesAndRanges(SimpleTestCase):
    """_sync_server_prefixes_and_ranges processes a pre-fetched subnet list."""

    def _make_server(self, name="kea1"):
        server = MagicMock()
        server.name = name
        server.sync_vrf = None
        return server

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_syncs_each_subnet(self, mock_entry):
        """Two subnets → _sync_subnet_entry called twice."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        subnets = [{"subnet": "10.0.0.0/24", "pools": []}, {"subnet": "10.0.1.0/24", "pools": []}]
        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(
            self._make_server(), version=4, subnets=subnets, sync_prefixes=True, sync_ip_ranges=True, stats=stats
        )
        self.assertEqual(mock_entry.call_count, 2)

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_none_subnets_increments_prefix_errors(self, mock_entry):
        """subnets=None (fetch failed) → prefix_errors incremented, no entries processed."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(
            self._make_server(), version=4, subnets=None, sync_prefixes=True, sync_ip_ranges=True, stats=stats
        )
        mock_entry.assert_not_called()
        self.assertEqual(stats["prefix_errors"], 1)
        self.assertEqual(stats["errors"], 0)

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_empty_subnet_list_no_calls(self, mock_entry):
        """Empty subnet list → _sync_subnet_entry never called, no errors."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(
            self._make_server(), version=4, subnets=[], sync_prefixes=True, sync_ip_ranges=True, stats=stats
        )
        mock_entry.assert_not_called()
        self.assertEqual(stats["prefix_errors"], 0)

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_vrf_forwarded_to_subnet_entry(self, mock_entry):
        """vrf kwarg is passed through to each _sync_subnet_entry call."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        fake_vrf = MagicMock()
        subnets = [{"subnet": "10.0.0.0/24", "pools": []}]
        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(
            self._make_server(),
            version=4,
            subnets=subnets,
            sync_prefixes=True,
            sync_ip_ranges=True,
            vrf=fake_vrf,
            stats=stats,
        )
        for c in mock_entry.call_args_list:
            self.assertEqual(c.args[3], fake_vrf)  # vrf is the 4th positional arg


# ---------------------------------------------------------------------------
# Tests for per-server prefix/range toggle in KeaIpamSyncJob.run()
# ---------------------------------------------------------------------------


class TestPerServerPrefixRangeToggles(SimpleTestCase):
    """Per-server sync_prefixes_enabled / sync_ip_ranges_enabled override tests."""

    def setUp(self):
        patcher = patch("netbox_kea.models.SyncConfig")
        self.MockSyncConfig = patcher.start()
        self.MockSyncConfig.get.return_value = MagicMock(
            sync_enabled=True,
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=True,
            sync_ip_ranges_enabled=True,
        )
        self.addCleanup(patcher.stop)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.jobs._sync_server_prefixes_and_ranges")
    @patch("netbox_kea.models.Server")
    def test_prefix_range_sync_called_when_both_enabled(self, MockServer, mock_pr):
        """Global and per-server both enabled → _sync_server_prefixes_and_ranges called."""
        server = _make_server()
        server.sync_prefixes_enabled = True
        server.sync_ip_ranges_enabled = True
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        mock_pr.assert_called()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.jobs._sync_server_prefixes_and_ranges")
    @patch("netbox_kea.models.Server")
    def test_prefix_range_not_called_when_server_disables_both(self, MockServer, mock_pr):
        """Global enabled but server disables both → _sync_server_prefixes_and_ranges NOT called."""
        server = _make_server()
        server.sync_prefixes_enabled = False
        server.sync_ip_ranges_enabled = False
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        mock_pr.assert_not_called()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.jobs._sync_server_prefixes_and_ranges")
    @patch("netbox_kea.models.Server")
    def test_only_prefix_enabled_calls_with_sync_ip_ranges_false(self, MockServer, mock_pr):
        """Server disables ip_ranges but keeps prefixes → called with sync_ip_ranges=False."""
        server = _make_server()
        server.sync_prefixes_enabled = True
        server.sync_ip_ranges_enabled = False
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        mock_pr.assert_called()
        # Check the effective flags passed in call
        call_kwargs = mock_pr.call_args_list[0].kwargs
        self.assertTrue(call_kwargs["sync_prefixes"])
        self.assertFalse(call_kwargs["sync_ip_ranges"])

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.jobs._sync_server_prefixes_and_ranges")
    @patch("netbox_kea.models.Server")
    def test_global_prefixes_disabled_overrides_server(self, MockServer, mock_pr):
        """Global sync_prefixes_enabled=False AND sync_ip_ranges_enabled=False → no call."""
        self.MockSyncConfig.get.return_value.sync_prefixes_enabled = False
        self.MockSyncConfig.get.return_value.sync_ip_ranges_enabled = False
        server = _make_server()
        server.sync_prefixes_enabled = True
        server.sync_ip_ranges_enabled = True
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        mock_pr.assert_not_called()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.jobs._sync_server_prefixes_and_ranges")
    @patch("netbox_kea.models.Server")
    def test_sync_vrf_forwarded_to_prefix_range_sync(self, MockServer, mock_pr):
        """server.sync_vrf is forwarded as vrf= to _sync_server_prefixes_and_ranges."""
        server = _make_server()
        fake_vrf = MagicMock(name="vrf-red")
        server.sync_vrf = fake_vrf
        server.sync_prefixes_enabled = True
        server.sync_ip_ranges_enabled = True
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        for c in mock_pr.call_args_list:
            self.assertEqual(c.kwargs.get("vrf"), fake_vrf)


# ---------------------------------------------------------------------------
# Tests for global all-sync-disabled early return
# ---------------------------------------------------------------------------


class TestAllSyncTypesDisabled(SimpleTestCase):
    """When all four sync type flags are False, run() exits early."""

    def setUp(self):
        patcher = patch("netbox_kea.models.SyncConfig")
        self.MockSyncConfig = patcher.start()
        self.MockSyncConfig.get.return_value = MagicMock(
            sync_enabled=True,
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        self.addCleanup(patcher.stop)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.Server")
    def test_all_disabled_logs_nothing_to_do(self, MockServer):
        """All four sync flags False → 'nothing to do' logged, Server.objects.all not called."""
        with self.assertLogs(level="INFO") as cm:
            KeaIpamSyncJob(_make_job()).run()

        MockServer.objects.all.assert_not_called()
        self.assertTrue(any("nothing to do" in msg.lower() for msg in cm.output))


# ---------------------------------------------------------------------------
# Tests for _prefetch_reservation_ips edge cases
# ---------------------------------------------------------------------------


class TestPrefetchReservationIpsEdgeCases(SimpleTestCase):
    """Edge-case tests for _prefetch_reservation_ips (lines 90, 95-96 in jobs.py)."""

    def _make_server(self):
        server = MagicMock()
        server.name = "kea1"
        return server

    def test_non_dict_page_item_is_skipped(self):
        """Non-dict items in the reservation page are silently skipped (line 90)."""
        from netbox_kea.jobs import _prefetch_reservation_ips

        server = self._make_server()
        client = MagicMock()
        page = ["not-a-dict", {"ip-address": "10.0.0.1"}]
        client.reservation_get_page.return_value = (page, 0, 0)
        server.get_client.return_value = client

        result = _prefetch_reservation_ips(server, version=4)

        self.assertIsNotNone(result)
        self.assertIn("10.0.0.1", result)
        self.assertEqual(len(result), 1)

    def test_ipv6_ip_addresses_list_collected(self):
        """Reservation with ip-addresses list → all addresses added to result set (lines 95-96)."""
        from netbox_kea.jobs import _prefetch_reservation_ips

        server = self._make_server()
        client = MagicMock()
        page = [{"ip-addresses": ["2001:db8::1", "2001:db8::2"]}]
        client.reservation_get_page.return_value = (page, 0, 0)
        server.get_client.return_value = client

        result = _prefetch_reservation_ips(server, version=6)

        self.assertIsNotNone(result)
        self.assertIn("2001:db8::1", result)
        self.assertIn("2001:db8::2", result)


# ---------------------------------------------------------------------------
# Tests for reservation updated path in _sync_server_reservations
# ---------------------------------------------------------------------------


class TestSyncServerReservationsUpdated(SimpleTestCase):
    """Tests that an updated reservation (changed=True, created=False) increments stats['updated']."""

    def _make_server(self):
        server = MagicMock()
        server.name = "kea1"
        return server

    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False, True))
    def test_reservation_updated_increments_stats(self, mock_sync_resv):
        """changed=True in sync_reservation_to_netbox → stats['updated'] += 1 (line 233)."""
        from netbox_kea.jobs import _sync_server_reservations

        server = self._make_server()
        client = MagicMock()
        client.reservation_get_page.return_value = ([_RESV4], 0, 0)
        server.get_client.return_value = client

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        all_synced: list = []
        result = _sync_server_reservations(server, version=4, stats=stats, all_synced=all_synced)

        self.assertTrue(result)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["created"], 0)


# ---------------------------------------------------------------------------
# Tests for job.save() failure in run() finally block
# ---------------------------------------------------------------------------


class TestJobSaveFailure(SimpleTestCase):
    """Tests that job.save() failure in the finally block is caught and logged."""

    def setUp(self):
        patcher = patch("netbox_kea.models.SyncConfig")
        self.MockSyncConfig = patcher.start()
        self.MockSyncConfig.get.return_value = MagicMock(
            sync_enabled=True,
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        self.addCleanup(patcher.stop)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.models.Server")
    def test_save_exception_is_caught_and_logged(self, MockServer):
        """job.save() raising an exception is caught; run() does not re-raise (lines 679-680)."""
        MockServer.objects.all.return_value = []
        mock_job = _make_job()
        mock_job.data = {}
        mock_job.save.side_effect = Exception("db write failed")

        with self.assertLogs("netbox_kea.jobs", level="ERROR") as cm:
            KeaIpamSyncJob(mock_job).run()

        self.assertTrue(any("persist" in msg.lower() or "summary" in msg.lower() for msg in cm.output))
