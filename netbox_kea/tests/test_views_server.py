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

from unittest.mock import patch

import requests
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from django.urls import reverse

from netbox_kea.models import Server

from .kea_stub import stub_kea
from .utils import _PLUGINS_CONFIG, User, _make_db_server, _ViewTestBase

# ---------------------------------------------------------------------------
# Kea response builders (real KeaClient + HTTP-boundary stub)
# ---------------------------------------------------------------------------
#
# Server.clean() runs a live connectivity check (``version-get`` per enabled
# service), and the status view issues ``status-get`` + ``version-get`` per
# daemon (CA-level = no service; DHCP = service=["dhcp{v}"]) plus ``config-get``
# for the global-options block. The stub keys by command name only, so where a
# test needs CA-vs-service-specific responses it registers a callable
# ``(body) -> payload`` that branches on ``body.get("service")``.

_VERSION_OK = {"result": 0, "arguments": {"extended": "3.2.0"}}


def _ok_status(**args):
    """A ``status-get`` payload (result 0) with sensible daemon defaults."""
    base = {"pid": 1234, "uptime": 3600, "reload": 0}
    base.update(args)
    return {"result": 0, "arguments": base}


def _config_get_empty(body):
    """A ``config-get`` payload with no global option-data, for the queried service."""
    svc = (body.get("service") or [""])[0]
    version = 6 if svc == "dhcp6" else 4
    key = f"Dhcp{version}"
    return {"result": 0, "arguments": {key: {"option-data": [], f"subnet{version}": [], "shared-networks": []}}}


def _status_stub(**overrides):
    """Stub the happy-path status view: status-get + version-get + config-get."""
    base = {"status-get": _ok_status(), "version-get": _VERSION_OK, "config-get": _config_get_empty}
    base.update(overrides)
    return stub_kea(base)


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
                    "ca_url": "http://unreachable.kea.example.com",
                    "dhcp4": True,
                    "dhcp6": False,
                    "ssl_verify": True,
                    "has_control_agent": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)
        self.assertNotIn(b"servers/None", response.content)

    def test_post_valid_data_redirects_to_integer_pk(self):
        """Successful server creation must redirect to servers/<int:pk>/, never /servers/None/."""
        # Server.clean() runs a live version-get connectivity check on the enabled service.
        url = reverse("plugins:netbox_kea:server_add")
        with stub_kea({"version-get": _VERSION_OK}):
            response = self.client.post(
                url,
                {
                    "name": "new-valid-server",
                    "ca_url": "https://kea.new.example.com",
                    "dhcp4": True,
                    "dhcp6": False,
                    "ssl_verify": True,
                    "has_control_agent": True,
                },
            )
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)

    def test_post_valid_server_is_saved_to_db(self):
        """After successful add, the Server object must exist in the DB."""
        url = reverse("plugins:netbox_kea:server_add")
        with stub_kea({"version-get": _VERSION_OK}):
            self.client.post(
                url,
                {
                    "name": "saved-server",
                    "ca_url": "https://kea.saved.example.com",
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
        response = self.client.post(url, {"name": "", "ca_url": ""})
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)

    def test_post_valid_edit_redirects_to_same_server(self):
        """Successful edit must redirect to the same server's detail URL."""
        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        with stub_kea({"version-get": _VERSION_OK}):
            response = self.client.post(
                url,
                {
                    "name": self.server.name,
                    "ca_url": "https://kea.edited.example.com",
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

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        with _status_stub():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_without_control_agent_returns_200(self):
        """Status view with has_control_agent=False must still return 200."""
        server = _make_db_server(name="direct-daemon", has_control_agent=False)
        url = reverse("plugins:netbox_kea:server_status", args=[server.pk])
        with _status_stub():
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

    def test_post_valid_csv_creates_server(self):
        """Valid CSV must create the server and redirect."""
        # Server.clean() runs a live version-get connectivity check per imported row.
        url = reverse("plugins:netbox_kea:server_bulk_import")
        csv_data = (
            "name,ca_url,dhcp4,dhcp6,ssl_verify,has_control_agent\r\n"
            "import-test-server,https://import.example.com,true,false,true,false\r\n"
        )
        with stub_kea({"version-get": _VERSION_OK}):
            response = self.client.post(
                url,
                {"data": csv_data, "format": "csv", "csv_delimiter": ","},
            )
        # Either 200 (results page) or 302 (redirect on success)
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(Server.objects.filter(name="import-test-server").exists())

    def test_post_duplicate_name_returns_error_not_500(self):
        """Duplicate server name must re-render the form with errors, not 500.

        The real clean() connectivity check (version-get) runs first, then the unique
        constraint on name fires — this guards against a regression where the constraint
        error would surface as a 500 instead of a form error.
        """
        url = reverse("plugins:netbox_kea:server_bulk_import")
        # setUp() already created a server named 'test-kea'
        csv_data = "name,ca_url,dhcp4,dhcp6\r\ntest-kea,https://dup.example.com,true,false\r\n"
        with stub_kea({"version-get": _VERSION_OK}):
            response = self.client.post(
                url,
                {"data": csv_data, "format": "csv", "csv_delimiter": ","},
            )
        self.assertIn(response.status_code, [200, 400])
        # Only one server with this name must exist
        self.assertEqual(Server.objects.filter(name="test-kea").count(), 1)
        # Duplicate name must produce a form error, not a 500
        form = response.context.get("form")
        self.assertIsNotNone(form, "Form must be present in response context for error display")
        self.assertTrue(
            any(
                "name" in str(e).lower() or "already" in str(e).lower()
                for e in (form.errors.get("name", []) or form.non_field_errors())
            ),
            "Expected duplicate name error in form",
        )


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


def _config_get_with_options(body):
    """A ``config-get`` payload carrying global option-data for the queried service."""
    svc = (body.get("service") or [""])[0]
    if svc == "dhcp6":
        return {
            "result": 0,
            "arguments": {
                "Dhcp6": {
                    "option-data": [{"code": 23, "name": "dns-servers", "data": "2001:db8::1"}],
                    "subnet6": [],
                    "shared-networks": [],
                }
            },
        }
    return {"result": 0, "arguments": {"Dhcp4": _CONFIG_WITH_OPTIONS_V4}}


def _global_options_stub(**overrides):
    """Status view stub whose config-get exposes global option-data (v2.4.1 daemon)."""
    base = {
        "status-get": {"result": 0, "arguments": {"pid": 1, "uptime": 100, "reload": 0}},
        "version-get": {"result": 0, "arguments": {"extended": "2.4.1"}},
        "config-get": _config_get_with_options,
    }
    base.update(overrides)
    return stub_kea(base)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerStatusGlobalOptions(_ViewTestBase):
    """Status view must render global DHCP options extracted from ``config-get``."""

    def test_global_options_present_in_context(self):
        """``global_options`` context key must exist and contain parsed option dicts."""
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        with _global_options_stub():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("global_options", response.context)
        opts = response.context["global_options"]
        # Server has dhcp4 enabled — DHCPv4 options must be present specifically under the "DHCPv4" key.
        # DHCPv4 code 6 (domain-name-servers) maps to "dns_servers" → "Dns Servers" after title-case.
        # Assert both the key and the underlying option value from the fixture.
        self.assertIn("Dns Servers", opts.get("DHCPv4", {}))
        self.assertEqual(opts.get("DHCPv4", {}).get("Dns Servers"), "8.8.8.8, 8.8.4.4")

    def test_global_options_dns_rendered_in_html(self):
        """DNS server IP must appear somewhere in the rendered status page."""
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        with _global_options_stub():
            response = self.client.get(url)
        self.assertContains(response, "8.8.8.8")

    def test_global_options_domain_name_rendered(self):
        """Domain name option must also appear in the status page HTML."""
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        with _global_options_stub():
            response = self.client.get(url)
        self.assertContains(response, "example.com")

    def test_status_still_200_when_config_get_fails(self):
        """If ``config-get`` raises, the status page must still return 200 (graceful degradation)."""
        # config-get result 1 → real KeaException → _get_global_options swallows it → {}.
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        with _global_options_stub(**{"config-get": {"result": 1, "text": "internal error"}}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["global_options"], {})


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
            ca_url="http://alpha.example.com:8000",
            dhcp4=True,
            dhcp6=False,
            has_control_agent=True,
        )
        s2 = Server.objects.create(
            name="beta-kea",
            ca_url="http://beta.example.com:8000",
            dhcp4=False,
            dhcp6=True,
            has_control_agent=False,
        )
        s3 = Server.objects.create(
            name="gamma-server",
            ca_url="http://gamma.example.com:9000",
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

    def test_filter_by_ca_url_contains(self):
        """ServerFilterSet supports case-insensitive ca_url substring filtering."""
        from netbox_kea.filtersets import ServerFilterSet

        self._make_servers()
        qs = ServerFilterSet({"ca_url": "beta"}, queryset=Server.objects.all()).qs
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

    def test_filter_form_has_ca_url_field(self):
        """ServerFilterForm includes a 'ca_url' text field."""
        from netbox_kea.forms import ServerFilterForm

        form = ServerFilterForm()
        self.assertIn("ca_url", form.fields)

    def test_filter_form_has_has_control_agent_field(self):
        """ServerFilterForm includes a 'has_control_agent' nullable boolean field."""
        from netbox_kea.forms import ServerFilterForm

        form = ServerFilterForm()
        self.assertIn("has_control_agent", form.fields)

    def test_server_list_filters_by_name_via_get(self):
        """GET /plugins/kea/servers/?name=<term> returns 200 and filters results."""
        Server.objects.all().delete()
        Server.objects.create(name="alpha-kea", ca_url="http://a:8000", dhcp4=True, dhcp6=False)
        Server.objects.create(name="gamma-server", ca_url="http://g:8000", dhcp4=True, dhcp6=False)
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
        perm.object_types.add(ContentType.objects.get_for_model(Server))
        self.client.force_login(readonly)
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_user_without_any_perm_gets_404_on_mutation_view(self):
        """User with no permissions gets 404 on a mutation view (pk lookup fails restrict check)."""
        no_perm = User.objects.create_user(username="noperm_mixin", password="pass")
        self.client.force_login(no_perm)
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Status view — null/empty args + HA data
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestStatusViewNullArgs(_ViewTestBase):
    """Lines 317-318, 322-323, 352-357: status-get returns empty/null arguments."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_status", args=[self.server.pk])

    def test_get_ca_status_empty_args_degrades_gracefully(self):
        """_get_ca_status degrades gracefully when arguments is empty."""

        def _status(body):
            svc = body.get("service")
            if not svc or "dhcp" not in svc[0]:
                return {"result": 0, "arguments": {}}  # CA → empty → RuntimeError
            return {"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}

        stub = {"status-get": _status, "version-get": {"result": 0, "arguments": {"extended": "2.0"}}}
        # The view catches exceptions from _get_ca_status; page still renders with empty statuses
        with _status_stub(**stub):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        services_by_name = {s["name"]: s for s in response.context["services"]}
        if "Control Agent" in services_by_name:
            self.assertEqual(services_by_name["Control Agent"]["status_data"], {})

    def test_get_dhcp_status_null_args_degrades_gracefully(self):
        """_get_dhcp_status degrades gracefully when arguments is None."""

        def _status(body):
            svc = body.get("service")
            if svc and "dhcp" in svc[0]:
                return {"result": 0, "arguments": None}  # dhcp → None → RuntimeError
            return {"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}}

        stub = {"status-get": _status, "version-get": {"result": 0, "arguments": {"extended": "2.0"}}}
        with _status_stub(**stub):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        # When arguments is None, _get_dhcp_status raises RuntimeError and the view
        # falls back to empty status_data — the service entry must still exist.
        services_by_name = {s["name"]: s for s in response.context["services"]}
        self.assertIn("DHCPv4", services_by_name)
        self.assertEqual(
            services_by_name["DHCPv4"]["status_data"],
            {},
            "DHCPv4 status_data must be empty/degraded when arguments is None",
        )

    def test_get_dhcp_status_ha_fields_included(self):
        """HA fields are present when high-availability is in status-get."""

        def _status(body):
            svc = body.get("service")
            if svc and "dhcp" in svc[0]:
                return {
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
            return {"result": 0, "arguments": {"pid": 1, "uptime": 100, "reload": 0}}

        stub = {"status-get": _status, "version-get": {"result": 0, "arguments": {"extended": "2.4.1"}}}
        with _status_stub(**stub):
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

    def test_generic_exception_swallowed(self):
        """A transport error in config-get for global options is swallowed; the page renders normally."""
        # config-get raises a transport error at the boundary → _get_global_options swallows it → {}.
        stub = {
            "status-get": {"result": 0, "arguments": {"pid": 1, "uptime": 0, "reload": 0}},
            "version-get": {"result": 0, "arguments": {"extended": "2.0"}},
            "config-get": requests.RequestException("unexpected crash"),
        }
        with stub_kea(stub):
            response = self.client.get(self._url())
        # Exception in get_extra_context() is caught by the outer try/except in get_extra_context;
        # the page must still render as 200 (degraded state, not 500).
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["global_options"], {})


# ---------------------------------------------------------------------------
# Bulk delete POST — missing permission
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestBulkDeletePermission(_ViewTestBase):
    """Line 800: POST without bulk_delete_lease_from_server permission returns 403."""

    def test_user_without_bulk_delete_perm_gets_403(self):
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
        # Permission check returns 403 before any Kea traffic.
        with stub_kea({}) as kea:
            response = self.client.post(url, {"pk": ["10.0.0.1"], "confirm": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(kea.commands(), [])


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

    def test_ca_version_get_null_args_returns_200(self):
        """CA version-get returns None args → RuntimeError caught in get_extra_context."""
        stub = {"status-get": _ok_status(uptime=60), "version-get": {"result": 0, "arguments": None}}
        url = reverse("plugins:netbox_kea:server_status", args=[self.server.pk])
        with _status_stub(**stub):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        services_by_name = {s["name"]: s for s in response.context["services"]}
        if "Control Agent" in services_by_name:
            self.assertEqual(services_by_name["Control Agent"]["status_data"], {})

    def test_dhcp_version_get_null_args_returns_200(self):
        """DHCP service version-get returns None args → RuntimeError caught."""
        stub = {"status-get": _ok_status(uptime=60), "version-get": {"result": 0, "arguments": None}}
        server_dhcp_only = _make_db_server(
            name="dhcp-only-sv",
            has_control_agent=False,
            dhcp4=True,
            dhcp6=False,
        )
        url = reverse("plugins:netbox_kea:server_status", args=[server_dhcp_only.pk])
        with _status_stub(**stub):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
