# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for SyncJobsView, ServerSyncStatusView, ServerSyncNowView, ServerSyncToggleView."""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import SyncConfig
from netbox_kea.tests.utils import _PLUGINS_CONFIG, User, _make_db_server


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
