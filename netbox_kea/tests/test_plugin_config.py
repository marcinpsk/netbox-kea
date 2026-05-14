# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for NetBoxKeaConfig.ready() helpers — _heal_ghost_scheduled_jobs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from netbox_kea import NetBoxKeaConfig


def _make_config() -> NetBoxKeaConfig:
    """Return a bare NetBoxKeaConfig instance without triggering real ready() logic."""
    return NetBoxKeaConfig.__new__(NetBoxKeaConfig)


def _make_db_job(job_id: str | None = "abc-123") -> MagicMock:
    mock = MagicMock()
    mock.job_id = job_id
    return mock


class TestHealGhostScheduledJobs(SimpleTestCase):
    """Tests for NetBoxKeaConfig._heal_ghost_scheduled_jobs()."""

    def _run(self, candidates, rq_statuses: dict[str, str] | None = None, no_such: set[str] | None = None):
        """
        Helper: run _heal_ghost_scheduled_jobs() with mocked DB queryset and RQ jobs.

        candidates  — list of mock DB job objects
        rq_statuses — {job_id: status_str} for jobs that exist in RQ
        no_such     — set of job_ids that raise NoSuchJobError
        """
        rq_statuses = rq_statuses or {}
        no_such = no_such or set()

        mock_qs = MagicMock()
        mock_qs.exists.return_value = bool(candidates)
        mock_qs.filter.return_value = mock_qs
        mock_qs.__iter__ = lambda self: iter(candidates)

        def _fetch(job_id, connection):
            if job_id in no_such:
                from rq.exceptions import NoSuchJobError

                raise NoSuchJobError(job_id)
            rq_job = MagicMock()
            status_str = rq_statuses.get(job_id, "scheduled")
            rq_job.get_status.return_value = MagicMock(value=status_str)
            return rq_job

        cfg = _make_config()
        with (
            patch("netbox_kea.NetBoxKeaConfig._configure_sync_job_interval"),
            patch("netbox_kea.jobs.KeaIpamSyncJob.get_jobs", return_value=mock_qs),
            patch("django_rq.get_connection", return_value=MagicMock()),
            patch("rq.job.Job.fetch", side_effect=_fetch),
        ):
            cfg._heal_ghost_scheduled_jobs()

        return candidates

    # ------------------------------------------------------------------
    # No candidates — early exit
    # ------------------------------------------------------------------

    def test_no_candidates_returns_early_without_rq_connection(self):
        mock_qs = MagicMock()
        mock_qs.exists.return_value = False
        mock_qs.filter.return_value = mock_qs

        cfg = _make_config()
        with (
            patch("netbox_kea.jobs.KeaIpamSyncJob.get_jobs", return_value=mock_qs),
            patch("django_rq.get_connection") as mock_conn,
        ):
            cfg._heal_ghost_scheduled_jobs()

        mock_conn.assert_not_called()

    # ------------------------------------------------------------------
    # Ghost scenarios — DB record should be deleted
    # ------------------------------------------------------------------

    def test_failed_rq_job_is_deleted(self):
        job = _make_db_job("job-1")
        self._run([job], rq_statuses={"job-1": "failed"})
        job.delete.assert_called_once()

    def test_canceled_rq_job_is_deleted(self):
        job = _make_db_job("job-2")
        self._run([job], rq_statuses={"job-2": "canceled"})
        job.delete.assert_called_once()

    def test_stopped_rq_job_is_deleted(self):
        job = _make_db_job("job-3")
        self._run([job], rq_statuses={"job-3": "stopped"})
        job.delete.assert_called_once()

    def test_no_such_job_in_redis_is_deleted(self):
        job = _make_db_job("job-4")
        self._run([job], no_such={"job-4"})
        job.delete.assert_called_once()

    def test_no_job_id_is_deleted(self):
        job = _make_db_job(job_id=None)
        self._run([job])
        job.delete.assert_called_once()

    # ------------------------------------------------------------------
    # Live job — must NOT be deleted
    # ------------------------------------------------------------------

    def test_live_scheduled_rq_job_not_deleted(self):
        job = _make_db_job("job-live")
        self._run([job], rq_statuses={"job-live": "scheduled"})
        job.delete.assert_not_called()

    def test_live_queued_rq_job_not_deleted(self):
        job = _make_db_job("job-queued")
        self._run([job], rq_statuses={"job-queued": "queued"})
        job.delete.assert_not_called()

    # ------------------------------------------------------------------
    # Warning log when ghosts are removed
    # ------------------------------------------------------------------

    def test_warning_logged_when_ghost_deleted(self):
        job = _make_db_job("job-warn")
        with self.assertLogs("netbox_kea", level="WARNING") as cm:
            self._run([job], rq_statuses={"job-warn": "failed"})
        self.assertTrue(any("ghost" in msg.lower() for msg in cm.output))

    def test_no_warning_when_nothing_deleted(self):
        job = _make_db_job("job-ok")
        # Should not produce WARNING-level logs
        import logging

        with self.assertLogs("netbox_kea", level="DEBUG") as cm:
            # Emit a debug log ourselves so assertLogs doesn't fail on empty
            logging.getLogger("netbox_kea").debug("sentinel")
            self._run([job], rq_statuses={"job-ok": "scheduled"})
        warnings = [m for m in cm.output if "WARNING" in m and "ghost" in m.lower()]
        self.assertEqual(warnings, [])

    # ------------------------------------------------------------------
    # Resilience — exceptions must not propagate
    # ------------------------------------------------------------------

    def test_db_unavailable_does_not_raise(self):
        cfg = _make_config()
        with patch("netbox_kea.jobs.KeaIpamSyncJob.get_jobs", side_effect=Exception("DB down")):
            # Must not raise
            cfg._heal_ghost_scheduled_jobs()

    def test_redis_unavailable_does_not_raise(self):
        job = _make_db_job("job-5")
        mock_qs = MagicMock()
        mock_qs.exists.return_value = True
        mock_qs.filter.return_value = mock_qs
        mock_qs.__iter__ = lambda self: iter([job])

        cfg = _make_config()
        with (
            patch("netbox_kea.jobs.KeaIpamSyncJob.get_jobs", return_value=mock_qs),
            patch("django_rq.get_connection", side_effect=Exception("Redis down")),
        ):
            # Must not raise
            cfg._heal_ghost_scheduled_jobs()
