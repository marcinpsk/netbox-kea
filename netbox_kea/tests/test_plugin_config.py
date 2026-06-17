# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the ghost-job self-heal and its scheduling wiring.

The self-heal lives on ``KeaIpamSyncJob`` and runs inside an ``enqueue_once``
override (invoked once per ``rqworker`` startup), not in ``AppConfig.ready()`` —
keeping all DB access off the app-initialisation path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from netbox_kea.jobs import KeaIpamSyncJob


class TestHealGhostScheduledJobs(TestCase):
    """Tests for KeaIpamSyncJob._heal_ghost_scheduled_jobs().

    Uses real ``core.models.Job`` rows so the ORM queries inside
    ``_heal_ghost_scheduled_jobs`` run against the actual DB.  RQ is still
    mocked — Redis is the true external boundary.
    """

    def _make_job(self, status: str = "scheduled"):
        """Create a real Job row with the given status."""
        import uuid

        from core.models import Job

        return Job.objects.create(
            name=KeaIpamSyncJob.name,
            status=status,
            job_id=uuid.uuid4(),
        )

    def _heal(self, rq_statuses: dict | None = None, no_such: set | None = None):
        """Run _heal_ghost_scheduled_jobs() with RQ responses pre-configured.

        ``rq_statuses`` — {str(job_id): status_string} for jobs that exist in RQ.
        ``no_such``     — set of str(job_id) values that raise NoSuchJobError.
        Jobs not listed in either dict return status "scheduled" (kept alive).
        """
        rq_statuses = rq_statuses or {}
        no_such = no_such or set()

        def _fetch(job_id, connection, **kwargs):
            # NetBox's Job.delete() signal may call RQJob.fetch with extra kwargs
            # (e.g. serializer=); accept and ignore them.
            str_id = str(job_id)
            if str_id in no_such:
                from rq.exceptions import NoSuchJobError

                raise NoSuchJobError(str_id)
            rq_job = MagicMock()  # mock-ok: RQ job (scheduler boundary)
            status_str = rq_statuses.get(str_id, "scheduled")
            rq_job.get_status.return_value = MagicMock(value=status_str)  # mock-ok: RQ JobStatus enum value
            return rq_job

        with (
            patch("django_rq.get_connection", return_value=MagicMock()),  # mock-ok: redis connection (external)
            patch("rq.job.Job.fetch", side_effect=_fetch),
        ):
            KeaIpamSyncJob._heal_ghost_scheduled_jobs()

    # ------------------------------------------------------------------
    # No candidates — early exit
    # ------------------------------------------------------------------

    def test_no_candidates_returns_early_without_rq_connection(self):
        # Empty DB → candidates.exists() is False → returns before connecting to RQ.
        with patch("django_rq.get_connection") as mock_conn:
            KeaIpamSyncJob._heal_ghost_scheduled_jobs()
        mock_conn.assert_not_called()

    # ------------------------------------------------------------------
    # Ghost scenarios — DB record should be deleted
    # ------------------------------------------------------------------

    def test_failed_rq_job_is_deleted(self):
        from core.models import Job

        job = self._make_job()
        self._heal(rq_statuses={str(job.job_id): "failed"})
        self.assertFalse(Job.objects.filter(pk=job.pk).exists())

    def test_canceled_rq_job_is_deleted(self):
        from core.models import Job

        job = self._make_job()
        self._heal(rq_statuses={str(job.job_id): "canceled"})
        self.assertFalse(Job.objects.filter(pk=job.pk).exists())

    def test_stopped_rq_job_is_deleted(self):
        from core.models import Job

        job = self._make_job()
        self._heal(rq_statuses={str(job.job_id): "stopped"})
        self.assertFalse(Job.objects.filter(pk=job.pk).exists())

    def test_no_such_job_in_redis_is_deleted(self):
        from core.models import Job

        job = self._make_job()
        self._heal(no_such={str(job.job_id)})
        self.assertFalse(Job.objects.filter(pk=job.pk).exists())

    def test_no_job_id_is_deleted(self):
        """Defensive path: a row with job_id=None is deleted without checking RQ.

        Uses a mock row because ``job_id`` is ``null=False`` in the DB, making this
        state unreachable via normal ORM saves; the guard still protects against
        unexpected DB state (e.g. direct SQL inserts or future schema changes).
        """
        mock_row = MagicMock()  # mock-ok: minimal scheduled-job row
        mock_row.job_id = None

        with (
            patch.object(KeaIpamSyncJob, "get_jobs") as mock_get_jobs,
            patch("django_rq.get_connection", return_value=MagicMock()),  # mock-ok: redis connection (external)
        ):
            mock_qs = MagicMock()  # mock-ok: queryset stand-in for ghost-job scan
            mock_qs.exists.return_value = True
            mock_qs.filter.return_value = mock_qs
            mock_qs.__iter__ = lambda self: iter([mock_row])
            mock_get_jobs.return_value = mock_qs
            KeaIpamSyncJob._heal_ghost_scheduled_jobs()

        mock_row.delete.assert_called_once()

    # ------------------------------------------------------------------
    # Live job — must NOT be deleted
    # ------------------------------------------------------------------

    def test_live_scheduled_rq_job_not_deleted(self):
        from core.models import Job

        job = self._make_job()
        self._heal(rq_statuses={str(job.job_id): "scheduled"})
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())

    def test_live_queued_rq_job_not_deleted(self):
        from core.models import Job

        job = self._make_job()
        self._heal(rq_statuses={str(job.job_id): "queued"})
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())

    # ------------------------------------------------------------------
    # Warning log when ghosts are removed
    # ------------------------------------------------------------------

    def test_warning_logged_when_ghost_deleted(self):
        job = self._make_job()
        with self.assertLogs("netbox_kea", level="WARNING") as cm:
            self._heal(rq_statuses={str(job.job_id): "failed"})
        self.assertTrue(any("ghost" in msg.lower() for msg in cm.output))

    def test_no_warning_when_nothing_deleted(self):
        import logging

        job = self._make_job()
        with self.assertLogs("netbox_kea", level="DEBUG") as cm:
            logging.getLogger("netbox_kea").debug("sentinel")
            self._heal(rq_statuses={str(job.job_id): "scheduled"})
        warnings = [m for m in cm.output if "WARNING" in m and "ghost" in m.lower()]
        self.assertEqual(warnings, [])

    # ------------------------------------------------------------------
    # Resilience — exceptions must not propagate
    # ------------------------------------------------------------------

    def test_db_unavailable_does_not_raise(self):
        # Simulate DB failure by patching get_jobs(); RQ is never reached.
        with patch("netbox_kea.jobs.KeaIpamSyncJob.get_jobs", side_effect=Exception("DB down")):
            KeaIpamSyncJob._heal_ghost_scheduled_jobs()

    def test_redis_unavailable_does_not_raise(self):
        self._make_job()  # ensure candidates.exists() is True
        with patch("django_rq.get_connection", side_effect=Exception("Redis down")):
            KeaIpamSyncJob._heal_ghost_scheduled_jobs()

    def test_per_record_error_does_not_abort_remaining_records(self):
        """A transient error on one record must not skip the others."""
        from core.models import Job

        bad_job = self._make_job()
        good_job = self._make_job()

        def _fetch(job_id, connection, **kwargs):  # match rq Job.fetch(id, connection, serializer)
            if str(job_id) == str(bad_job.job_id):
                raise RuntimeError("transient Redis error")
            rq_job = MagicMock()  # mock-ok: RQ job (scheduler boundary)
            rq_job.get_status.return_value = MagicMock(value="failed")
            return rq_job

        with (
            patch("django_rq.get_connection", return_value=MagicMock()),  # mock-ok: redis connection (external)
            patch("rq.job.Job.fetch", side_effect=_fetch),
        ):
            KeaIpamSyncJob._heal_ghost_scheduled_jobs()

        self.assertTrue(Job.objects.filter(pk=bad_job.pk).exists())
        self.assertFalse(Job.objects.filter(pk=good_job.pk).exists())

    # ------------------------------------------------------------------
    # Plain-string status fallback (rq < enum era)
    # ------------------------------------------------------------------

    def test_plain_string_status_failed_is_deleted(self):
        """Cover the str(status) fallback when get_status() returns a bare string."""
        from core.models import Job

        job = self._make_job()

        def _fetch(job_id, connection, **kwargs):  # match rq Job.fetch(id, connection, serializer)
            rq_job = MagicMock()  # mock-ok: RQ job (scheduler boundary)
            rq_job.get_status.return_value = "failed"  # plain string, no .value
            return rq_job

        with (
            patch("django_rq.get_connection", return_value=MagicMock()),  # mock-ok: redis connection (external)
            patch("rq.job.Job.fetch", side_effect=_fetch),
        ):
            KeaIpamSyncJob._heal_ghost_scheduled_jobs()

        self.assertFalse(Job.objects.filter(pk=job.pk).exists())

    def test_plain_string_status_live_not_deleted(self):
        """A plain-string live status must not trigger deletion."""
        from core.models import Job

        job = self._make_job()

        def _fetch(job_id, connection, **kwargs):  # match rq Job.fetch(id, connection, serializer)
            rq_job = MagicMock()  # mock-ok: RQ job (scheduler boundary)
            rq_job.get_status.return_value = "scheduled"  # plain string
            return rq_job

        with (
            patch("django_rq.get_connection", return_value=MagicMock()),  # mock-ok: redis connection (external)
            patch("rq.job.Job.fetch", side_effect=_fetch),
        ):
            KeaIpamSyncJob._heal_ghost_scheduled_jobs()

        self.assertTrue(Job.objects.filter(pk=job.pk).exists())


class TestEnqueueOnceWiring(SimpleTestCase):
    """The enqueue_once override must heal first, then delegate to NetBox."""

    def test_enqueue_once_heals_then_delegates(self):
        """Ghost-heal runs before super().enqueue_once(), and kwargs pass through."""
        manager = MagicMock()  # mock-ok: call-order manager (attach_mock/mock_calls)
        sentinel = object()

        # Patch the heal on our class and JobRunner.enqueue_once (the super impl).
        with (
            patch.object(KeaIpamSyncJob, "_heal_ghost_scheduled_jobs") as mock_heal,
            patch("netbox.jobs.JobRunner.enqueue_once", return_value=sentinel) as mock_super,
        ):
            manager.attach_mock(mock_heal, "heal")
            manager.attach_mock(mock_super, "enqueue")

            result = KeaIpamSyncJob.enqueue_once(interval=5)

        # Delegated return value is forwarded unchanged.
        self.assertIs(result, sentinel)
        # Heal ran before the delegation, and kwargs were forwarded.
        self.assertEqual([c[0] for c in manager.mock_calls], ["heal", "enqueue"])
        mock_super.assert_called_once_with(interval=5)
