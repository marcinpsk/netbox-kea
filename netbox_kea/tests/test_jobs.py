# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Exercise the sync job through real ORM writes and a Kea HTTP transport stub.

Job tests assert IPAddress, Prefix, IPRange and summary results. Catalogue parsing
and malformed wire shapes belong to the catalogue and configuration tests.
"""

from __future__ import annotations

import ipaddress
import re
from contextlib import contextmanager, suppress
from unittest.mock import MagicMock, patch

import requests
from core.exceptions import JobFailed
from django.test import TestCase, override_settings
from ipam.models import IPAddress as NbIP
from ipam.models import IPRange, Prefix

from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.models import SyncConfig

from .kea_stub import _catalogue_responses_for_subnets, _res_page, _reservation_family, _subnet_list, queued, stub_kea
from .utils import plugins_config

_PLUGINS_CONFIG = plugins_config(stale_ip_cleanup="none")

# Variant used by tests that exercise the stale-IP cleanup path.
_PLUGINS_CONFIG_CLEANUP = plugins_config()

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


def _set_sync_config(**fields) -> None:
    """Change fields of the SyncConfig row that the migrations create."""
    config = SyncConfig.get()
    for name, value in fields.items():
        setattr(config, name, value)
    config.save()


def _make_job() -> MagicMock:
    """Create a minimal mock Job object for JobRunner.__init__."""
    mock_job = MagicMock()  # mock-ok: NetBox job-runner stand-in
    mock_job.data = {}
    mock_job.log = MagicMock()  # mock-ok: job log sink
    return mock_job


def _lease_page(leases: list[dict] | None) -> dict:
    """A ``lease{v}-get-page`` payload holding *leases* as a single page.

    ``count == len(leases) < per_page`` (250) so the real ``lease_get_all``
    pagination loop stops after this one page. An empty list is reported with
    Kea's "no leases" result code 3, exactly as a live daemon answers.
    """
    leases = list(leases or [])
    if not leases:
        return {"result": 3, "text": "0 lease(s) found"}
    return {"result": 0, "arguments": {"leases": leases, "count": len(leases)}}


def _reservation_subnets(reservations: list[dict], version: int) -> list[dict]:
    subnets: dict[int, str] = {}
    default_network = "2001:db8::/64" if version == 6 else "198.18.0.0/24"
    for host in reservations:
        if _reservation_family(host) != version or not host.get("subnet-id"):
            continue
        addresses = host.get("ip-addresses") or [host.get("ip-address", "")]
        address = next((value for value in addresses if value), None)
        if address:
            prefix_length = 64 if version == 6 else 24
            try:
                network = str(ipaddress.ip_network(f"{address}/{prefix_length}", strict=False))
            except ValueError:
                # Keep the Subnet verified so a malformed address is quarantined as an
                # invalid address, not as a Reservation in an unverified Scope.
                network = default_network
        else:
            network = default_network
        subnets[int(host["subnet-id"])] = network
    return [{"id": subnet_id, "subnet": network} for subnet_id, network in sorted(subnets.items())]


#: The Reservation page size ``KeaIpamSyncJob`` asks for. ``reservation_page`` only
#: reports a next cursor once a page fills it, so a partial traversal needs a full page.
_JOB_RESERVATION_PAGE_SIZE = 100
_PAGE_HOSTNAME = "reserved1"


def _full_reservation_page() -> list[dict]:
    """Return one full page of valid DHCPv4 Reservations that share a hostname."""
    return [
        {
            "ip-address": f"10.0.0.{100 + index}",
            "hw-address": f"11:22:33:44:00:{index:02x}",
            "hostname": _PAGE_HOSTNAME,
            "subnet-id": 1,
        }
        for index in range(_JOB_RESERVATION_PAGE_SIZE)
    ]


@contextmanager
def _patch_kea(
    *,
    leases4: list[dict] | None = None,
    leases6: list[dict] | None = None,
    reservations: list[dict] | None = None,
    responses: dict | None = None,
):
    """Stub only the Kea HTTP boundary; the real ``KeaClient`` and all ORM/sync run.

    Registers the commands ``KeaIpamSyncJob`` issues — ``config-get``,
    ``lease{4,6}-get-page`` and ``reservation-get-page`` — so the real
    ``lease_get_all`` / Reservation Snapshot pagination and ``command()``
    response parsing actually execute. A broken parser can no longer stay green
    behind a method-level ``MagicMock``, and truncation is driven by the real
    ``max_leases`` cap rather than a faked flag.

    Pass *responses* to override or add a command for one test — e.g. a
    ``config-get`` carrying a subnet, a ``reservation-get-page`` returning a Kea
    error code, or an exception instance raised at the HTTP boundary.
    """
    reservation_rows = list(reservations or [])

    def config_get(body: dict) -> dict:
        service = (body.get("service") or ["dhcp4"])[0]
        version = 6 if service == "dhcp6" else 4
        root = f"Dhcp{version}"
        subnet_key = f"subnet{version}"
        return {
            "result": 0,
            "arguments": {root: {subnet_key: _reservation_subnets(reservation_rows, version), "shared-networks": []}},
        }

    def subnet_list(body: dict) -> dict:
        version = 6 if body.get("command") == "subnet6-list" else 4
        subnets = _reservation_subnets(reservation_rows, version)
        return {"result": 0, "arguments": {"subnets": subnets}} if subnets else {"result": 3}

    def reservation_page(body: dict) -> dict:
        version = 6 if (body.get("service") or ["dhcp4"])[0] == "dhcp6" else 4
        hosts = [host for host in reservation_rows if _reservation_family(host) == version]
        return _res_page(hosts)

    registry: dict = {
        "config-get": config_get,
        "subnet4-list": subnet_list,
        "subnet6-list": subnet_list,
        "lease4-get-page": _lease_page(leases4),
        "lease6-get-page": _lease_page(leases6),
        "reservation-get-page": reservation_page,
    }
    if responses:
        registry.update(responses)
    with stub_kea(registry) as stub:
        yield stub


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaIpamSyncJobRun(TestCase):
    """Run the job with real sync helpers and ORM writes; stub only Kea HTTP."""

    # ── scaffolding ──────────────────────────────────────────────────────────

    def _run(self) -> MagicMock:
        """Run the job, swallowing JobFailed; return the mock job object."""
        job = _make_job()
        with suppress(JobFailed):
            KeaIpamSyncJob(job).run()
        return job

    def _run_raises(self) -> MagicMock:
        """Run the job, assert JobFailed is raised, and return the mock job object."""
        job = _make_job()
        with self.assertRaises(JobFailed):
            KeaIpamSyncJob(job).run()
        return job

    def _make_db_server(self, **kwargs):
        from netbox_kea.tests.utils import _make_db_server

        return _make_db_server(**kwargs)

    # ── basic lease sync ──────────────────────────────────────────────────────

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
    def test_malformed_foreign_lease_hostname_is_excluded_from_keep_set(self):
        self._make_db_server(dhcp6=False)
        foreign = NbIP.objects.create(address="198.18.0.20/32", status="active", description="Manual address")
        stale = NbIP.objects.create(
            address="198.18.0.29/32",
            status="dhcp",
            dns_name="valid.example.invalid",
            description="[kea-sync: lease]",
        )
        valid_lease = {"ip-address": "198.18.0.30", "subnet-id": 1, "hostname": "valid.example.invalid"}
        for hostname in (["host.example.invalid"], {"name": "host.example.invalid"}):
            with self.subTest(hostname=hostname):
                lease = {"ip-address": "198.18.0.20", "subnet-id": 1, "hostname": hostname}
                job = _make_job()
                with _patch_kea(leases4=[lease, valid_lease]), self.assertRaises(JobFailed):
                    KeaIpamSyncJob(job).run()
                self.assertEqual(job.data["summary"][0]["errors"], 1)
                self.assertEqual(job.data["summary"][0]["conflicts"], 0)
                foreign.refresh_from_db()
                self.assertEqual(str(foreign.address), "198.18.0.20/32")
                self.assertEqual(foreign.status, "active")
                self.assertEqual(foreign.dns_name, "")
                self.assertEqual(foreign.description, "Manual address")
                self.assertTrue(NbIP.objects.filter(pk=stale.pk).exists())
                self.assertTrue(NbIP.objects.filter(address__net_host="198.18.0.30").exists())

    def test_creates_ip_from_lease(self):
        """Lease sync creates an IPAddress row in the real DB."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())

    def test_lease_ip_status_is_dhcp(self):
        """Synced lease IP has status='dhcp'."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        ip = IPAddress.objects.filter(address__net_host="10.0.0.1").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "dhcp")

    def test_creates_v6_ip_from_lease(self):
        """DHCPv6 lease creates an IPv6 IPAddress row."""
        self._make_db_server(dhcp4=False, dhcp6=True)
        with _patch_kea(leases6=[_LEASE6]):
            self._run()
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__net_host="2001:db8::1").exists())

    def test_dual_protocol_creates_both_v4_and_v6(self):
        """Server with dhcp4=True and dhcp6=True creates both address families."""
        self._make_db_server(dhcp4=True, dhcp6=True)
        with _patch_kea(leases4=[_LEASE4], leases6=[_LEASE6]):
            self._run()
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())
        self.assertTrue(IPAddress.objects.filter(address__net_host="2001:db8::1").exists())

    # ── reservation sync ──────────────────────────────────────────────────────

    def test_creates_reserved_ip_from_reservation(self):
        """Reservation sync creates an IPAddress with status='reserved'."""
        _set_sync_config(
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=True,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        self._make_db_server(sync_leases_enabled=False)
        with _patch_kea(reservations=[_RESV4]):
            self._run()
        from ipam.models import IPAddress

        ip = IPAddress.objects.filter(address__net_host="10.0.0.100").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_reservation_updates_existing_address_and_summary(self):
        self._make_db_server(dhcp6=False, sync_prefixes_enabled=False, sync_ip_ranges_enabled=False)
        existing = NbIP.objects.create(
            address="198.18.0.20/24",
            status="reserved",
            dns_name="old.example.invalid",
            description="[kea-sync: reservation]",
        )
        reservation = {
            "ip-address": "198.18.0.20",
            "hw-address": "00:11:22:33:44:55",
            "hostname": "updated.example.invalid",
            "subnet-id": 1,
        }
        with _patch_kea(reservations=[reservation]):
            job = _make_job()
            KeaIpamSyncJob(job).run()
        existing.refresh_from_db()
        self.assertEqual(existing.dns_name, "updated.example.invalid")
        self.assertEqual(existing.status, "reserved")
        self.assertEqual(NbIP.objects.count(), 1)
        self.assertEqual(job.data["summary"][0]["created"], 0)
        self.assertEqual(job.data["summary"][0]["updated"], 1)
        self.assertEqual(job.data["summary"][0]["errors"], 0)

    def test_ipv6_prefix_only_reservation_is_skipped(self):
        self._make_db_server(dhcp4=False, dhcp6=True, sync_prefixes_enabled=False, sync_ip_ranges_enabled=False)
        reservation = {"duid": "00:01:00:01:12:34", "subnet-id": 12, "prefixes": ["2001:db8:1::/64"]}
        with _patch_kea(reservations=[reservation]):
            job = _make_job()
            KeaIpamSyncJob(job).run()
        self.assertFalse(NbIP.objects.exists())
        self.assertEqual(job.data["summary"][0]["created"], 0)
        self.assertEqual(job.data["summary"][0]["errors"], 0)
        self.assertEqual(job.data["summary"][0]["skipped"], 1)

    def test_both_lease_and_reservation_synced(self):
        """Both lease and reservation IPs are persisted in the same run."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4], reservations=[_RESV4]):
            self._run()
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.100").exists())

    # ── disabled flags → no DB writes ────────────────────────────────────────

    def test_skips_leases_when_sync_leases_disabled(self):
        """sync_leases_enabled=False → no IPAddress from lease created."""
        _set_sync_config(
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())

    def test_skips_reservations_when_sync_reservations_disabled(self):
        """sync_reservations_enabled=False → no reserved IPAddress created."""
        _set_sync_config(
            interval_minutes=5,
            sync_leases_enabled=True,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        self._make_db_server()
        with _patch_kea(leases4=[], reservations=[_RESV4]):
            self._run()
        from ipam.models import IPAddress

        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.100").exists())

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
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())

    def test_invalid_lease_page_does_not_sync_a_partial_batch(self):
        """An invalid paged response is rejected before any lease is synced."""
        self._make_db_server()
        bad_lease = {**_LEASE4, "ip-address": "not-an-ip"}
        good_lease = {**_LEASE4, "ip-address": "10.0.0.2"}
        with _patch_kea(leases4=[bad_lease, good_lease]):
            self._run_raises()  # errors > 0 → JobFailed
        from ipam.models import IPAddress

        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.2").exists())

    # ── idempotency ────────────────────────────────────────────────────────

    def test_second_sync_does_not_create_duplicate(self):
        """Syncing the same lease twice creates exactly one IPAddress row."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertEqual(IPAddress.objects.filter(address__net_host="10.0.0.1").count(), 1)

    def test_second_sync_updates_existing_ip_dns_name(self):
        """A subsequent sync with a new hostname updates dns_name in place."""
        self._make_db_server()
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        updated_lease = {**_LEASE4, "hostname": "updated-hostname.example.com"}
        with _patch_kea(leases4=[updated_lease]):
            self._run()
        from ipam.models import IPAddress

        ip = IPAddress.objects.filter(address__net_host="10.0.0.1").first()
        self.assertEqual(ip.dns_name, "updated-hostname.example.com")

    # ── truncation ─────────────────────────────────────────────────────────

    @override_settings(PLUGINS_CONFIG=plugins_config(sync_max_leases_per_server=1))
    def test_truncation_warning_skips_cleanup(self):
        """Truncated lease fetch → warning logged and stale IP not removed.

        Two leases with ``max_leases=1`` genuinely exceed the cap, so the real
        ``lease_get_all`` drops the overflow lease and returns ``truncated=True``
        — the real truncation path, not a faked flag or a complete-dataset edge case.
        """
        from ipam.models import IPAddress

        self._make_db_server()
        stale = IPAddress.objects.create(
            address="10.0.0.99/32",
            status="dhcp",
            dns_name="host1",
            description="[kea-sync: lease]",
        )
        overflow = {**_LEASE4, "ip-address": "10.0.0.2", "hostname": "host2"}
        with _patch_kea(leases4=[_LEASE4, overflow]):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING") as cm:
                self._run()
        self.assertTrue(any("truncated" in msg for msg in cm.output))
        # The overflow lease past the cap was genuinely dropped, not synced.
        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.2").exists())
        # Cleanup must be skipped when fetch was truncated.
        self.assertTrue(IPAddress.objects.filter(pk=stale.pk).exists())

    # ── host_cmds hook absent ─────────────────────────────────────────────

    def test_host_cmds_absent_warning_logged(self):
        """result=2 (unknown command) from reservation-get-page → WARNING about host_cmds.

        The real ``command()`` turns the result-2 code into a ``KeaException``,
        which the Reservation phase reads as "host_cmds hook not loaded".
        """
        self._make_db_server()
        with _patch_kea(
            leases4=[_LEASE4],
            responses={"reservation-get-page": {"result": 2, "text": "unknown command"}},
        ):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING") as cm:
                self._run()
        self.assertTrue(any("host_cmds" in msg for msg in cm.output))

    def test_identity_oserror_does_not_stop_the_other_family(self):
        self._make_db_server(dhcp4=True, dhcp6=True)
        lease = {**_LEASE6, "ip-address": "2001:db8::123", "hostname": "lease.example.invalid"}
        with _patch_kea(
            leases6=[lease], responses={"subnet4-list": OSError("TLS CA certificate file is unavailable")}
        ) as kea:
            self._run()
        self.assertTrue(NbIP.objects.filter(address__net_host="2001:db8::123", status="dhcp").exists())
        self.assertIn("subnet6-list", kea.commands())
        self.assertIn("lease6-get-page", kea.commands())

    def test_catalogue_failure_is_not_reported_as_missing_host_cmds(self):
        self._make_db_server(dhcp6=False)
        with _patch_kea(
            leases4=[_LEASE4],
            responses={"subnet4-list": {"result": 2, "text": "unknown command"}},
        ) as kea:
            with self.assertLogs("netbox_kea.jobs", level="WARNING") as cm:
                job = self._run()

        self.assertEqual([entry["errors"] for entry in job.data["summary"]], [1])
        self.assertTrue(any("Subnet Catalogue unavailable" in message for message in cm.output))
        self.assertFalse(any("host_cmds" in message for message in cm.output))
        self.assertNotIn("reservation-get-page", kea.commands())

    def test_absent_host_cmds_is_skipped_not_counted_as_an_error(self):
        """A server without host_cmds has no reservations to sync, so the job must not fail.

        A missing hook once counted as a failed read, so every sync run against such a
        server ended in ``JobFailed``.
        """
        self._make_db_server()
        with _patch_kea(
            leases4=[_LEASE4],
            responses={"reservation-get-page": {"result": 2, "text": "unknown command"}},
        ):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING"):
                # Must not raise JobFailed: nothing failed, the feature is absent.
                KeaIpamSyncJob(_make_job()).run()

    def test_absent_host_cmds_reports_zero_errors_in_the_summary(self):
        self._make_db_server()
        with _patch_kea(
            leases4=[_LEASE4],
            responses={"reservation-get-page": {"result": 2, "text": "unknown command"}},
        ):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING"):
                job = self._run()

        self.assertEqual([entry["errors"] for entry in job.data["summary"]], [0])

    def test_a_failed_reservation_read_records_its_traceback(self):
        """The job continues past the failure, so the traceback is the only record of it.

        The message alone named the exception text and lost the stack that produced it.
        """
        self._make_db_server(dhcp6=False)
        with _patch_kea(
            leases4=[_LEASE4],
            responses={"reservation-get-page": {"result": 1, "text": "internal error"}},
        ):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING") as logs:
                self._run()

        failures = [
            record
            for record in logs.records
            if "the Reservation snapshot failed" in record.getMessage() and record.exc_info is not None
        ]
        self.assertTrue(failures, [record.getMessage() for record in logs.records])

    def test_a_failed_reservation_read_is_still_counted_as_an_error(self):
        """Only the missing-hook case is a skip; a real read failure must still fail the job."""
        # DHCPv4 only, so the count is exactly one failed reservation read.
        self._make_db_server(dhcp6=False)
        with _patch_kea(
            leases4=[_LEASE4],
            responses={"reservation-get-page": {"result": 1, "text": "internal error"}},
        ):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING"):
                job = self._run()

        self.assertEqual([entry["errors"] for entry in job.data["summary"]], [1])

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
    def test_cleanup_skipped_when_host_cmds_absent(self):
        """Reservation phase skipped (host_cmds absent) → cleanup_safe=False → stale IP preserved."""
        from ipam.models import IPAddress

        self._make_db_server()
        stale = IPAddress.objects.create(
            address="10.0.0.99/32",
            status="dhcp",
            dns_name="host1",
            description="[kea-sync: lease]",
        )

        with _patch_kea(
            leases4=[_LEASE4],
            responses={"reservation-get-page": {"result": 2, "text": "unknown command"}},
        ):
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
            description="[kea-sync: lease]",
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
            description="[kea-sync: lease]",
        )
        bad_lease = {**_LEASE4, "ip-address": "not-an-ip"}
        with _patch_kea(leases4=[bad_lease]):
            self._run_raises()
        self.assertTrue(IPAddress.objects.filter(pk=stale.pk).exists())

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
    def test_an_incomplete_snapshot_syncs_valid_records_but_preserves_stale_ips(self):
        """Kea did not show every Reservation, so the run is additive only.

        The records Kea did return are still valid and are synchronized. The keep-set
        they form is not complete, so stale cleanup must not delete anything.
        """
        from ipam.models import IPAddress

        self._make_db_server(dhcp6=False)
        stale = IPAddress.objects.create(
            address="10.0.0.99/32",
            status="reserved",
            dns_name=_PAGE_HOSTNAME,
            description="[kea-sync: reservation]",
        )

        # A full page yields a cursor; the page it points at never arrives.
        hosts = _full_reservation_page()
        with _patch_kea(
            leases4=[_LEASE4],
            reservations=hosts,
            responses={
                "reservation-get-page": queued(
                    _res_page(hosts, next_from=len(hosts), next_source=1),
                    RuntimeError("page fetch failed"),
                )
            },
        ):
            with self.assertLogs("netbox_kea.jobs", level="WARNING"):
                self._run()

        addresses = sorted(str(ip.address) for ip in IPAddress.objects.all())
        self.assertTrue(
            any(address.startswith("10.0.0.100/") for address in addresses),
            f"The valid Reservations were not synchronized; NetBox holds {addresses}",
        )
        self.assertTrue(IPAddress.objects.filter(pk=stale.pk).exists())

    # ── per-server sync_enabled toggle ────────────────────────────────────

    def test_per_server_sync_enabled_false_skips_server(self):
        """Server with sync_enabled=False is not synced in a scheduled run."""
        self._make_db_server(sync_enabled=False)
        with _patch_kea(leases4=[_LEASE4]):
            self._run()
        from ipam.models import IPAddress

        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())

    # ── reservation pagination ────────────────────────────────────────────

    def test_reservation_pagination_fetches_all_pages(self):
        """Multi-page reservation responses are fully iterated and all rows synced."""
        from ipam.models import IPAddress

        self._make_db_server()
        resv1 = {**_RESV4, "ip-address": "10.0.0.100"}
        resv2 = {**_RESV4, "ip-address": "10.0.0.101", "hw-address": "22:33:44:55:66:77"}

        def _paged(body):
            # Drive the real (from, source-index) cursor loop: page 1 hands out a
            # non-zero cursor, page 2 exhausts the source. Stateless on ``from`` so
            # it answers both the pre-fetch and main-sync passes identically.
            if body["arguments"]["from"] == 0:
                return _res_page([resv1], next_from=1)
            return _res_page([resv2])

        with _patch_kea(
            leases4=[],
            reservations=[resv1, resv2],
            responses={"reservation-get-page": _paged},
        ):
            self._run()

        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.100").exists())
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.101").exists())

    @override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG_CLEANUP)
    def test_reservation_pagination_preserves_valid_rows_before_a_later_failure(self):
        """A later page failure keeps earlier rows and suppresses stale cleanup."""
        from ipam.models import IPAddress

        self._make_db_server(dhcp6=False)
        first_page = [
            {
                "subnet-id": 1,
                "hw-address": f"02:00:00:00:00:{index:02x}",
                "ip-address": f"198.18.0.{index + 1}",
                "hostname": "partial-snapshot.example.invalid",
            }
            for index in range(100)
        ]
        stale = IPAddress.objects.create(
            address="198.18.0.200/32",
            status="reserved",
            dns_name="partial-snapshot.example.invalid",
            description="[kea-sync: reservation]",
        )

        with _patch_kea(
            leases4=[],
            reservations=first_page,
            responses={
                "reservation-get-page": queued(
                    _res_page(first_page, next_from=100, next_source=1),
                    requests.ConnectionError("later page unavailable"),
                )
            },
        ):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING") as logs:
                self._run()

        self.assertTrue(IPAddress.objects.filter(address__net_host="198.18.0.1").exists())
        self.assertTrue(IPAddress.objects.filter(address__net_host="198.18.0.100").exists())
        self.assertTrue(IPAddress.objects.filter(pk=stale.pk).exists())
        self.assertTrue(any("the Reservation snapshot is incomplete" in message for message in logs.output))
        self.assertFalse(any("malformed Reservation" in message for message in logs.output))

    # ── reservation KeaException (non-result-2) ───────────────────────────

    def test_reservation_kea_error_increments_errors_and_raises_job_failed(self):
        """result=1 from reservation-get-page → KeaException → errors++ → JobFailed."""
        self._make_db_server()
        with _patch_kea(
            leases4=[_LEASE4],
            responses={"reservation-get-page": {"result": 1, "text": "internal error"}},
        ):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING") as cm:
                self._run_raises()
        self.assertTrue(any("the Reservation snapshot failed" in msg for msg in cm.output))

    # ── per-reservation sync exception ───────────────────────────────────

    def test_per_reservation_error_does_not_abort_batch(self):
        """An unparseable reservation does not stop other reservations from syncing."""
        from ipam.models import IPAddress

        self._make_db_server()
        bad_resv = {**_RESV4, "ip-address": "not-an-ip"}
        good_resv = {**_RESV4, "ip-address": "10.0.0.102", "hw-address": "33:44:55:66:77:88"}
        with _patch_kea(leases4=[], reservations=[bad_resv, good_resv]):
            self._run_raises()  # errors > 0 → JobFailed
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.102").exists())

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

    def test_foreign_ip_skipped_and_counted_in_summary(self):
        """A foreign NetBox IP is never overwritten by the background job, and is counted.

        Issue #64 acceptance: a NetBox IP with description 'Router loopback' is
        never overwritten by a bulk/background sync run, and the conflict count is
        visible in the per-server summary.
        """
        from ipam.models import IPAddress

        # v4-only so the shared Kea fake doesn't replay the v4 reservation under v6.
        self._make_db_server(name="kea-conflict", dhcp6=False)
        IPAddress.objects.create(address="10.0.0.100/32", status="active", description="Router loopback")
        with _patch_kea(leases4=[], reservations=[_RESV4]):
            mock_job = _make_job()
            with suppress(JobFailed):
                KeaIpamSyncJob(mock_job).run()

        # Foreign IP left exactly as the operator set it.
        ip = IPAddress.objects.get(address="10.0.0.100/32")
        self.assertEqual(ip.status, "active")
        self.assertEqual(ip.description, "Router loopback")

        # Conflict surfaced in the per-server summary.
        entry = next(e for e in mock_job.data["summary"] if e["name"] == "kea-conflict")
        self.assertEqual(entry["conflicts"], 1)

    def test_same_foreign_ip_in_both_phases_counts_once(self):
        """One foreign IP seen by both the lease and reservation phase is ONE conflict.

        The phases each appended to their own accumulator and added ``len()`` to the
        same per-server counter, so a host that has both a lease and a reservation was
        reported twice — inflating a number the operator cannot reconcile against
        anything. The summary also names the addresses so the conflict is diagnosable.
        """
        from ipam.models import IPAddress

        self._make_db_server(name="kea-dupe", dhcp6=False)
        IPAddress.objects.create(address="10.0.0.100/32", status="active", description="Router loopback")
        lease_for_same_ip = {**_LEASE4, "ip-address": "10.0.0.100", "hostname": "reserved1"}
        with _patch_kea(leases4=[lease_for_same_ip], reservations=[_RESV4]):
            mock_job = _make_job()
            with suppress(JobFailed):
                KeaIpamSyncJob(mock_job).run()

        entry = next(e for e in mock_job.data["summary"] if e["name"] == "kea-dupe")
        self.assertEqual(entry["conflicts"], 1)
        self.assertEqual(entry["conflict_sample"], ["10.0.0.100"])

    def test_address_less_reservation_reported_as_skipped_not_failed(self):
        """An identifier-only reservation must not fail the nightly job (#110).

        ``sync_reservation_to_netbox`` raises on a reservation with no address; counting
        that as an error made ``run()`` raise ``JobFailed`` for a perfectly legal Kea
        configuration, with the reason visible only at debug level.
        """
        self._make_db_server(name="kea-skip", dhcp6=False)
        address_less = {"hw-address": "11:22:33:44:55:66", "hostname": "printer-1", "subnet-id": 1}
        with _patch_kea(leases4=[], reservations=[address_less]):
            mock_job = _make_job()
            KeaIpamSyncJob(mock_job).run()  # must NOT raise JobFailed

        entry = next(e for e in mock_job.data["summary"] if e["name"] == "kea-skip")
        self.assertEqual(entry["errors"], 0)
        self.assertEqual(entry["skipped"], 1)

    def test_config_get_failure_falls_back_to_prefix_match_and_logs(self):
        """When config-get fails, leases still sync (NetBox-prefix fallback) and it's logged.

        The Kea subnet-id → prefix-length map is the authoritative mask source, but
        a config-get failure must degrade gracefully to NetBox prefix matching rather
        than dropping the lease — and surface an INFO log so the degradation is visible.
        """
        from ipam.models import IPAddress

        self._make_db_server(name="kea-noconfig", dhcp6=False)

        with _patch_kea(
            leases4=[_LEASE4],
            responses={"config-get": RuntimeError("config-get boom")},
        ):
            with self.assertLogs("netbox_kea.jobs", level="INFO") as cm:
                self._run()

        # Lease IP still created despite the missing Kea subnet config.
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())
        # Fallback is surfaced to the operator.
        self.assertTrue(any("fall back to NetBox prefix matching" in m for m in cm.output))

    def test_catalogue_prefix_length_masks_lease_without_netbox_prefix(self):
        """A verified Catalogue supplies the lease mask before any Prefix exists."""
        from ipam.models import IPAddress, Prefix

        # Prefix/range sync off so the /24 cannot come from a *created* NetBox Prefix.
        self._make_db_server(
            name="kea-mask",
            dhcp6=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        lease = {"ip-address": "10.0.0.50", "hostname": "masked-host", "subnet-id": 1}
        responses = _catalogue_responses_for_subnets(4, [{"id": 1, "subnet": "10.0.0.0/24"}])
        with _patch_kea(leases4=[lease], responses=responses):
            self._run()

        ip = IPAddress.objects.get(address__net_host="10.0.0.50")
        self.assertEqual(str(ip.address), "10.0.0.50/24")
        # The Catalogue supplies the mask without creating a NetBox Prefix.
        self.assertFalse(Prefix.objects.filter(prefix="10.0.0.0/24").exists())

    # ── reservation generic exception ─────────────────────────────────────

    def test_reservation_generic_exception_increments_errors(self):
        """A transport error on reservation-get-page → warning logged → JobFailed."""
        self._make_db_server()
        with _patch_kea(
            leases4=[_LEASE4],
            responses={"reservation-get-page": RuntimeError("unexpected")},
        ):
            with self.assertLogs("netbox_kea.ipam_reconciliation", level="WARNING") as cm:
                self._run_raises()
        self.assertTrue(any("the Reservation snapshot failed" in msg for msg in cm.output))

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
                with patch(
                    "netbox_kea.sync.cleanup_stale_ips_batch", side_effect=RuntimeError("db gone"), autospec=True
                ):
                    with self.assertLogs("netbox.jobs", level="ERROR") as cm:
                        job = self._run_raises()
        self.assertTrue(any("Unhandled error syncing server" in msg for msg in cm.output))

        # This entry is on an exception path a clean run never reaches, so it is the
        # one self.logger call TestJobLogRendersValues cannot see. See that class for
        # why eager formatting is required.
        from core.dataclasses import JobLogEntry

        entries = [JobLogEntry.from_logrecord(call.args[0]).message for call in job.log.call_args_list]
        failures = [m for m in entries if "Unhandled error syncing server" in m]
        self.assertTrue(failures, entries)
        self.assertNotIn("%s", failures[0])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestJobLogRendersValues(TestCase):
    """The job log must show real values, never unsubstituted format placeholders.

    NetBox's JobLogEntry.from_logrecord stores ``record.msg`` and drops
    ``record.args``, so a ``self.logger`` call using lazy %-formatting would put a
    literal "%s" in front of the user. Every self.logger call in jobs.py therefore
    has to interpolate eagerly. ruff's G004 asks for the opposite and is disabled
    for jobs.py in pyproject.toml; this test is what makes that safe.
    """

    def _job_log_messages(self) -> list[str]:
        """Run the job and render its records exactly as NetBox would."""
        from core.dataclasses import JobLogEntry

        from netbox_kea.tests.utils import _make_db_server

        _make_db_server(name="kea-logfmt")
        job = _make_job()
        with suppress(JobFailed), _patch_kea(leases4=[_LEASE4]):
            KeaIpamSyncJob(job).run()
        return [JobLogEntry.from_logrecord(call.args[0]).message for call in job.log.call_args_list]

    def test_no_unsubstituted_placeholders(self):
        """No rendered entry may still contain a %-placeholder."""
        messages = self._job_log_messages()
        self.assertTrue(messages, "the job logged nothing, so this test proves nothing")
        offenders = [m for m in messages if re.search(r"%[srd]", m)]
        self.assertEqual(offenders, [], f"job log entries kept a format placeholder: {offenders}")

    def test_server_name_reaches_the_job_log(self):
        """The server name must appear as a value, which only eager formatting gives."""
        messages = self._job_log_messages()
        self.assertTrue(any("kea-logfmt" in m for m in messages), messages)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaIpamSyncJobKillSwitches(TestCase):
    """Tests for SyncConfig global kill-switch, per-server sync_enabled, and job metadata.

    Uses the real ORM — Server rows and SyncConfig are created in the test DB.
    Only Kea HTTP calls are mocked via ``_patch_kea``.
    """

    def _run(self) -> MagicMock:
        job = _make_job()
        with suppress(JobFailed):
            KeaIpamSyncJob(job).run()
        return job

    def _make_db_server(self, **kwargs):
        from netbox_kea.tests.utils import _make_db_server

        return _make_db_server(**kwargs)

    # ── global kill-switch ────────────────────────────────────────────────

    def test_global_kill_switch_creates_no_ips(self):
        """SyncConfig.sync_enabled=False → no IPs synced, no Kea calls made."""
        _set_sync_config(interval_minutes=5, sync_enabled=False)
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

        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.20").exists())
        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())

    def test_server_pk_bypasses_per_server_sync_enabled(self):
        """run(server_pk=X) syncs server X even when its sync_enabled=False."""
        server = self._make_db_server(sync_enabled=False)
        with _patch_kea(leases4=[_LEASE4]):
            KeaIpamSyncJob(_make_job()).run(server_pk=server.pk)
        from ipam.models import IPAddress

        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.1").exists())

    # ── job metadata (job.data) ────────────────────────────────────────────

    def test_summary_written_on_global_kill_switch(self):
        """job.data['summary'] is an empty list even when kill-switch aborts the run."""
        _set_sync_config(interval_minutes=5, sync_enabled=False)
        mock_job = _make_job()
        KeaIpamSyncJob(mock_job).run()
        self.assertIn("summary", mock_job.data)
        self.assertEqual(mock_job.data["summary"], [])
        mock_job.save.assert_called_once_with(update_fields=["data"])

    def test_job_data_summary_written_when_data_is_none(self):
        """job.data['summary'] is written even when job.data starts as None."""
        _set_sync_config(
            interval_minutes=5,
            sync_leases_enabled=False,
            sync_reservations_enabled=False,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=False,
        )
        mock_job = _make_job()
        mock_job.data = None
        KeaIpamSyncJob(mock_job).run()
        self.assertIsInstance(mock_job.data, dict)
        self.assertIn("summary", mock_job.data)
        mock_job.save.assert_called_once_with(update_fields=["data"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncIntervalFromSyncConfig(TestCase):
    """The stored SyncConfig.interval_minutes sets every periodic schedule; PLUGINS_CONFIG does not."""

    def _db_job(self, *, interval: int | None):
        import uuid

        from core.models import Job
        from django.utils import timezone

        return Job.objects.create(
            name=KeaIpamSyncJob.name,
            status="scheduled" if interval else "pending",
            scheduled=timezone.now() if interval else None,
            interval=interval,
            job_id=uuid.uuid4(),
        )

    def _successors(self, job):
        from core.models import Job

        return Job.objects.filter(name=KeaIpamSyncJob.name).exclude(pk=job.pk)

    def test_enqueue_once_schedules_the_stored_interval(self):
        """The rqworker passes the registry interval; the Job gets the stored one."""
        from core.models import Job

        _set_sync_config(interval_minutes=17)
        KeaIpamSyncJob.enqueue_once(interval=5)
        self.assertEqual(list(Job.objects.filter(name=KeaIpamSyncJob.name).values_list("interval", flat=True)), [17])

    def test_a_positional_interval_is_replaced_by_the_stored_one(self):
        """NetBox's signature is (instance, schedule_at, interval), so a positional interval is valid."""
        from core.models import Job

        _set_sync_config(interval_minutes=17)
        KeaIpamSyncJob.enqueue_once(None, None, 5)
        self.assertEqual(list(Job.objects.filter(name=KeaIpamSyncJob.name).values_list("interval", flat=True)), [17])

    def test_periodic_run_schedules_its_successor_with_the_stored_interval(self):
        """An interval saved after the job was scheduled applies to the next successor."""
        _set_sync_config(interval_minutes=23, sync_enabled=False)
        job = self._db_job(interval=5)
        KeaIpamSyncJob.handle(job)
        self.assertEqual(list(self._successors(job).values_list("interval", flat=True)), [23])

    def test_one_off_run_stays_one_off(self):
        """A Sync Now job has no interval, so it gets none and schedules no successor."""
        _set_sync_config(interval_minutes=23, sync_enabled=False)
        job = self._db_job(interval=None)
        KeaIpamSyncJob.handle(job)
        job.refresh_from_db()
        self.assertIsNone(job.interval)
        self.assertFalse(self._successors(job).exists())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestJobSaveFailure(TestCase):
    """A JobRunner persistence failure is logged after the real job flow finishes."""

    def test_save_exception_is_caught_and_logged(self):
        config = SyncConfig.get()
        config.sync_enabled = False
        config.save()
        job = _make_job()
        job.save.side_effect = RuntimeError("job persistence unavailable")
        with self.assertLogs("netbox_kea.jobs", level="ERROR") as logs:
            KeaIpamSyncJob(job).run()
        self.assertEqual(job.data["summary"], [])
        job.save.assert_called_once_with(update_fields=["data"])
        self.assertTrue(any("Failed to persist sync job summary data" in message for message in logs.output))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetCatalogueJob(TestCase):
    def setUp(self):
        from netbox_kea.tests.utils import _make_db_server

        self.server = _make_db_server(dhcp6=False)
        self.config = SyncConfig.get()
        self.config.sync_leases_enabled = False
        self.config.sync_reservations_enabled = False
        self.config.sync_prefixes_enabled = True
        self.config.sync_ip_ranges_enabled = True
        self.config.save()

    def test_inconsistent_catalogue_rejects_prefix_and_range_sync(self):
        responses = _catalogue_responses_for_subnets(
            4, [{"id": 1, "subnet": "198.18.0.0/24", "pools": [{"pool": "198.18.0.10-198.18.0.20"}]}]
        )
        responses["subnet4-list"] = _subnet_list(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        job = _make_job()
        with stub_kea(responses), self.assertRaises(JobFailed):
            KeaIpamSyncJob(job).run()
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPRange.objects.exists())
        self.assertEqual(job.data["summary"][0]["prefix_errors"], 1)
        self.assertEqual(job.data["summary"][0]["errors"], 0)

    def _run(self, responses, *, failed=False):
        job = self.job = _make_job()
        with stub_kea(responses) as kea:
            if failed:
                with self.assertRaises(JobFailed):
                    KeaIpamSyncJob(job).run()
            else:
                KeaIpamSyncJob(job).run()
        return job.data["summary"], kea

    def _responses(self, pools=()):
        return _catalogue_responses_for_subnets(
            4, [{"id": 1, "subnet": "198.18.0.0/24", "pools": [{"pool": pool} for pool in pools]}]
        )

    def test_invalid_global_option_facts_do_not_block_prefix_sync(self):
        subnets = [{"id": 1, "subnet": "198.18.0.0/24", "pools": []}]
        cases = {
            "option-data": {"global_options": ({"data": "no identity"},)},
            "option-def": {"option_definitions": ({"code": 224, "name": "example"},)},
        }
        for label, invalid in cases.items():
            with self.subTest(label):
                Prefix.objects.all().delete()
                summary, _ = self._run(_catalogue_responses_for_subnets(4, subnets, **invalid))
                self.assertTrue(Prefix.objects.filter(prefix="198.18.0.0/24").exists())
                self.assertEqual(summary[0]["prefix_errors"], 0)

    def test_prefix_created_updated_and_unchanged_counts(self):
        responses = self._responses()
        summary, _ = self._run(responses)
        prefix = Prefix.objects.get(prefix="198.18.0.0/24")
        self.assertEqual(prefix.status, "active")
        self.assertEqual(summary[0]["created"], 1)
        self.assertEqual(summary[0]["updated"], 0)
        prefix.description = ""
        prefix.save()
        summary, _ = self._run(responses)
        prefix.refresh_from_db()
        self.assertEqual(prefix.description, "[kea-sync: subnet]")
        self.assertEqual(summary[0]["created"], 0)
        self.assertEqual(summary[0]["updated"], 1)
        summary, _ = self._run(responses)
        self.assertEqual(summary[0]["created"], 0)
        self.assertEqual(summary[0]["updated"], 0)
        self.assertEqual(Prefix.objects.count(), 1)

    def test_pools_create_update_and_keep_ranges(self):
        responses = self._responses(("198.18.0.10 - 198.18.0.20", "198.18.0.128/25"))
        summary, _ = self._run(responses)
        ranges = list(IPRange.objects.order_by("start_address"))
        self.assertEqual(
            [(str(r.start_address), str(r.end_address)) for r in ranges],
            [("198.18.0.10/24", "198.18.0.20/24"), ("198.18.0.128/24", "198.18.0.255/24")],
        )
        self.assertEqual(summary[0]["created"], 3)
        ranges[0].description = ""
        ranges[0].save()
        ranges[1].description = "Operator pool note"
        ranges[1].save()
        summary, _ = self._run(responses)
        ranges[0].refresh_from_db()
        ranges[1].refresh_from_db()
        self.assertEqual(ranges[0].description, "[kea-sync: pool]")
        self.assertEqual(ranges[1].description, "Operator pool note")
        self.assertEqual(summary[0]["updated"], 1)
        self.assertEqual(summary[0]["created"], 0)
        self.assertEqual(IPRange.objects.count(), 2)

    def test_existing_cidr_pool_keeps_one_range_and_operator_description(self):
        from netaddr import IPNetwork

        existing = IPRange.objects.create(
            start_address=IPNetwork("198.18.0.128/25"),
            end_address=IPNetwork("198.18.0.255/25"),
            status="active",
            description="Operator pool note",
        )
        summary, _ = self._run(self._responses(("198.18.0.128/25",)))
        self.assertEqual(IPRange.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.description, "Operator pool note")
        self.assertEqual(str(existing.start_address), "198.18.0.128/25")
        self.assertEqual(str(existing.end_address), "198.18.0.255/25")
        self.assertEqual(summary[0]["created"], 1)
        self.assertEqual(summary[0]["updated"], 0)
        self.assertEqual(summary[0]["prefix_errors"], 0)

    def test_large_ipv6_pool_is_skipped_without_error(self):
        self.server.dhcp4 = False
        self.server.dhcp6 = True
        self.server.save()
        responses = _catalogue_responses_for_subnets(
            6, [{"id": 1, "subnet": "2001:db8::/64", "pools": [{"pool": "2001:db8::/64"}]}]
        )
        summary, _ = self._run(responses)
        self.assertTrue(Prefix.objects.filter(prefix="2001:db8::/64").exists())
        self.assertFalse(IPRange.objects.exists())
        self.assertEqual(summary[0]["created"], 1)
        self.assertEqual(summary[0]["prefix_errors"], 0)

    def test_invalid_pool_fails_catalogue_without_partial_writes(self):
        summary, _ = self._run(self._responses(("198.18.0.10-198.18.0.20", "invalid-pool")), failed=True)
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPRange.objects.exists())
        self.assertEqual(summary[0]["prefix_errors"], 1)
        self.assertEqual(summary[0]["errors"], 0)

    def test_per_server_prefix_range_toggles(self):
        for prefixes, ranges in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(prefixes=prefixes, ranges=ranges):
                Prefix.objects.all().delete()
                IPRange.objects.all().delete()
                self.server.sync_prefixes_enabled = prefixes
                self.server.sync_ip_ranges_enabled = ranges
                self.server.save()
                summary, kea = self._run(self._responses(("198.18.0.10-198.18.0.20",)))
                self.assertEqual(Prefix.objects.count(), int(prefixes))
                self.assertEqual(IPRange.objects.count(), int(ranges))
                self.assertEqual(summary[0]["created"], int(prefixes) + int(ranges))
                self.assertEqual(summary[0]["prefix_errors"], 0)
                if not prefixes and not ranges:
                    self.assertEqual(kea.commands(), [])

    def test_global_prefix_range_toggles(self):
        for prefixes, ranges in ((True, False), (False, True), (False, False)):
            with self.subTest(prefixes=prefixes, ranges=ranges):
                Prefix.objects.all().delete()
                IPRange.objects.all().delete()
                self.config.sync_prefixes_enabled = prefixes
                self.config.sync_ip_ranges_enabled = ranges
                self.config.save()
                summary, kea = self._run(self._responses(("198.18.0.10-198.18.0.20",)))
                self.assertEqual(Prefix.objects.count(), int(prefixes))
                self.assertEqual(IPRange.objects.count(), int(ranges))
                if not prefixes and not ranges:
                    self.assertEqual(summary, [])
                    self.assertEqual(kea.commands(), [])

    def test_prefix_and_range_use_server_vrf(self):
        from ipam.models import VRF

        self.server.sync_vrf = VRF.objects.create(name="catalogue-vrf")
        self.server.save()
        self._run(self._responses(("198.18.0.10-198.18.0.20",)))
        self.assertEqual(Prefix.objects.get().vrf_id, self.server.sync_vrf_id)
        self.assertEqual(IPRange.objects.get().vrf_id, self.server.sync_vrf_id)

    def test_unavailable_catalogue_counts_one_error_and_falls_back_for_leases(self):
        self.config.sync_leases_enabled = True
        self.config.save()
        Prefix.objects.create(prefix="198.18.0.0/25", status="active", description="Fallback prefix")
        responses = self._responses()
        responses["config-get"] = requests.ConnectionError("unavailable")
        responses["lease4-get-page"] = _lease_page(
            [{"ip-address": "198.18.0.10", "subnet-id": 1, "hostname": "lease.example.invalid"}]
        )
        with self.assertLogs("netbox_kea.jobs", level="INFO") as logs:
            summary, _ = self._run(responses, failed=True)
        self.assertEqual(str(NbIP.objects.get().address), "198.18.0.10/25")
        self.assertEqual(summary[0]["created"], 1)
        self.assertEqual(summary[0]["prefix_errors"], 1)
        self.assertEqual(summary[0]["errors"], 0)
        self.assertEqual(Prefix.objects.count(), 1)
        self.assertFalse(IPRange.objects.exists())
        self.assertTrue(any("Subnet Catalogue unavailable" in message for message in logs.output))
        self.assertTrue(any("lease masks fall back to NetBox prefix matching" in message for message in logs.output))

    def test_shared_network_subnets_sync_prefixes_and_pools(self):
        for family, cidr, pool in (
            (4, "198.18.0.0/24", "198.18.0.10-198.18.0.20"),
            (6, "2001:db8::/64", "2001:db8::10-2001:db8::20"),
        ):
            with self.subTest(family=family):
                self.server.dhcp4 = family == 4
                self.server.dhcp6 = family == 6
                self.server.save()
                subnet = {"id": 2, "subnet": cidr, "pools": [{"pool": pool}]}
                responses = _catalogue_responses_for_subnets(
                    family, [], shared_networks=[{"name": "shared", f"subnet{family}": [subnet]}]
                )
                responses[f"subnet{family}-list"] = _subnet_list(
                    family, [{"id": 2, "subnet": cidr, "shared-network-name": "shared"}]
                )
                summary, _ = self._run(responses)
                self.assertTrue(Prefix.objects.filter(prefix=cidr).exists())
                self.assertTrue(
                    IPRange.objects.filter(start_address=f"{pool.split('-')[0]}/{cidr.split('/')[1]}").exists()
                )
                self.assertEqual(summary[0]["created"], 2)
                self.assertEqual(summary[0]["prefix_errors"], 0)

    def test_multiple_subnets_sync_all_prefixes_and_pools(self):
        standalone_subnets = [
            {"id": 1, "subnet": "198.18.0.0/26", "pools": [{"pool": "198.18.0.10-198.18.0.20"}]},
            {"id": 2, "subnet": "198.18.0.64/27", "pools": [{"pool": "198.18.0.70-198.18.0.80"}]},
        ]
        shared_subnet = {
            "id": 3,
            "subnet": "198.18.0.96/28",
            "pools": [{"pool": "198.18.0.100-198.18.0.110"}],
        }
        responses = _catalogue_responses_for_subnets(
            4, standalone_subnets, shared_networks=[{"name": "shared", "subnet4": [shared_subnet]}]
        )
        responses["subnet4-list"] = _subnet_list(
            4,
            [
                {"id": 1, "subnet": "198.18.0.0/26"},
                {"id": 2, "subnet": "198.18.0.64/27"},
                {"id": 3, "subnet": "198.18.0.96/28", "shared-network-name": "shared"},
            ],
        )

        summary, _ = self._run(responses)

        self.assertCountEqual(
            [str(prefix.prefix) for prefix in Prefix.objects.all()],
            ["198.18.0.0/26", "198.18.0.64/27", "198.18.0.96/28"],
        )
        self.assertCountEqual(
            [(str(ip_range.start_address), str(ip_range.end_address)) for ip_range in IPRange.objects.all()],
            [
                ("198.18.0.10/26", "198.18.0.20/26"),
                ("198.18.0.70/27", "198.18.0.80/27"),
                ("198.18.0.100/28", "198.18.0.110/28"),
            ],
        )
        self.assertEqual(summary[0]["created"], 6)
        self.assertEqual(summary[0]["errors"], 0)
        self.assertEqual(summary[0]["prefix_errors"], 0)

    def test_multiple_subnet_ids_supply_each_lease_mask(self):
        self.config.sync_leases_enabled = True
        self.config.sync_prefixes_enabled = False
        self.config.sync_ip_ranges_enabled = False
        self.config.save()
        responses = _catalogue_responses_for_subnets(
            4,
            [
                {"id": 1, "subnet": "198.18.0.0/26"},
                {"id": 2, "subnet": "198.18.0.64/27"},
                {"id": 3, "subnet": "198.18.0.96/28"},
            ],
        )
        responses["lease4-get-page"] = _lease_page(
            [
                {"ip-address": "198.18.0.10", "subnet-id": 1, "hostname": "lease1.example.invalid"},
                {"ip-address": "198.18.0.70", "subnet-id": 2, "hostname": "lease2.example.invalid"},
                {"ip-address": "198.18.0.100", "subnet-id": 3, "hostname": "lease3.example.invalid"},
            ]
        )
        self.assertFalse(Prefix.objects.exists())

        summary, _ = self._run(responses)

        self.assertCountEqual(
            [str(ip.address) for ip in NbIP.objects.all()],
            ["198.18.0.10/26", "198.18.0.70/27", "198.18.0.100/28"],
        )
        self.assertFalse(Prefix.objects.exists())
        self.assertEqual(summary[0]["created"], 3)
        self.assertEqual(summary[0]["errors"], 0)
        self.assertEqual(summary[0]["prefix_errors"], 0)

    def test_all_phases_share_one_catalogue_per_family(self):
        self.server.dhcp6 = True
        self.server.save()
        self.config.sync_leases_enabled = True
        self.config.sync_reservations_enabled = True
        self.config.save()
        families = {
            4: ("198.18.0.0/26", "198.18.0.10", "198.18.0.20"),
            6: ("2001:db8::/72", "2001:db8::10", "2001:db8::20"),
        }
        responses = {}
        for family, (cidr, lease, _reservation) in families.items():
            responses.update(_catalogue_responses_for_subnets(family, [{"id": 1, "subnet": cidr}]))
            responses[f"lease{family}-get-page"] = _lease_page(
                [{"ip-address": lease, "subnet-id": 1, "hostname": f"lease{family}.example.invalid"}]
            )

        def config_get(body):
            family = int(body["service"][0][-1])
            return _catalogue_responses_for_subnets(family, [{"id": 1, "subnet": families[family][0]}])["config-get"]

        def reservation_page(body):
            family = int(body["service"][0][-1])
            address = families[family][2]
            row = {"subnet-id": 1, "hostname": f"reservation{family}.example.invalid"}
            row.update(
                {"ip-address": address, "hw-address": "00:11:22:33:44:55"}
                if family == 4
                else {"ip-addresses": [address], "duid": "00:01:02:03"}
            )
            return _res_page([row])

        responses["config-get"] = config_get
        responses["reservation-get-page"] = reservation_page
        summary, kea = self._run(responses)
        self.assertEqual(len(kea.bodies("config-get")), 2)
        self.assertEqual(len(kea.bodies("subnet4-list")), 1)
        self.assertEqual(len(kea.bodies("subnet6-list")), 1)
        self.assertEqual(summary[0]["created"], 6)
        self.assertEqual(summary[0]["errors"], 0)
        self.assertEqual(summary[0]["prefix_errors"], 0)
        for cidr, lease, reservation in families.values():
            mask = cidr.split("/")[1]
            self.assertEqual(str(NbIP.objects.get(address__net_host=lease).address), f"{lease}/{mask}")
            self.assertEqual(str(NbIP.objects.get(address__net_host=reservation).address), f"{reservation}/{mask}")
            self.assertTrue(Prefix.objects.filter(prefix=cidr).exists())

    def test_duplicate_prefix_counts_error_and_still_syncs_pool(self):
        for _ in range(2):
            Prefix.objects.create(prefix="198.18.0.0/24", status="active")
        summary, _ = self._run(self._responses(("198.18.0.10-198.18.0.20",)), failed=True)
        self.assertEqual(Prefix.objects.count(), 2)
        self.assertEqual(IPRange.objects.count(), 1)
        self.assertEqual(summary[0]["created"], 1)
        self.assertEqual(summary[0]["prefix_errors"], 1)
        self.assertEqual(summary[0]["errors"], 0)
        self._assert_duplicate_logged("subnet 198.18.0.0/24", "/ipam/prefixes/?prefix=198.18.0.0%2F24&vrf_id=null")

    def test_duplicate_range_counts_error_and_keeps_prefix(self):
        from netaddr import IPNetwork

        for _ in range(2):
            IPRange.objects.create(
                start_address=IPNetwork("198.18.0.10/24"), end_address=IPNetwork("198.18.0.20/24"), status="active"
            )
        summary, _ = self._run(self._responses(("198.18.0.10-198.18.0.20",)), failed=True)
        self.assertEqual(Prefix.objects.count(), 1)
        self.assertEqual(IPRange.objects.count(), 2)
        self.assertEqual(summary[0]["created"], 1)
        self.assertEqual(summary[0]["prefix_errors"], 1)
        self.assertEqual(summary[0]["errors"], 0)
        self._assert_duplicate_logged(
            "pool 198.18.0.10-198.18.0.20",
            "/ipam/ip-ranges/?start_address=198.18.0.10&end_address=198.18.0.20&vrf_id=null",
        )

    def test_a_failed_pool_counts_an_error_and_the_next_pool_still_syncs(self):
        from netbox_kea import sync

        real_sync = sync.sync_pool_to_netbox_ip_range

        def fail_first_pool(pool, subnet, vrf=None):
            if pool.range == "198.18.0.10-198.18.0.20":
                raise RuntimeError("IPRange save failed")
            return real_sync(pool, subnet, vrf=vrf)

        # NetBox saves an IPRange without validation, so no row state makes the save fail inside a test transaction.
        with (
            patch.object(sync, "sync_pool_to_netbox_ip_range", autospec=True, side_effect=fail_first_pool),
            self.assertLogs("netbox_kea.jobs", level="ERROR") as logs,
        ):
            summary, _ = self._run(
                self._responses(("198.18.0.10-198.18.0.20", "198.18.0.100-198.18.0.110")), failed=True
            )
        self.assertEqual(
            [(str(r.start_address), str(r.end_address)) for r in IPRange.objects.all()],
            [("198.18.0.100/24", "198.18.0.110/24")],
        )
        self.assertEqual(summary[0]["created"], 2)
        self.assertEqual(summary[0]["prefix_errors"], 1)
        self.assertEqual(summary[0]["errors"], 0)
        self.assertTrue(
            any("Failed to sync pool 198.18.0.10-198.18.0.20 from server" in line for line in logs.output), logs.output
        )

    def _assert_duplicate_logged(self, kea_object, list_url):
        """The job log names the Kea object and the filtered list URL, but no row pk."""
        from core.dataclasses import JobLogEntry

        messages = [JobLogEntry.from_logrecord(call.args[0]).message for call in self.job.log.call_args_list]
        lines = [m for m in messages if kea_object in m]
        self.assertEqual(len(lines), 1, messages)
        self.assertIn(list_url, lines[0])
        self.assertNotRegex(
            lines[0].replace(list_url, "").replace(kea_object, ""), r"\d", "a row pk reached the job log"
        )

    def test_empty_catalogue_is_successful(self):
        summary, kea = self._run(_catalogue_responses_for_subnets(4, []))
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPRange.objects.exists())
        self.assertEqual(summary[0]["created"], 0)
        self.assertEqual(summary[0]["prefix_errors"], 0)
        self.assertEqual(kea.commands().count("config-get"), 1)
