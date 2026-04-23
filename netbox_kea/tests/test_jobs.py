# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea/jobs.py — KeaIpamSyncJob."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from core.exceptions import JobFailed
from django.test import SimpleTestCase, override_settings

from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.kea import KeaException

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
    mock_job.log = MagicMock()
    return mock_job


def _make_server(name: str = "kea1", dhcp4: bool = True, dhcp6: bool = False, pk: int = 1) -> MagicMock:
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


def _make_client(
    leases4: list[dict] | None = None,
    leases6: list[dict] | None = None,
    reservations: list[dict] | None = None,
    truncated: bool = False,
) -> MagicMock:
    """Return a mock KeaClient pre-configured with standard responses."""
    client = MagicMock()

    def _lease_get_all(*args, **kwargs):
        version = kwargs.get("version") or (args[0] if args else 4)
        if version == 6:
            return (leases6 or [], truncated)
        return (leases4 or [], truncated)

    client.lease_get_all.side_effect = _lease_get_all
    client.reservation_get_page.return_value = (reservations or [], 0, 0)
    return client


class TestKeaIpamSyncJobRun(SimpleTestCase):
    """Tests for KeaIpamSyncJob.run()."""

    def setUp(self):
        """Patch SyncConfig so run() doesn't hit the DB in unit tests."""
        patcher = patch("netbox_kea.models.SyncConfig")
        self.MockSyncConfig = patcher.start()
        self.MockSyncConfig.get.return_value = MagicMock(
            sync_enabled=True,
            interval_minutes=5,
            sync_leases_enabled=True,
            sync_reservations_enabled=True,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        self.addCleanup(patcher.stop)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_syncs_leases_and_reservations(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """Normal path: leases and reservations both synced."""
        server = _make_server()
        client = _make_client(leases4=[_LEASE4], reservations=[_RESV4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        job = KeaIpamSyncJob(_make_job())
        job.run()

        mock_sync_lease.assert_called_once_with(_LEASE4, cleanup=False)
        mock_sync_resv.assert_called_once_with(_RESV4, cleanup=False)
        mock_cleanup.assert_called_once()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_skips_leases_when_disabled(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """sync_leases_enabled=False → lease_get_all never called."""
        self.MockSyncConfig.get.return_value.sync_leases_enabled = False
        server = _make_server()
        client = _make_client(reservations=[_RESV4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        client.lease_get_all.assert_not_called()
        mock_sync_lease.assert_not_called()
        mock_sync_resv.assert_called_once()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_skips_reservations_when_disabled(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """sync_reservations_enabled=False → reservation_get_page never called."""
        self.MockSyncConfig.get.return_value.sync_reservations_enabled = False
        server = _make_server()
        client = _make_client(leases4=[_LEASE4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        client.reservation_get_page.assert_not_called()
        mock_sync_resv.assert_not_called()
        mock_sync_lease.assert_called_once()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_isolates_per_server_errors(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """Exception on server 1 does not prevent server 2 from syncing."""
        server1 = _make_server("s1", pk=1)
        server1.get_client.side_effect = ValueError("connection refused")

        server2 = _make_server("s2", pk=2)
        client2 = _make_client(leases4=[_LEASE4])
        server2.get_client.return_value = client2

        MockServer.objects.all.return_value = [server1, server2]

        with self.assertRaises(JobFailed):
            KeaIpamSyncJob(_make_job()).run()

        # server2 lease was still synced
        mock_sync_lease.assert_called_once_with(_LEASE4, cleanup=False)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_logs_truncation_warning(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """lease_get_all returns truncated=True → warning logged, sync still proceeds, cleanup skipped."""
        server = _make_server()
        client = _make_client(leases4=[_LEASE4], truncated=True)
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
            KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("truncated" in msg for msg in cm.output))
        mock_sync_lease.assert_called_once()
        # Truncated fetch means cleanup_safe=False — cleanup must not run
        mock_cleanup.assert_not_called()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_skips_host_cmds_absent(self, MockServer, mock_sync_lease, mock_cleanup):
        """reservation_get_page result=2 (host_cmds not loaded) → WARNING logged, no exception."""
        server = _make_server()
        client = MagicMock()
        client.lease_get_all.return_value = ([], False)
        client.reservation_get_page.side_effect = KeaException({"result": 2, "text": "unknown command"})
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
            KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("host_cmds" in msg for msg in cm.output))

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_cleanup_skipped_when_host_cmds_absent(self, MockServer, mock_sync_lease, mock_cleanup):
        """When host_cmds not loaded (result=2), reservation sync is skipped → cleanup_safe=False → no cleanup."""
        server = _make_server()
        client = MagicMock()
        client.lease_get_all.return_value = ([_LEASE4], False)
        client.reservation_get_page.side_effect = KeaException({"result": 2, "text": "unknown command"})
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING"):
            KeaIpamSyncJob(_make_job()).run()

        # cleanup_safe=False from reservation skip — cleanup must not run
        mock_cleanup.assert_not_called()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_v4_only_server(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """dhcp4=True, dhcp6=False → get_client called only with version=4."""
        server = _make_server(dhcp4=True, dhcp6=False)
        client = _make_client(leases4=[_LEASE4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(server.get_client.called, "get_client() was never called")
        for c in server.get_client.call_args_list:
            self.assertEqual(c, call(version=4))

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_v6_only_server(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """dhcp4=False, dhcp6=True → get_client called only with version=6."""
        server = _make_server(dhcp4=False, dhcp6=True)
        client = _make_client(leases6=[_LEASE6])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(server.get_client.called, "get_client() was never called")
        for c in server.get_client.call_args_list:
            self.assertEqual(c, call(version=6))

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_no_servers_is_no_op(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """No servers configured → sync functions never called."""
        MockServer.objects.all.return_value = []

        KeaIpamSyncJob(_make_job()).run()

        mock_sync_lease.assert_not_called()
        mock_sync_resv.assert_not_called()
        mock_cleanup.assert_not_called()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", side_effect=ValueError("bad address"))
    @patch("netbox_kea.models.Server")
    def test_per_lease_error_increments_error_counter(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """Individual sync failure is logged at DEBUG and doesn't abort the batch."""
        server = _make_server()
        lease2 = {**_LEASE4, "ip-address": "10.0.0.2"}
        client = _make_client(leases4=[_LEASE4, lease2], reservations=[])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        # JobFailed is raised when total["errors"] > 0
        with self.assertRaises(JobFailed):
            KeaIpamSyncJob(_make_job()).run()

        self.assertEqual(mock_sync_lease.call_count, 2)
        # errors > 0 → cleanup must not run (partial all_synced would cause false deletions)
        mock_cleanup.assert_not_called()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_cleanup_called_once_per_server(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """cleanup_stale_ips_batch is called exactly once per server (not per lease/reservation)."""
        server1 = _make_server("s1", pk=1)
        client1 = _make_client(leases4=[_LEASE4, {**_LEASE4, "ip-address": "10.0.0.2"}], reservations=[_RESV4])
        server1.get_client.return_value = client1

        server2 = _make_server("s2", pk=2)
        client2 = _make_client(leases4=[{**_LEASE4, "ip-address": "10.1.0.1"}])
        server2.get_client.return_value = client2

        MockServer.objects.all.return_value = [server1, server2]

        KeaIpamSyncJob(_make_job()).run()

        # One call per server (2 servers)
        self.assertEqual(mock_cleanup.call_count, 2)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_dual_protocol_server_calls_both_versions(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """Server with dhcp4=True and dhcp6=True → get_client called for both v4 and v6."""
        server = _make_server(dhcp4=True, dhcp6=True)
        client = MagicMock()
        client.lease_get_all.return_value = ([], False)
        client.reservation_get_page.return_value = ([], 0, 0)
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        versions_called = {c.kwargs["version"] for c in server.get_client.call_args_list}
        self.assertEqual(versions_called, {4, 6})

    # ------------------------------------------------------------------ #
    # max_leases normalization                                             #
    # ------------------------------------------------------------------ #

    @override_settings(
        PLUGINS_CONFIG={
            **_PLUGINS_CONFIG,
            "netbox_kea": {**_PLUGINS_CONFIG["netbox_kea"], "sync_max_leases_per_server": "not-a-number"},
        }
    )
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_invalid_max_leases_string_falls_back_to_default(self, MockServer, mock_sync_lease, mock_cleanup):
        """sync_max_leases_per_server='not-a-number' → warning logged, fallback to 50000."""
        server = _make_server()
        client = _make_client(leases4=[_LEASE4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox.jobs", level="WARNING") as cm:
            KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("Invalid sync_max_leases_per_server" in msg for msg in cm.output))
        # Sync still ran with fallback value
        mock_sync_lease.assert_called_once_with(_LEASE4, cleanup=False)

    @override_settings(
        PLUGINS_CONFIG={
            **_PLUGINS_CONFIG,
            "netbox_kea": {**_PLUGINS_CONFIG["netbox_kea"], "sync_max_leases_per_server": -1},
        }
    )
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_negative_max_leases_resets_to_zero(self, MockServer, mock_sync_lease, mock_cleanup):
        """sync_max_leases_per_server=-1 → warning logged, value reset to 0 (no cap)."""
        server = _make_server()
        client = _make_client(leases4=[_LEASE4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox.jobs", level="WARNING") as cm:
            KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("Negative sync_max_leases_per_server" in msg for msg in cm.output))
        mock_sync_lease.assert_called_once()

    # ------------------------------------------------------------------ #
    # Skip cleanup on sync errors                                          #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.models.Server")
    def test_cleanup_skipped_when_errors_occurred(self, MockServer, mock_sync_resv, mock_cleanup):
        """When lease sync errors occur, cleanup_stale_ips_batch must NOT run to avoid partial-set deletions.

        One lease succeeds (populates all_synced) and one fails (stats['errors']=1).
        The non-empty all_synced + errors>0 should trigger the warning and skip cleanup.
        """
        server = _make_server()
        good_lease = _LEASE4
        bad_lease = {**_LEASE4, "ip-address": "10.0.0.2"}

        def _side_effect(lease, **kwargs):
            if lease["ip-address"] == "10.0.0.2":
                raise ValueError("bad address")
            return (MagicMock(), True)

        with patch("netbox_kea.sync.sync_lease_to_netbox", side_effect=_side_effect):
            client = _make_client(leases4=[good_lease, bad_lease])
            server.get_client.return_value = client
            MockServer.objects.all.return_value = [server]

            with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
                with self.assertRaises(JobFailed):
                    KeaIpamSyncJob(_make_job()).run()

        mock_cleanup.assert_not_called()
        self.assertTrue(any("skipping stale-IP cleanup" in msg for msg in cm.output))

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.jobs._sync_server_prefixes_and_ranges")
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_prefix_errors_do_not_block_stale_ip_cleanup(
        self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup, mock_pr
    ):
        """Prefix/range errors must NOT block stale-IP cleanup.

        Lease+reservation sync succeeded (errors=0, all_synced non-empty, cleanup_safe=True).
        A prefix_errors from _sync_server_prefixes_and_ranges should not prevent cleanup.
        """
        self.MockSyncConfig.get.return_value.sync_prefixes_enabled = True
        self.MockSyncConfig.get.return_value.sync_ip_ranges_enabled = True

        server = _make_server()
        client = _make_client(leases4=[_LEASE4], reservations=[_RESV4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        # Simulate a prefix error: mutate the stats dict that's passed in
        def _add_prefix_error(srv, version, **kwargs):
            kwargs["stats"]["prefix_errors"] += 1

        mock_pr.side_effect = _add_prefix_error

        with self.assertLogs("netbox.jobs", level="INFO"):
            with self.assertRaises(JobFailed):
                KeaIpamSyncJob(_make_job()).run()

        # Cleanup must still run despite prefix errors (lease/reservation errors=0)
        mock_cleanup.assert_called_once()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.jobs._sync_server_prefixes_and_ranges")
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_prefix_errors_alone_raise_job_failed(
        self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup, mock_pr
    ):
        """prefix_errors > 0 with no lease/reservation errors still raises JobFailed."""
        self.MockSyncConfig.get.return_value.sync_prefixes_enabled = True
        self.MockSyncConfig.get.return_value.sync_ip_ranges_enabled = True

        server = _make_server()
        client = _make_client(leases4=[_LEASE4], reservations=[_RESV4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        def _add_prefix_error(srv, version, **kwargs):
            kwargs["stats"]["prefix_errors"] += 1

        mock_pr.side_effect = _add_prefix_error

        with self.assertLogs("netbox.jobs", level="INFO"):
            with self.assertRaises(JobFailed):
                KeaIpamSyncJob(_make_job()).run()

    # ------------------------------------------------------------------ #
    # lease sync updated (not created)                                     #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), False))  # False = updated
    @patch("netbox_kea.models.Server")
    def test_lease_updated_increments_updated_counter(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """sync_lease_to_netbox returning (ip, False) increments stats['updated'], not 'created'."""
        server = _make_server()
        client = _make_client(leases4=[_LEASE4], reservations=[])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox.jobs", level="INFO") as cm:
            KeaIpamSyncJob(_make_job()).run()

        mock_sync_lease.assert_called_once_with(_LEASE4, cleanup=False)
        # Verify stats['updated'] was incremented (not 'created')
        self.assertTrue(any("updated=1" in msg and "created=0" in msg for msg in cm.output))

    # ------------------------------------------------------------------ #
    # both sync flags disabled                                             #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_both_sync_disabled_returns_early(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """When all sync type flags are False, job returns immediately."""
        sync_cfg = self.MockSyncConfig.get.return_value
        sync_cfg.sync_leases_enabled = False
        sync_cfg.sync_reservations_enabled = False
        sync_cfg.sync_prefixes_enabled = False
        sync_cfg.sync_ip_ranges_enabled = False
        KeaIpamSyncJob(_make_job()).run()

        MockServer.objects.all.assert_not_called()
        mock_sync_lease.assert_not_called()
        mock_sync_resv.assert_not_called()
        mock_cleanup.assert_not_called()

    # ------------------------------------------------------------------ #
    # reservation multi-page pagination                                    #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_reservation_pagination_fetches_all_pages(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """When reservation_get_page returns non-zero next_from/next_source, loop continues."""
        server = _make_server()
        client = MagicMock()
        client.lease_get_all.return_value = ([], False)
        # First call returns page with next_from=1, next_source=0 → continue
        resv1 = {**_RESV4, "ip-address": "10.0.0.100"}
        resv2 = {**_RESV4, "ip-address": "10.0.0.101"}
        client.reservation_get_page.side_effect = [
            ([resv1], 1, 0),  # first page, pagination continues
            ([resv2], 0, 0),  # second page, done
        ]
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        self.assertEqual(mock_sync_resv.call_count, 2)
        mock_sync_resv.assert_any_call(resv1, cleanup=False)
        mock_sync_resv.assert_any_call(resv2, cleanup=False)

    # ------------------------------------------------------------------ #
    # reservation KeaException non-result-2                               #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_reservation_kea_exception_non_result2_increments_errors(self, MockServer, mock_sync_lease, mock_cleanup):
        """KeaException with result != 2 on reservation fetch → warning logged, errors incremented."""
        server = _make_server()
        client = MagicMock()
        client.lease_get_all.return_value = ([_LEASE4], False)
        exc = KeaException({"result": 1, "text": "internal error"})
        client.reservation_get_page.side_effect = exc
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
            with self.assertRaises(JobFailed):
                KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("Failed to fetch reservations" in msg for msg in cm.output))

    # ------------------------------------------------------------------ #
    # reservation KeaException result==2 (hook not loaded)                #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_reservation_kea_exception_result2_skips_gracefully(self, MockServer, mock_sync_lease, mock_cleanup):
        """KeaException with result==2 → WARNING logged (hook not loaded), no error counter increment."""
        server = _make_server()
        client = MagicMock()
        client.lease_get_all.return_value = ([], False)
        exc = KeaException({"result": 2, "text": "unsupported"})
        client.reservation_get_page.side_effect = exc
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
            KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("host_cmds" in msg for msg in cm.output))
        mock_cleanup.assert_not_called()

    # ------------------------------------------------------------------ #
    # reservation generic exception                                        #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_reservation_generic_exception_increments_errors(self, MockServer, mock_sync_lease, mock_cleanup):
        """Unexpected exception during reservation fetch → warning logged, errors incremented."""
        server = _make_server()
        client = MagicMock()
        client.lease_get_all.return_value = ([_LEASE4], False)
        client.reservation_get_page.side_effect = RuntimeError("unexpected")
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
            with self.assertRaises(JobFailed):
                KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("Unexpected error fetching reservations" in msg for msg in cm.output))

    # ------------------------------------------------------------------ #
    # per-reservation sync exception                                       #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_per_reservation_sync_exception_increments_errors(self, MockServer, mock_sync_lease, mock_cleanup):
        """Individual reservation sync failure → debug logged, doesn't abort batch."""
        server = _make_server()
        client = _make_client(leases4=[], reservations=[_RESV4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with patch("netbox_kea.sync.sync_reservation_to_netbox", side_effect=ValueError("oops")):
            with self.assertRaises(JobFailed):
                KeaIpamSyncJob(_make_job()).run()

        mock_cleanup.assert_not_called()

    # ------------------------------------------------------------------ #
    # unhandled exception in _sync_one_server                             #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", side_effect=RuntimeError("db gone"))
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_unhandled_exception_in_sync_one_server_is_caught(
        self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup
    ):
        """Unhandled exception from _sync_one_server is caught by run()'s outer loop."""
        server = _make_server()
        client = _make_client(leases4=[_LEASE4], reservations=[_RESV4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox.jobs", level="ERROR") as cm:
            with self.assertRaises(JobFailed):
                KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("Unhandled error syncing server" in msg for msg in cm.output))

    # ------------------------------------------------------------------ #
    # get_client failure in _sync_server_leases                           #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_get_client_failure_in_lease_sync_increments_errors(
        self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup
    ):
        """get_client() failure during lease sync increments errors but doesn't abort reservation sync."""
        server = _make_server()
        client = _make_client(reservations=[_RESV4])

        call_count = [0]

        def _get_client_side_effect(version):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("connection refused")
            return client

        server.get_client.side_effect = _get_client_side_effect
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
            with self.assertRaises(JobFailed):
                KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("Failed to fetch leases" in msg for msg in cm.output))
        mock_sync_resv.assert_called_once_with(_RESV4, cleanup=False)

    # ------------------------------------------------------------------ #
    # get_client failure in _sync_server_reservations                     #
    # ------------------------------------------------------------------ #

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_get_client_failure_in_reservation_sync_increments_errors(
        self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup
    ):
        """get_client() failure during reservation sync increments errors but lease sync still ran."""
        server = _make_server()
        client = _make_client(leases4=[_LEASE4])

        call_count = [0]

        def _get_client_side_effect(version):
            call_count[0] += 1
            if call_count[0] == 2:  # first call (lease) succeeds, second (resv) fails
                raise ValueError("connection refused")
            return client

        server.get_client.side_effect = _get_client_side_effect
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
            with self.assertRaises(JobFailed):
                KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("Unexpected error fetching reservations" in msg for msg in cm.output))
        mock_sync_lease.assert_called_once_with(_LEASE4, cleanup=False)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaIpamSyncJobKillSwitches(SimpleTestCase):
    """Tests for SyncConfig global kill-switch and Server.sync_enabled."""

    def _make_job(self):
        mock_job = MagicMock()
        mock_job.data = {}
        return mock_job

    @patch("netbox_kea.models.SyncConfig")
    @patch("netbox_kea.jobs._sync_one_server")
    @patch("netbox_kea.models.Server")
    def test_global_kill_switch_skips_all_servers(self, MockServer, mock_sync_one, MockSyncConfig):
        MockSyncConfig.get.return_value = MagicMock(sync_enabled=False, interval_minutes=5)
        server = MagicMock(dhcp4=True, dhcp6=False, sync_enabled=True)
        server.name = "kea"
        MockServer.objects.all.return_value = [server]

        job = KeaIpamSyncJob(self._make_job())
        job.run()

        mock_sync_one.assert_not_called()

    @patch("netbox_kea.models.SyncConfig")
    @patch("netbox_kea.jobs._sync_one_server")
    @patch("netbox_kea.models.Server")
    def test_per_server_disabled_skips_that_server(self, MockServer, mock_sync_one, MockSyncConfig):
        MockSyncConfig.get.return_value = MagicMock(sync_enabled=True, interval_minutes=5)
        server_a = MagicMock(dhcp4=True, dhcp6=False, sync_enabled=True)
        server_a.name = "enabled"
        server_b = MagicMock(dhcp4=True, dhcp6=False, sync_enabled=False)
        server_b.name = "disabled"
        MockServer.objects.all.return_value = [server_a, server_b]

        job = KeaIpamSyncJob(self._make_job())
        job.run()

        # _sync_one_server called once (for server_a only)
        self.assertEqual(mock_sync_one.call_count, 1)
        self.assertEqual(mock_sync_one.call_args[0][0], server_a)

    @patch("netbox_kea.models.SyncConfig")
    @patch("netbox_kea.jobs._sync_one_server")
    @patch("netbox_kea.models.Server")
    def test_server_pk_kwarg_filters_to_one_server(self, MockServer, mock_sync_one, MockSyncConfig):
        MockSyncConfig.get.return_value = MagicMock(sync_enabled=True, interval_minutes=5)
        server = MagicMock(pk=42, dhcp4=True, dhcp6=False, sync_enabled=True)
        server.name = "target"
        MockServer.objects.all.return_value = [server]
        MockServer.objects.filter.return_value = [server]

        job = KeaIpamSyncJob(self._make_job())
        job.run(server_pk=42)

        MockServer.objects.filter.assert_called_once_with(pk=42)
        mock_sync_one.assert_called_once()

    @patch("netbox_kea.models.SyncConfig")
    @patch("netbox_kea.jobs._sync_one_server")
    @patch("netbox_kea.models.Server")
    def test_server_pk_bypasses_per_server_sync_enabled(self, MockServer, mock_sync_one, MockSyncConfig):
        """Run Now (server_pk set) must sync even if server.sync_enabled is False."""
        MockSyncConfig.get.return_value = MagicMock(sync_enabled=True, interval_minutes=5)
        server = MagicMock(pk=42, dhcp4=True, dhcp6=False, sync_enabled=False)
        server.name = "disabled-server"
        MockServer.objects.filter.return_value = [server]

        job = KeaIpamSyncJob(self._make_job())
        job.run(server_pk=42)

        mock_sync_one.assert_called_once()

    @patch("netbox_kea.models.SyncConfig")
    @patch("netbox_kea.jobs._sync_one_server")
    @patch("netbox_kea.models.Server")
    def test_summary_written_on_global_kill_switch(self, MockServer, mock_sync_one, MockSyncConfig):
        """job.data['summary'] must be an empty list even when kill-switch aborts the run."""
        MockSyncConfig.get.return_value = MagicMock(sync_enabled=False, interval_minutes=5)
        MockServer.objects.all.return_value = []

        mock_job = self._make_job()
        job = KeaIpamSyncJob(mock_job)
        job.run()

        self.assertIn("summary", mock_job.data)
        self.assertEqual(mock_job.data["summary"], [])
        mock_job.save.assert_called_once_with(update_fields=["data"])

    @patch("netbox_kea.models.SyncConfig")
    @patch("netbox_kea.jobs._sync_one_server")
    @patch("netbox_kea.models.Server")
    def test_job_data_summary_written_after_run(self, MockServer, mock_sync_one, MockSyncConfig):
        MockSyncConfig.get.return_value = MagicMock(
            sync_enabled=True,
            interval_minutes=5,
            sync_leases_enabled=True,
            sync_reservations_enabled=True,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        server = MagicMock(pk=1, dhcp4=True, dhcp6=False, sync_enabled=True)
        server.name = "kea-prod"
        MockServer.objects.all.return_value = [server]

        def fake_sync(srv, sync_leases, sync_reservations, sync_prefixes, sync_ip_ranges, max_leases, stats):
            stats["created"] += 3
            stats["updated"] += 7

        mock_sync_one.side_effect = fake_sync

        mock_job = self._make_job()
        job = KeaIpamSyncJob(mock_job)
        job.run()

        self.assertIn("summary", mock_job.data)
        entry = mock_job.data["summary"][0]
        self.assertEqual(entry["name"], "kea-prod")
        self.assertEqual(entry["created"], 3)
        self.assertEqual(entry["updated"], 7)
        self.assertEqual(entry["errors"], 0)
        mock_job.save.assert_called_once_with(update_fields=["data"])

    @patch("netbox_kea.models.SyncConfig")
    @patch("netbox_kea.jobs._sync_one_server")
    @patch("netbox_kea.models.Server")
    def test_job_data_summary_written_when_data_is_none(self, MockServer, mock_sync_one, MockSyncConfig):
        """job.data['summary'] must be written even when job.data starts as None."""
        MockSyncConfig.get.return_value = MagicMock(
            sync_enabled=True,
            interval_minutes=5,
            sync_leases_enabled=True,
            sync_reservations_enabled=True,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        server = MagicMock(pk=1, name="kea", dhcp4=True, dhcp6=False, sync_enabled=True)
        MockServer.objects.all.return_value = [server]

        mock_job = MagicMock()
        mock_job.data = None  # production-realistic starting value
        job = KeaIpamSyncJob(mock_job)
        job.run()

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


class TestSyncServerPrefixesAndRanges(SimpleTestCase):
    """Tests for _sync_server_prefixes_and_ranges in jobs.py."""

    def _make_server(self, name="kea1"):
        server = MagicMock()
        server.name = name
        server.sync_vrf = None
        return server

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_syncs_each_subnet(self, mock_entry):
        """Happy path: two subnets → _sync_subnet_entry called twice."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        server = self._make_server()
        client = MagicMock()
        client.command.return_value = _DHCP4_CONFIG_RESPONSE
        server.get_client.return_value = client

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(server, version=4, sync_prefixes=True, sync_ip_ranges=True, stats=stats)

        self.assertEqual(mock_entry.call_count, 2)

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_shared_network_subnets_included(self, mock_entry):
        """Shared-network subnets are appended to the sync list."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        server = self._make_server()
        client = MagicMock()
        client.command.return_value = _DHCP4_CONFIG_WITH_SHARED
        server.get_client.return_value = client

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(server, version=4, sync_prefixes=True, sync_ip_ranges=True, stats=stats)
        # 1 top-level subnet + 1 in shared-network = 2
        self.assertEqual(mock_entry.call_count, 2)

    def test_get_client_exception_increments_errors(self):
        """get_client() failing → prefix_errors incremented, no exception propagated."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        server = self._make_server()
        server.get_client.side_effect = ValueError("no url")

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(server, version=4, sync_prefixes=True, sync_ip_ranges=True, stats=stats)
        self.assertEqual(stats["prefix_errors"], 1)
        self.assertEqual(stats["errors"], 0)

    def test_command_exception_increments_errors(self):
        """client.command() failing → prefix_errors incremented."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        server = self._make_server()
        client = MagicMock()
        client.command.side_effect = Exception("timeout")
        server.get_client.return_value = client

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(server, version=4, sync_prefixes=True, sync_ip_ranges=True, stats=stats)
        self.assertEqual(stats["prefix_errors"], 1)
        self.assertEqual(stats["errors"], 0)

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_malformed_config_response_increments_errors(self, mock_entry):
        """config-get returning a non-list → prefix_errors incremented, no subnet entries processed."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        server = self._make_server()
        client = MagicMock()
        # Return a string instead of list to trigger the malformed path
        client.command.return_value = "not-a-list"
        server.get_client.return_value = client

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(server, version=4, sync_prefixes=True, sync_ip_ranges=True, stats=stats)
        # malformed response → prefix error counted, no subnet entries processed
        mock_entry.assert_not_called()
        self.assertEqual(stats["prefix_errors"], 1)
        self.assertEqual(stats["errors"], 0)

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_vrf_forwarded_to_subnet_entry(self, mock_entry):
        """vrf kwarg is passed through to each _sync_subnet_entry call."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        server = self._make_server()
        fake_vrf = MagicMock()
        client = MagicMock()
        client.command.return_value = _DHCP4_CONFIG_RESPONSE
        server.get_client.return_value = client

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(
            server, version=4, sync_prefixes=True, sync_ip_ranges=True, vrf=fake_vrf, stats=stats
        )
        for c in mock_entry.call_args_list:
            self.assertEqual(c.args[3], fake_vrf)  # vrf is the 4th positional arg

    @patch("netbox_kea.jobs._sync_subnet_entry")
    def test_empty_subnet_list_no_calls(self, mock_entry):
        """Config with no subnets → _sync_subnet_entry never called."""
        from netbox_kea.jobs import _sync_server_prefixes_and_ranges

        server = self._make_server()
        client = MagicMock()
        client.command.return_value = [{"result": 0, "arguments": {"Dhcp4": {"subnet4": []}}}]
        server.get_client.return_value = client

        stats = {"created": 0, "updated": 0, "errors": 0, "prefix_errors": 0}
        _sync_server_prefixes_and_ranges(server, version=4, sync_prefixes=True, sync_ip_ranges=True, stats=stats)
        mock_entry.assert_not_called()


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
