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

        # Should not raise even though all lease syncs fail
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

            with self.assertLogs("netbox.jobs", level="WARNING") as cm:
                KeaIpamSyncJob(_make_job()).run()

        mock_cleanup.assert_not_called()
        self.assertTrue(any("skipping stale-IP cleanup" in msg for msg in cm.output))

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

    @override_settings(
        PLUGINS_CONFIG={
            **_PLUGINS_CONFIG,
            "netbox_kea": {
                **_PLUGINS_CONFIG["netbox_kea"],
                "sync_leases_enabled": False,
                "sync_reservations_enabled": False,
            },
        }
    )
    @patch("netbox_kea.sync.cleanup_stale_ips_batch", return_value=0)
    @patch("netbox_kea.sync.sync_reservation_to_netbox", return_value=(MagicMock(), False))
    @patch("netbox_kea.sync.sync_lease_to_netbox", return_value=(MagicMock(), True))
    @patch("netbox_kea.models.Server")
    def test_both_sync_disabled_returns_early(self, MockServer, mock_sync_lease, mock_sync_resv, mock_cleanup):
        """When both sync_leases_enabled and sync_reservations_enabled are False, job returns immediately."""
        MockServer.objects.all.return_value = [_make_server()]

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
            KeaIpamSyncJob(_make_job()).run()

        self.assertTrue(any("Unexpected error fetching reservations" in msg for msg in cm.output))
        mock_sync_lease.assert_called_once_with(_LEASE4, cleanup=False)


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
