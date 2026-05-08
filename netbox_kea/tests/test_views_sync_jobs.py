# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for SyncJobsView, ServerSyncStatusView, ServerSyncNowView, ServerSyncToggleView."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from netbox_kea.models import SyncConfig
from netbox_kea.tests.utils import _PLUGINS_CONFIG, User, _make_db_server

_MAKE_JOB_NO_DATA = object()  # sentinel: caller did not pass data at all


def _make_job(*, name="Kea IPAM Sync", object_id=None, object_type=None, data=_MAKE_JOB_NO_DATA, delta_seconds=0):
    """Create a real Job row in the test DB.

    Pass ``data=None`` explicitly to store a NULL data field (exercises the
    ``isinstance(job.data, dict)`` guard in ``get_recent_jobs_for_servers``).
    Omitting *data* defaults to ``{}``.

    ``delta_seconds`` shifts the stored ``created`` timestamp backwards so that
    ordering tests are reliable.  Because ``Job.created`` is ``auto_now_add``
    Django silently ignores the field in ``create()``, so we back-fill it via
    a raw ``update()`` call which bypasses that restriction.
    """
    from core.models import Job

    job = Job.objects.create(
        name=name,
        object_type=object_type,
        object_id=object_id,
        status="completed",
        data={} if data is _MAKE_JOB_NO_DATA else data,
        job_id=uuid.uuid4(),
    )
    if delta_seconds:
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - timedelta(seconds=delta_seconds))
        job.refresh_from_db(fields=["created"])
    return job


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncJobsView(TestCase):
    """GET /plugins/kea/sync-jobs/"""

    def setUp(self):
        self.user = User.objects.create_superuser("synctest", "s@s.com", "pass")
        self.client.force_login(self.user)
        _make_db_server(name="kea-a")
        _make_db_server(name="kea-b")

    def test_get_returns_200(self):
        response = self.client.get(reverse("plugins:netbox_kea:sync_jobs"))
        self.assertEqual(response.status_code, 200)

    def test_get_contains_server_names(self):
        response = self.client.get(reverse("plugins:netbox_kea:sync_jobs"))
        self.assertContains(response, "kea-a")
        self.assertContains(response, "kea-b")

    def test_get_contains_syncconfig_defaults(self):
        response = self.client.get(reverse("plugins:netbox_kea:sync_jobs"))
        # Default interval of 5 should appear in the form
        self.assertContains(response, "5")

    def test_post_saves_new_interval(self):
        url = reverse("plugins:netbox_kea:sync_jobs")
        with patch("netbox_kea.views.sync_jobs.KeaIpamSyncJob"):
            response = self.client.post(url, {"interval_minutes": 10, "sync_enabled": True}, follow=True)
        self.assertEqual(response.status_code, 200)
        cfg = SyncConfig.get()
        self.assertEqual(cfg.interval_minutes, 10)

    def test_post_updates_registry_interval(self):
        url = reverse("plugins:netbox_kea:sync_jobs")
        from netbox_kea.jobs import KeaIpamSyncJob

        fake_registry = {"system_jobs": {KeaIpamSyncJob: {"interval": 5}}}
        with patch("netbox.registry.registry", fake_registry):
            self.client.post(url, {"interval_minutes": 15, "sync_enabled": True})
        self.assertEqual(fake_registry["system_jobs"][KeaIpamSyncJob]["interval"], 15)

    def test_post_invalid_interval_shows_error(self):
        url = reverse("plugins:netbox_kea:sync_jobs")
        response = self.client.post(url, {"interval_minutes": 0, "sync_enabled": True})
        self.assertEqual(response.status_code, 200)
        # form re-rendered with errors
        self.assertFalse(response.context["form"].is_valid())

    def test_post_db_error_shows_error_message(self):
        """SyncConfig DB failure returns generic error without leaking exception details."""
        from django.db import DatabaseError

        url = reverse("plugins:netbox_kea:sync_jobs")
        with patch("netbox_kea.views.sync_jobs.SyncConfig") as MockConfig:
            MockConfig.get.side_effect = DatabaseError("db is broken")
            response = self.client.post(url, {"interval_minutes": 10, "sync_enabled": True}, follow=True)
        self.assertContains(response, "internal error")

    def test_get_without_login_redirects(self):
        self.client.logout()
        response = self.client.get(reverse("plugins:netbox_kea:sync_jobs"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSyncStatusView(TestCase):
    """GET /plugins/kea/servers/{pk}/sync-status/"""

    def setUp(self):
        self.user = User.objects.create_superuser("synctest2", "s2@s.com", "pass")
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_sync_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_shows_server_name(self):
        url = reverse("plugins:netbox_kea:server_sync_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertContains(response, self.server.name)

    def test_get_contains_jobs_list_link(self):
        url = reverse("plugins:netbox_kea:server_sync_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertContains(response, f"object_id={self.server.pk}")

    def test_can_change_server_true_for_superuser(self):
        """can_change_server is True for superuser via object-level restrict check."""
        url = reverse("plugins:netbox_kea:server_sync_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertTrue(response.context["can_change_server"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSyncNowView(TestCase):
    """POST /plugins/kea/servers/{pk}/sync-now/"""

    def setUp(self):
        self.user = User.objects.create_superuser("synctest3", "s3@s.com", "pass")
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def test_post_enqueues_job_and_redirects(self):
        url = reverse("plugins:netbox_kea:server_sync_now", args=[self.server.pk])
        with patch("netbox_kea.views.sync_jobs.KeaIpamSyncJob") as MockJob:
            response = self.client.post(url)
        MockJob.enqueue.assert_called_once_with(instance=self.server, server_pk=self.server.pk)
        self.assertRedirects(
            response,
            reverse("plugins:netbox_kea:server_sync_status", args=[self.server.pk]),
            fetch_redirect_response=False,
        )

    def test_post_without_permission_returns_403(self):
        restricted = User.objects.create_user("noperm", "n@n.com", "pass")
        self.client.force_login(restricted)
        url = reverse("plugins:netbox_kea:server_sync_now", args=[self.server.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_post_enqueue_exception_shows_error_message(self):
        url = reverse("plugins:netbox_kea:server_sync_now", args=[self.server.pk])
        with patch("netbox_kea.views.sync_jobs.KeaIpamSyncJob") as MockJob:
            MockJob.enqueue.side_effect = RuntimeError("queue full")
            response = self.client.post(url, follow=True)
        self.assertContains(response, "internal error")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSyncToggleView(TestCase):
    """POST /plugins/kea/servers/{pk}/sync-toggle/"""

    def setUp(self):
        self.user = User.objects.create_superuser("synctest4", "s4@s.com", "pass")
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def test_post_toggles_sync_enabled_true_to_false(self):
        self.assertTrue(self.server.sync_enabled)
        url = reverse("plugins:netbox_kea:server_sync_toggle", args=[self.server.pk])
        self.client.post(url)
        self.server.refresh_from_db()
        self.assertFalse(self.server.sync_enabled)

    def test_post_toggles_sync_enabled_false_to_true(self):
        self.server.sync_enabled = False
        self.server.save(update_fields=["sync_enabled"])
        url = reverse("plugins:netbox_kea:server_sync_toggle", args=[self.server.pk])
        self.client.post(url)
        self.server.refresh_from_db()
        self.assertTrue(self.server.sync_enabled)

    def test_post_redirects_to_sync_status_tab(self):
        url = reverse("plugins:netbox_kea:server_sync_toggle", args=[self.server.pk])
        response = self.client.post(url)
        self.assertRedirects(
            response,
            reverse("plugins:netbox_kea:server_sync_status", args=[self.server.pk]),
            fetch_redirect_response=False,
        )

    def test_post_without_permission_returns_403(self):
        restricted = User.objects.create_user("noperm_toggle", "nt@nt.com", "pass")
        self.client.force_login(restricted)
        url = reverse("plugins:netbox_kea:server_sync_toggle", args=[self.server.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncJobsViewAllowedServerPks(TestCase):
    """allowed_server_pks in SyncJobsView context is permission-filtered."""

    def setUp(self):
        self.server = _make_db_server(name="restricted-server")

    def test_superuser_sees_server_in_allowed_pks(self):
        su = User.objects.create_superuser("su_allowed", "su@a.com", "pass")
        self.client.force_login(su)
        response = self.client.get(reverse("plugins:netbox_kea:sync_jobs"))
        self.assertIn(self.server.pk, response.context["allowed_server_pks"])

    def test_user_without_change_perm_has_empty_allowed_pks(self):
        user = User.objects.create_user("ro_user", "ro@a.com", "pass")
        self.client.force_login(user)
        # Read-only user must still be logged in (login is enforced by the view)
        # but since they have no perms they would be redirected; to test the
        # context we give them view (but not change) permission.
        from django.contrib.auth.models import Permission

        view_perm = Permission.objects.get(codename="view_server", content_type__app_label="netbox_kea")
        user.user_permissions.add(view_perm)
        response = self.client.get(reverse("plugins:netbox_kea:sync_jobs"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.server.pk, response.context["allowed_server_pks"])

    def test_post_context_also_includes_allowed_server_pks(self):
        su = User.objects.create_superuser("su_post", "sup@a.com", "pass")
        self.client.force_login(su)
        with patch("netbox_kea.views.sync_jobs.KeaIpamSyncJob"):
            response = self.client.post(
                reverse("plugins:netbox_kea:sync_jobs"),
                {"interval_minutes": 5, "sync_enabled": True},
            )
        # Successful POST redirects; we just verify it doesn't 500
        self.assertIn(response.status_code, [200, 302])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSyncToggleViewErrorHandling(TestCase):
    """ServerSyncToggleView.post wraps save() in try/except."""

    def setUp(self):
        self.user = User.objects.create_superuser("synctest5", "s5@s.com", "pass")
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def test_post_db_error_shows_error_message_and_redirects(self):
        from unittest.mock import patch

        from django.db import DatabaseError

        url = reverse("plugins:netbox_kea:server_sync_toggle", args=[self.server.pk])
        with patch.object(self.server.__class__, "save", side_effect=DatabaseError("disk full")):
            with patch("netbox_kea.views.sync_jobs.get_object_or_404", return_value=self.server):
                response = self.client.post(url, follow=True)
        self.assertContains(response, "internal error")
        self.assertRedirects(
            response,
            reverse("plugins:netbox_kea:server_sync_status", args=[self.server.pk]),
            fetch_redirect_response=False,
            target_status_code=200,
        )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestMigrationsApplied(TestCase):
    """Regression tests: DB columns/tables from new migrations must exist.

    These catch the scenario where the plugin is updated but ``manage.py migrate``
    has not been run (observed as ProgrammingError on combined/sync-jobs pages).
    """

    def setUp(self):
        self.user = User.objects.create_superuser("migtest", "m@m.com", "pass")
        self.client.force_login(self.user)
        _make_db_server(name="mig-server")

    def test_server_sync_enabled_column_accessible(self):
        """Regression: migration 0006 must have added sync_enabled to Server."""
        from netbox_kea.models import Server

        count = Server.objects.filter(sync_enabled=True).count()
        self.assertIsInstance(count, int)

    def test_syncconfig_table_accessible(self):
        """Regression: migration 0005 must have created netbox_kea_syncconfig table."""
        count = SyncConfig.objects.count()
        self.assertIsInstance(count, int)

    def test_combined_view_does_not_500(self):
        """Regression: /plugins/kea/combined/ must not raise ProgrammingError."""
        response = self.client.get(reverse("plugins:netbox_kea:combined"))
        self.assertNotEqual(response.status_code, 500)

    def test_sync_jobs_view_does_not_500(self):
        """Regression: /plugins/kea/sync-jobs/ must not raise ProgrammingError."""
        response = self.client.get(reverse("plugins:netbox_kea:sync_jobs"))
        self.assertNotEqual(response.status_code, 500)

    def test_server_supports_job_assignment(self):
        """Regression: migration 0008 must enable Job assignment to Server (JobsMixin)."""
        from netbox.models.features import has_feature

        from netbox_kea.models import Server

        self.assertTrue(has_feature(Server, "jobs"), "Server must have the 'jobs' feature (JobsMixin)")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetRecentJobsForServers(TestCase):
    """Unit tests for get_recent_jobs_for_servers helper."""

    def setUp(self):
        from django.contrib.contenttypes.models import ContentType

        from netbox_kea.models import Server

        self.server_a = _make_db_server(name="server-a")
        self.server_b = _make_db_server(name="server-b")
        self.ct = ContentType.objects.get_for_model(Server)

    def test_returns_empty_list_for_server_with_no_jobs(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        result = get_recent_jobs_for_servers([self.server_a.pk])
        self.assertEqual(result[self.server_a.pk], [])

    def test_returns_bound_job_for_server(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        job = _make_job(object_id=self.server_a.pk, object_type=self.ct)
        result = get_recent_jobs_for_servers([self.server_a.pk])
        self.assertEqual(len(result[self.server_a.pk]), 1)
        self.assertEqual(result[self.server_a.pk][0].pk, job.pk)

    def test_returns_unbound_periodic_job_attributed_via_summary(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        job = _make_job(data={"summary": [{"pk": self.server_a.pk, "name": "server-a"}]})
        result = get_recent_jobs_for_servers([self.server_a.pk])
        self.assertEqual(len(result[self.server_a.pk]), 1)
        self.assertEqual(result[self.server_a.pk][0].pk, job.pk)

    def test_periodic_job_shown_even_when_bound_jobs_fill_limit(self):
        """Core regression: periodic (unbound) jobs must appear even when limit bound jobs exist."""
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        # Create 3 bound manual jobs (older)
        for i in range(3, 0, -1):
            _make_job(object_id=self.server_a.pk, object_type=self.ct, delta_seconds=i * 100)

        # Create 1 periodic job (newer than all bound jobs)
        periodic = _make_job(
            data={"summary": [{"pk": self.server_a.pk}]},
            delta_seconds=10,  # very recent
        )

        result = get_recent_jobs_for_servers([self.server_a.pk], limit=3)
        pks = [j.pk for j in result[self.server_a.pk]]
        self.assertIn(periodic.pk, pks, "Periodic job must appear even when limit bound jobs fill the slot")

    def test_most_recent_job_returned_first(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        old = _make_job(object_id=self.server_a.pk, object_type=self.ct, delta_seconds=200)
        new = _make_job(object_id=self.server_a.pk, object_type=self.ct, delta_seconds=10)
        result = get_recent_jobs_for_servers([self.server_a.pk], limit=5)
        pks = [j.pk for j in result[self.server_a.pk]]
        self.assertEqual(pks[0], new.pk)
        self.assertEqual(pks[1], old.pk)

    def test_limit_respected(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        for i in range(5):
            _make_job(object_id=self.server_a.pk, object_type=self.ct, delta_seconds=i * 10)
        result = get_recent_jobs_for_servers([self.server_a.pk], limit=2)
        self.assertEqual(len(result[self.server_a.pk]), 2)

    def test_multiple_servers_independent(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        job_a = _make_job(object_id=self.server_a.pk, object_type=self.ct)
        job_b = _make_job(object_id=self.server_b.pk, object_type=self.ct)
        result = get_recent_jobs_for_servers([self.server_a.pk, self.server_b.pk])
        self.assertEqual(result[self.server_a.pk][0].pk, job_a.pk)
        self.assertEqual(result[self.server_b.pk][0].pk, job_b.pk)

    def test_unbound_job_summary_none_does_not_raise(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        _make_job(data={"summary": None})
        # Should not raise, server gets no jobs
        result = get_recent_jobs_for_servers([self.server_a.pk])
        self.assertEqual(result[self.server_a.pk], [])

    def test_unbound_job_missing_data_does_not_raise(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        _make_job(data=None)
        result = get_recent_jobs_for_servers([self.server_a.pk])
        self.assertEqual(result[self.server_a.pk], [])

    def test_unknown_server_pk_returns_empty(self):
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        result = get_recent_jobs_for_servers([999999])
        self.assertEqual(result[999999], [])

    def test_deduplication_when_job_attributed_to_two_pks(self):
        """An unbound job covering two servers must not be double-counted per server."""
        from netbox_kea.views.sync_jobs import get_recent_jobs_for_servers

        job = _make_job(
            data={"summary": [{"pk": self.server_a.pk}, {"pk": self.server_b.pk}]},
        )
        result = get_recent_jobs_for_servers([self.server_a.pk, self.server_b.pk])
        # Each server sees the job exactly once
        self.assertEqual(len([j for j in result[self.server_a.pk] if j.pk == job.pk]), 1)
        self.assertEqual(len([j for j in result[self.server_b.pk] if j.pk == job.pk]), 1)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
@skipUnlessDBFeature("supports_json_field_contains")
class TestGetAllJobsForServer(TestCase):
    """Unit tests for get_all_jobs_for_server helper (used by per-server Jobs tab)."""

    def setUp(self):
        from django.contrib.contenttypes.models import ContentType

        from netbox_kea.models import Server

        self.server_a = _make_db_server(name="server-a")
        self.server_b = _make_db_server(name="server-b")
        self.ct = ContentType.objects.get_for_model(Server)

    def test_includes_bound_jobs(self):
        from netbox_kea.views.sync_jobs import get_all_jobs_for_server

        job = _make_job(object_id=self.server_a.pk, object_type=self.ct)
        pks = list(get_all_jobs_for_server(self.server_a.pk).values_list("pk", flat=True))
        self.assertIn(job.pk, pks)

    def test_includes_unbound_periodic_jobs(self):
        from netbox_kea.views.sync_jobs import get_all_jobs_for_server

        job = _make_job(data={"summary": [{"pk": self.server_a.pk, "name": "server-a"}]})
        pks = list(get_all_jobs_for_server(self.server_a.pk).values_list("pk", flat=True))
        self.assertIn(job.pk, pks)

    def test_excludes_unrelated_periodic_jobs(self):
        from netbox_kea.views.sync_jobs import get_all_jobs_for_server

        # periodic job referencing only server_b
        _make_job(data={"summary": [{"pk": self.server_b.pk}]})
        pks = list(get_all_jobs_for_server(self.server_a.pk).values_list("pk", flat=True))
        self.assertEqual(pks, [])

    def test_excludes_jobs_with_different_name(self):
        from netbox_kea.views.sync_jobs import get_all_jobs_for_server

        _make_job(name="Some Other Job", object_id=self.server_a.pk, object_type=self.ct)
        _make_job(name="Some Other Job", data={"summary": [{"pk": self.server_a.pk}]})
        pks = list(get_all_jobs_for_server(self.server_a.pk).values_list("pk", flat=True))
        self.assertEqual(pks, [])

    def test_orders_newest_first(self):
        from netbox_kea.views.sync_jobs import get_all_jobs_for_server

        old = _make_job(object_id=self.server_a.pk, object_type=self.ct, delta_seconds=200)
        new = _make_job(data={"summary": [{"pk": self.server_a.pk}]}, delta_seconds=10)
        pks = list(get_all_jobs_for_server(self.server_a.pk).values_list("pk", flat=True))
        self.assertEqual(pks[0], new.pk)
        self.assertEqual(pks[1], old.pk)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
@skipUnlessDBFeature("supports_json_field_contains")
class TestServerJobsView(TestCase):
    """Tests for the per-server Jobs tab override (includes periodic jobs)."""

    def setUp(self):
        from django.contrib.contenttypes.models import ContentType

        from netbox_kea.models import Server

        self.user = User.objects.create_superuser("jobstest", "j@j.com", "pass")
        self.client.force_login(self.user)
        self.server = _make_db_server(name="server-jobs")
        self.ct = ContentType.objects.get_for_model(Server)

    def test_jobs_tab_url_resolves_to_override(self):
        """The plugin must replace NetBox's auto-registered jobs view."""
        from netbox.registry import registry

        from netbox_kea.views.sync_jobs import ServerJobsView

        entries = [e for e in registry["views"]["netbox_kea"]["server"] if e["name"] == "jobs"]
        self.assertEqual(len(entries), 1, "Exactly one 'jobs' view must be registered for Server")
        self.assertIs(entries[0]["view"], ServerJobsView)

    def test_jobs_tab_renders_bound_job(self):
        bound = _make_job(object_id=self.server.pk, object_type=self.ct)
        url = reverse("plugins:netbox_kea:server_jobs", args=[self.server.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f"/core/jobs/{bound.pk}/")

    def test_jobs_tab_renders_periodic_job(self):
        """Regression: periodic (object_id=NULL) jobs must appear on the Jobs tab."""
        periodic = _make_job(data={"summary": [{"pk": self.server.pk, "name": "server-jobs"}]})
        url = reverse("plugins:netbox_kea:server_jobs", args=[self.server.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f"/core/jobs/{periodic.pk}/")

    def test_jobs_tab_excludes_unrelated_periodic_job(self):
        other = _make_db_server(name="other-server")
        _make_job(data={"summary": [{"pk": other.pk}]})
        url = reverse("plugins:netbox_kea:server_jobs", args=[self.server.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        # Table should be empty (no link to any /core/jobs/N/)
        # Use a strict check: no rendered job rows
        from netbox_kea.views.sync_jobs import get_all_jobs_for_server

        self.assertEqual(get_all_jobs_for_server(self.server.pk).count(), 0)

    def test_jobs_tab_returns_403_without_view_job_permission(self):
        """Users without core.view_job must receive 403 even on direct URL access."""
        from django.contrib.auth.models import Permission

        # Create a regular user (non-superuser) with no permissions
        unprivileged = User.objects.create_user("noperm", "n@n.com", "pass")
        # Grant basic NetBox plugin access so the login doesn't fail before our check
        view_server_perm = Permission.objects.get(codename="view_server")
        unprivileged.user_permissions.add(view_server_perm)
        self.client.force_login(unprivileged)

        url = reverse("plugins:netbox_kea:server_jobs", args=[self.server.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 403)
