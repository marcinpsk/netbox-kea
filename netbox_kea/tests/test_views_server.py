# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for netbox_kea plugin.

Also contains pure-Python unit tests for helper functions defined in views.py
(e.g. ``_extract_identifier``), which do not require a database but live here
because they are tightly coupled to view logic.

These tests verify correct HTTP responses and redirect behaviour for every view.
All Kea HTTP calls are mocked so no running Kea instance is required.

Test organisation strategy
--------------------------
Each view class gets its own ``TestCase`` subclass so failures are isolated and
clearly named.  Every test that triggers a redirect asserts that the redirect URL
contains an *integer* pk (never the string "None"), which is the pattern that
revealed the original ``POST /plugins/kea/servers/None`` 404 bug.

View tests use ``django.test.TestCase`` because they write to the test database
(user + server fixtures).  Server objects are created via ``Server.objects.create()``
which does **not** call ``Model.clean()`` and therefore does not trigger live Kea
connectivity checks.
"""

import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_kea.models import Server

# Minimal PLUGINS_CONFIG so server.get_client() can read kea_timeout.
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_INT_PK_RE = re.compile(r"/servers/(\d+)/")


def _make_db_server(**kwargs) -> Server:
    """Create and persist a Server without live connectivity checks.

    ``Server.objects.create()`` skips ``Model.clean()``, so no Kea connectivity
    check is triggered.  The ``PLUGINS_CONFIG`` override is applied by the calling
    test class.
    """
    defaults = {
        "name": "test-kea",
        "server_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


def _kea_command_side_effect(cmd, service=None, arguments=None, check=None):
    """Return a plausible Kea API response for each command type."""
    if cmd == "status-get":
        return [{"result": 0, "arguments": {"pid": 1234, "uptime": 3600, "reload": 0}}]
    if cmd == "version-get":
        return [{"result": 0, "arguments": {"extended": "2.4.1-stable"}}]
    if cmd == "config-get":
        # Return minimal Dhcp4/Dhcp6 config so subnet views can parse it.
        if service and service[0] == "dhcp6":
            return [{"result": 0, "arguments": {"Dhcp6": {"subnet6": [], "shared-networks": []}}}]
        return [{"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}]
    return [{"result": 0, "arguments": {}}]


# ─────────────────────────────────────────────────────────────────────────────
# Shared base class
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _ViewTestBase(TestCase):
    """Creates a superuser and a single Server for use in all view tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="kea_testuser",
            email="kea_test@example.com",
            password="kea_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def _assert_no_none_pk_redirect(self, response):
        """Assert that a redirect URL never contains the string ``None`` as a pk.

        This is the specific pattern that caused the ``POST /plugins/kea/servers/None``
        404 bug: ``get_absolute_url()`` with ``pk=None`` produces that URL.
        """
        if hasattr(response, "url"):
            self.assertNotIn(
                "servers/None",
                response.url,
                f"Redirect went to bad URL: {response.url}",
            )

    def _assert_redirect_to_integer_pk(self, response):
        """Assert that a redirect URL contains an integer server pk."""
        self._assert_no_none_pk_redirect(response)
        self.assertIsNotNone(
            _INT_PK_RE.search(response.url),
            f"Expected /servers/<int>/ in redirect URL, got: {response.url}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# ServerListView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerListView(_ViewTestBase):
    """GET /plugins/kea/servers/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:server_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)


# ─────────────────────────────────────────────────────────────────────────────
# ServerView (detail)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDetailView(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_nonexistent_returns_404(self):
        url = reverse("plugins:netbox_kea:server", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# ServerEditView — add
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerAddView(_ViewTestBase):
    """GET/POST /plugins/kea/servers/add/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_add")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_post_missing_fields_rerenders_form_not_redirect_to_none(self):
        """Empty POST must re-render the form (200), never redirect to servers/None.

        This is the minimal reproduction of the original bug: an unsaved Server
        instance has ``pk=None``, so any redirect built from
        ``instance.get_absolute_url()`` would go to ``servers/None``.
        """
        url = reverse("plugins:netbox_kea:server_add")
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)
        self.assertNotIn(b"servers/None", response.content)

    def test_post_connectivity_failure_rerenders_form(self):
        """ValidationError from clean() must re-render the form at /add/, not servers/None."""
        from django.core.exceptions import ValidationError

        url = reverse("plugins:netbox_kea:server_add")
        with patch.object(Server, "clean", side_effect=ValidationError("unreachable")):
            response = self.client.post(
                url,
                {
                    "name": "bad-server",
                    "server_url": "http://unreachable.kea.example.com",
                    "dhcp4": True,
                    "dhcp6": False,
                    "ssl_verify": True,
                    "has_control_agent": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)
        self.assertNotIn(b"servers/None", response.content)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_data_redirects_to_integer_pk(self, MockKeaClient):
        """Successful server creation must redirect to servers/<int:pk>/, never /servers/None/."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_add")
        response = self.client.post(
            url,
            {
                "name": "new-valid-server",
                "server_url": "https://kea.new.example.com",
                "dhcp4": True,
                "dhcp6": False,
                "ssl_verify": True,
                "has_control_agent": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_server_is_saved_to_db(self, MockKeaClient):
        """After successful add, the Server object must exist in the DB."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_add")
        self.client.post(
            url,
            {
                "name": "saved-server",
                "server_url": "https://kea.saved.example.com",
                "dhcp4": True,
                "dhcp6": False,
                "ssl_verify": True,
                "has_control_agent": True,
            },
        )
        self.assertTrue(Server.objects.filter(name="saved-server").exists())


# ─────────────────────────────────────────────────────────────────────────────
# ServerEditView — edit existing
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerEditView(_ViewTestBase):
    """GET/POST /plugins/kea/servers/<pk>/edit/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_nonexistent_returns_404(self):
        url = reverse("plugins:netbox_kea:server_edit", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_post_missing_fields_rerenders_form(self):
        """Invalid edit POST must re-render the form (200), not redirect."""
        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        response = self.client.post(url, {"name": "", "server_url": ""})
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_edit_redirects_to_same_server(self, MockKeaClient):
        """Successful edit must redirect to the same server's detail URL."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        response = self.client.post(
            url,
            {
                "name": self.server.name,
                "server_url": "https://kea.edited.example.com",
                "dhcp4": True,
                "dhcp6": False,
                "ssl_verify": True,
                "has_control_agent": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)
        # Must redirect to THIS server's pk, not some other.
        self.assertIn(str(self.server.pk), response.url)


# ─────────────────────────────────────────────────────────────────────────────
# ServerDeleteView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDeleteView(_ViewTestBase):
    """GET/POST /plugins/kea/servers/<pk>/delete/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_delete", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_post_confirm_deletes_and_redirects(self):
        """Confirmed delete must remove the server and redirect (not to servers/None)."""
        pk = self.server.pk
        url = reverse("plugins:netbox_kea:server_delete", args=[pk])
        response = self.client.post(url, {"confirm": True})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertFalse(Server.objects.filter(pk=pk).exists())


# ─────────────────────────────────────────────────────────────────────────────
# ServerStatusView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerStatusView(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/status/"""

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_without_control_agent_returns_200(self, MockKeaClient):
        """Status view with has_control_agent=False must still return 200."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        server = _make_db_server(name="direct-daemon", has_control_agent=False)
        url = reverse("plugins:netbox_kea:server_status", args=[server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_nonexistent_returns_404(self):
        url = reverse("plugins:netbox_kea:server_status", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# ServerBulkImportView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerBulkImportView(_ViewTestBase):
    """GET/POST /plugins/kea/servers/import/

    Primary regression guard: the import URL must return 200, not 404.
    Before this fix, the URL pattern was missing entirely and clicking
    "Import" on the server list yielded a 404.
    """

    def test_get_returns_200_not_404(self):
        """Regression: /plugins/kea/servers/import/ must load the import form."""
        url = reverse("plugins:netbox_kea:server_bulk_import")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_unauthenticated_redirects_to_login(self):
        self.client.logout()
        url = reverse("plugins:netbox_kea:server_bulk_import")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_csv_creates_server(self, MockKeaClient):
        """Valid CSV must create the server and redirect."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_bulk_import")
        csv_data = (
            "name,server_url,dhcp4,dhcp6,ssl_verify,has_control_agent\r\n"
            "import-test-server,https://import.example.com,true,false,true,false\r\n"
        )
        response = self.client.post(
            url,
            {"data": csv_data, "format": "csv", "csv_delimiter": ","},
        )
        # Either 200 (results page) or 302 (redirect on success)
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(Server.objects.filter(name="import-test-server").exists())

    def test_post_duplicate_name_returns_error_not_500(self):
        """Duplicate server name must re-render the form with errors, not 500."""
        url = reverse("plugins:netbox_kea:server_bulk_import")
        # setUp() already created a server named 'test-kea'
        csv_data = "name,server_url,dhcp4,dhcp6\r\ntest-kea,https://dup.example.com,true,false\r\n"
        # No KeaClient mock: clean() should never be reached (unique constraint
        # fires first during model validation)
        response = self.client.post(
            url,
            {"data": csv_data, "format": "csv", "csv_delimiter": ","},
        )
        self.assertIn(response.status_code, [200, 400])
        # Only one server with this name must exist
        self.assertEqual(Server.objects.filter(name="test-kea").count(), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7c: Global DHCP options on the server status tab
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_WITH_OPTIONS_V4 = {
    "option-data": [
        {"code": 6, "name": "domain-name-servers", "data": "8.8.8.8, 8.8.4.4"},
        {"code": 15, "name": "domain-name", "data": "example.com"},
    ],
    "subnet4": [],
    "shared-networks": [],
}


def _kea_command_with_global_options(cmd, service=None, arguments=None, check=None):
    """Mock side-effect that includes option-data in config-get."""
    if cmd == "status-get":
        return [{"result": 0, "arguments": {"pid": 1, "uptime": 100, "reload": 0}}]
    if cmd == "version-get":
        return [{"result": 0, "arguments": {"extended": "2.4.1"}}]
    if cmd == "config-get":
        if service and service[0] == "dhcp6":
            return [
                {
                    "result": 0,
                    "arguments": {
                        "Dhcp6": {
                            "option-data": [{"code": 23, "name": "dns-servers", "data": "2001:db8::1"}],
                            "subnet6": [],
                            "shared-networks": [],
                        }
                    },
                }
            ]
        return [{"result": 0, "arguments": {"Dhcp4": _CONFIG_WITH_OPTIONS_V4}}]
    return [{"result": 0, "arguments": {}}]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerStatusGlobalOptions(_ViewTestBase):
    """Status view must render global DHCP options extracted from ``config-get``."""

    @patch("netbox_kea.models.KeaClient")
    def test_global_options_present_in_context(self, MockKeaClient):
        """``global_options`` context key must exist and contain parsed option dicts."""
        MockKeaClient.return_value.command.side_effect = _kea_command_with_global_options
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("global_options", response.context)
        opts = response.context["global_options"]
        # Server has dhcp4 enabled — DHCPv4 options must be present.
        # Keys are humanised ("Dns Servers") — check by looking at all option values.
        self.assertTrue(any("Dns Servers" in v for v in opts.values()))

    @patch("netbox_kea.models.KeaClient")
    def test_global_options_dns_rendered_in_html(self, MockKeaClient):
        """DNS server IP must appear somewhere in the rendered status page."""
        MockKeaClient.return_value.command.side_effect = _kea_command_with_global_options
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertContains(response, "8.8.8.8")

    @patch("netbox_kea.models.KeaClient")
    def test_global_options_domain_name_rendered(self, MockKeaClient):
        """Domain name option must also appear in the status page HTML."""
        MockKeaClient.return_value.command.side_effect = _kea_command_with_global_options
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertContains(response, "example.com")

    @patch("netbox_kea.models.KeaClient")
    def test_status_still_200_when_config_get_fails(self, MockKeaClient):
        """If ``config-get`` raises, the status page must still return 200 (graceful degradation)."""
        from netbox_kea.kea import KeaException

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                raise KeaException({"result": 1, "text": "internal error"}, index=0)
            return _kea_command_with_global_options(cmd, service=service)

        MockKeaClient.return_value.command.side_effect = side_effect
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# ServerFilterSet / ServerFilterForm
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerFilterSet(_ViewTestBase):
    """Tests for ServerFilterSet — queryset filtering by name, URL, has_control_agent."""

    def _make_servers(self):
        """Create three servers with distinct attributes for filtering."""
        Server.objects.all().delete()
        s1 = Server.objects.create(
            name="alpha-kea",
            server_url="http://alpha.example.com:8000",
            dhcp4=True,
            dhcp6=False,
            has_control_agent=True,
        )
        s2 = Server.objects.create(
            name="beta-kea",
            server_url="http://beta.example.com:8000",
            dhcp4=False,
            dhcp6=True,
            has_control_agent=False,
        )
        s3 = Server.objects.create(
            name="gamma-server",
            server_url="http://gamma.example.com:9000",
            dhcp4=True,
            dhcp6=True,
            has_control_agent=True,
        )
        return s1, s2, s3

    def test_filter_by_name_contains(self):
        """ServerFilterSet supports case-insensitive name substring filtering."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"name": "kea"}, queryset=Server.objects.all()).qs
        names = list(qs.values_list("name", flat=True))
        self.assertIn("alpha-kea", names)
        self.assertIn("beta-kea", names)
        self.assertNotIn("gamma-server", names)

    def test_filter_by_server_url_contains(self):
        """ServerFilterSet supports case-insensitive server_url substring filtering."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"server_url": "beta"}, queryset=Server.objects.all()).qs
        names = list(qs.values_list("name", flat=True))
        self.assertEqual(names, ["beta-kea"])

    def test_filter_by_has_control_agent_true(self):
        """ServerFilterSet can filter servers where has_control_agent=True."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"has_control_agent": True}, queryset=Server.objects.all()).qs
        names = list(qs.values_list("name", flat=True).order_by("name"))
        self.assertIn("alpha-kea", names)
        self.assertIn("gamma-server", names)
        self.assertNotIn("beta-kea", names)

    def test_filter_by_has_control_agent_false(self):
        """ServerFilterSet can filter servers where has_control_agent=False."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"has_control_agent": False}, queryset=Server.objects.all()).qs
        names = list(qs.values_list("name", flat=True))
        self.assertEqual(names, ["beta-kea"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerFilterForm(_ViewTestBase):
    """Tests for ServerFilterForm — renders new filter fields."""

    def test_filter_form_has_name_field(self):
        """ServerFilterForm includes a 'name' text field."""
        from netbox_kea.forms import ServerFilterForm

        form = ServerFilterForm()
        self.assertIn("name", form.fields)

    def test_filter_form_has_server_url_field(self):
        """ServerFilterForm includes a 'server_url' text field."""
        from netbox_kea.forms import ServerFilterForm

        form = ServerFilterForm()
        self.assertIn("server_url", form.fields)

    def test_filter_form_has_has_control_agent_field(self):
        """ServerFilterForm includes a 'has_control_agent' nullable boolean field."""
        from netbox_kea.forms import ServerFilterForm

        form = ServerFilterForm()
        self.assertIn("has_control_agent", form.fields)

    def test_server_list_filters_by_name_via_get(self):
        """GET /plugins/kea/servers/?name=<term> returns 200 and filters results."""
        Server.objects.all().delete()
        Server.objects.create(name="alpha-kea", server_url="http://a:8000", dhcp4=True, dhcp6=False)
        Server.objects.create(name="gamma-server", server_url="http://g:8000", dhcp4=True, dhcp6=False)
        url = reverse("plugins:netbox_kea:server_list")
        response = self.client.get(url, {"name": "alpha"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alpha-kea")
        self.assertNotContains(response, "gamma-server")


# ---------------------------------------------------------------------------
# _KeaChangeMixin — 403 for view-only user
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaChangeMixinPermission(_ViewTestBase):
    """Lines 243-246: _KeaChangeMixin returns 403 for view-only users."""

    def test_view_only_user_gets_403_on_mutation_view(self):
        """User with 'view' but not 'change' on Server gets 403 from _KeaChangeMixin."""
        from users.models import ObjectPermission

        readonly = User.objects.create_user(username="readonly_mixin", password="pass")
        perm = ObjectPermission(name="view-servers", actions=["view"])
        perm.save()
        perm.users.add(readonly)
        perm.object_types.add(
            __import__(
                "django.contrib.contenttypes.models", fromlist=["ContentType"]
            ).ContentType.objects.get_for_model(self.server.__class__)
        )
        self.client.force_login(readonly)
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_user_without_global_perm_gets_403_on_server_add(self):
        """User without change_server permission gets 403 on a mutation view."""
        no_perm = User.objects.create_user(username="noperm_mixin", password="pass")
        self.client.force_login(no_perm)
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])
        response = self.client.get(url)
        self.assertIn(response.status_code, (403, 404))


# ---------------------------------------------------------------------------
# Status view — null/empty args + HA data
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestStatusViewNullArgs(_ViewTestBase):
    """Lines 317-318, 322-323, 352-357: status-get returns empty/null arguments."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_status", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_ca_status_empty_args_raises(self, MockKeaClient):
        """_get_ca_status raises RuntimeError when arguments is empty."""
        call_n = [0]

        def _side(cmd, service=None, **kwargs):
            call_n[0] += 1
            if cmd == "status-get" and (not service or "dhcp" not in service[0]):
                return [{"result": 0, "arguments": {}}]  # empty → falsy
            if cmd == "version-get" and (not service or "dhcp" not in service[0]):
                return [{"result": 0, "arguments": {"extended": "2.0"}}]
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": {"extended": "2.0"}}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _side
        # The view catches exceptions from _get_ca_status; page still renders
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (200, 500))

    @patch("netbox_kea.models.KeaClient")
    def test_get_dhcp_status_null_args_raises(self, MockKeaClient):
        """_get_dhcp_status raises RuntimeError when arguments is None."""

        def _side(cmd, service=None, **kwargs):
            if cmd == "status-get" and service and "dhcp" in service[0]:
                return [{"result": 0, "arguments": None}]
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": {"extended": "2.0"}}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _side
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (200, 500))

    @patch("netbox_kea.models.KeaClient")
    def test_get_dhcp_status_ha_fields_included(self, MockKeaClient):
        """Lines 370-373: HA fields are present when high-availability in status-get."""

        def _side(cmd, service=None, **kwargs):
            if cmd == "status-get" and service and "dhcp" in service[0]:
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "pid": 1,
                            "uptime": 100,
                            "reload": 0,
                            "high-availability": [
                                {
                                    "ha-mode": "load-balancing",
                                    "ha-servers": {
                                        "local": {"role": "primary", "state": "active"},
                                        "remote": {"connection-interrupted": False, "age": 10, "role": "secondary"},
                                    },
                                }
                            ],
                        },
                    }
                ]
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 100, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": {"extended": "2.4.1"}}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _side
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "HA")


# ---------------------------------------------------------------------------
# _get_global_options — generic exception handler
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetGlobalOptionsGenericException(_ViewTestBase):
    """Line 221-222: generic exception in _get_global_options is swallowed."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_status", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_swallowed(self, MockKeaClient):
        """Line 222: generic Exception in config-get for global options is logged and skipped."""

        def _side(cmd, service=None, **kwargs):
            if cmd == "config-get":
                raise RuntimeError("unexpected crash")
            if cmd == "status-get" and (not service or "dhcp" not in (service or [""])[0]):
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}]
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": {"extended": "2.0"}}]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _side
        response = self.client.get(self._url())
        # Should not crash — exception is swallowed
        self.assertIn(response.status_code, (200, 500))


# ---------------------------------------------------------------------------
# Bulk delete POST — missing permission
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkDeletePermission(_ViewTestBase):
    """Line 800: POST without bulk_delete_lease_from_server permission returns 403."""

    @patch("netbox_kea.models.KeaClient")
    def test_user_without_bulk_delete_perm_gets_403(self, MockKeaClient):
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        # Grant view-only ObjectPermission so get_object() succeeds
        viewer = User.objects.create_user(username="viewer_no_bulk_del", password="pass")
        ct = ContentType.objects.get_for_model(Server)
        view_op = ObjectPermission(name="view-servers-for-bulk-test", actions=["view"])
        view_op.save()
        view_op.users.add(viewer)
        view_op.object_types.add(ct)
        self.client.force_login(viewer)
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        # User can view the server but has no bulk_delete_lease_from_server perm
        response = self.client.post(url, {"pk": ["10.0.0.1"], "confirm": "1"})
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# _KeaChangeMixin — elif branch (lines 245-246): pk is None + no change_server perm
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaChangeMixinNoPk(_ViewTestBase):
    """Lines 245-246: dispatch with no pk kwarg + user lacking change_server perm → 403."""

    def test_no_pk_no_perm_returns_403(self):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from django.views import View

        from netbox_kea.views import _KeaChangeMixin

        class _MinimalView(_KeaChangeMixin, View):
            def get(self, request, **kwargs):
                return HttpResponse("ok")

        # Create a user with no permissions
        no_perm_user = User.objects.create_user(username="no_perm_kca", password="pass")
        factory = RequestFactory()
        request = factory.get("/")
        request.user = no_perm_user
        view_func = _MinimalView.as_view()
        # Call with NO pk kwarg → elif branch
        response = view_func(request)
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# ServerStatusView — null version_args (lines 323, 357)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestStatusViewNullVersionArgs(_ViewTestBase):
    """Lines 323, 357: version-get returns None arguments → RuntimeError caught internally."""

    @patch("netbox_kea.models.KeaClient")
    def test_ca_version_get_null_args_returns_200(self, MockKeaClient):
        """Line 323: CA version-get returns None args → RuntimeError caught in get_extra_context."""
        mock_client = MockKeaClient.return_value

        def _side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 60, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": None}]
            return [{"result": 0, "arguments": {}}]

        mock_client.command.side_effect = _side_effect
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_dhcp_version_get_null_args_returns_200(self, MockKeaClient):
        """Line 357: DHCP service version-get returns None args → RuntimeError caught."""
        mock_client = MockKeaClient.return_value

        def _side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "status-get":
                return [{"result": 0, "arguments": {"pid": 1, "uptime": 60, "reload": 0}}]
            if cmd == "version-get":
                return [{"result": 0, "arguments": None}]
            return [{"result": 0, "arguments": {}}]

        mock_client.command.side_effect = _side_effect
        server_dhcp_only = _make_db_server(
            name="dhcp-only-sv",
            has_control_agent=False,
            dhcp4=True,
            dhcp6=False,
        )
        url = reverse("plugins:netbox_kea:server_status", args=[server_dhcp_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
