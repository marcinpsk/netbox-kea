# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea/jobs.py — KeaIpamSyncJob."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

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
    return server


def _make_client(
    leases4: list[dict] | None = None,
    leases6: list[dict] | None = None,
    reservations: list[dict] | None = None,
    truncated: bool = False,
) -> MagicMock:
    """Return a mock KeaClient pre-configured with standard responses."""
    client = MagicMock()
    client.lease_get_all.return_value = (leases4 or [], truncated)
    client.reservation_get_page.return_value = (reservations or [], 0, 0)
    return client


class TestKeaIpamSyncJobRun(SimpleTestCase):
    """Tests for KeaIpamSyncJob.run()."""

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

    @override_settings(
        PLUGINS_CONFIG={
            **_PLUGINS_CONFIG,
            "netbox_kea": {**_PLUGINS_CONFIG["netbox_kea"], "sync_leases_enabled": False},
        }
    )
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_skips_leases_when_disabled(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """sync_leases_enabled=False → lease_get_all never called."""
        server = _make_server()
        client = _make_client(reservations=[_RESV4])
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

        client.lease_get_all.assert_not_called()
        mock_sync_lease.assert_not_called()
        mock_sync_resv.assert_called_once()

    @override_settings(
        PLUGINS_CONFIG={
            **_PLUGINS_CONFIG,
            "netbox_kea": {**_PLUGINS_CONFIG["netbox_kea"], "sync_reservations_enabled": False},
        }
    )
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_skips_reservations_when_disabled(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """sync_reservations_enabled=False → reservation_get_page never called."""
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

        KeaIpamSyncJob(_make_job()).run()

        # server2 lease was still synced
        mock_sync_lease.assert_called_once_with(_LEASE4, cleanup=False)

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_logs_truncation_warning(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """lease_get_all returns truncated=True → warning logged, sync still proceeds."""
        server = _make_server()
        client = _make_client(leases4=[_LEASE4], truncated=True)
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
            KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("truncated" in msg for msg in cm.output))
        mock_sync_lease.assert_called_once()

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_run_skips_host_cmds_absent(self, MockServer, mock_sync_lease, mock_cleanup):
        """reservation_get_page result=2 (host_cmds not loaded) → skip, no exception."""
        server = _make_server()
        client = MagicMock()
        client.lease_get_all.return_value = ([], False)
        client.reservation_get_page.side_effect = KeaException({"result": 2, "text": "unknown command"})
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        # Should not raise
        KeaIpamSyncJob(_make_job()).run()

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
        client.lease_get_all.return_value = ([_LEASE6], False)
        server.get_client.return_value = client
        MockServer.objects.all.return_value = [server]

        KeaIpamSyncJob(_make_job()).run()

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

        # Should not raise even though all lease syncs fail
        KeaIpamSyncJob(_make_job()).run()

        self.assertEqual(mock_sync_lease.call_count, 2)

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

            with self.assertLogs("netbox.jobs", level="WARNING") as cm:
                KeaIpamSyncJob(_make_job()).run()

        mock_cleanup.assert_not_called()
        self.assertTrue(any("skipping stale-IP cleanup" in msg for msg in cm.output))


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
