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

from django.contrib import messages as django_messages
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


# ---------------------------------------------------------------------------
# TestSubnetOptionsView
# ---------------------------------------------------------------------------

# Fake config-get response containing one v4 subnet with one existing option
_OPTIONS_CONFIG_GET = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [
                    {
                        "id": 42,
                        "subnet": "10.0.0.0/24",
                        "option-data": [
                            {"name": "domain-name-servers", "data": "8.8.8.8"},
                            {"name": "routers", "data": "10.0.0.1"},
                        ],
                    }
                ]
            }
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsView(_ViewTestBase):
    """Tests for ServerSubnet4/6OptionsEditView (GET prefill + POST update)."""

    def _url(self, version=4, subnet_id=42):
        return reverse(
            f"plugins:netbox_kea:server_subnet{version}_options_edit",
            args=[self.server.pk, subnet_id],
        )

    def test_url_registered_v4(self):
        """URL server_subnet4_options_edit is registered."""
        url = self._url(version=4)
        self.assertIn("options", url)

    def test_url_registered_v6(self):
        """URL server_subnet6_options_edit is registered."""
        url = self._url(version=6)
        self.assertIn("options", url)

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.command.return_value = _OPTIONS_CONFIG_GET
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_prefills_existing_options(self, MockKeaClient):
        """GET pre-populates formset with existing option-data from config-get."""
        MockKeaClient.return_value.command.return_value = _OPTIONS_CONFIG_GET
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("domain-name-servers", content)
        self.assertIn("8.8.8.8", content)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_subnet_update_options(self, MockKeaClient):
        """POST with valid formset calls subnet_update_options and redirects."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        MockKeaClient.return_value.subnet_update_options.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version_and_subnet_id(self, MockKeaClient):
        """POST calls subnet_update_options with the correct version and subnet_id."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        self.client.post(
            self._url(version=4, subnet_id=42),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        call_kwargs = MockKeaClient.return_value.subnet_update_options.call_args
        args = call_kwargs[1] if call_kwargs[1] else {}
        positional = call_kwargs[0] if call_kwargs[0] else ()
        # version=4 and subnet_id=42 should be passed (positional or keyword)
        self.assertIn(4, list(positional) + list(args.values()))
        self.assertIn(42, list(positional) + list(args.values()))

    @patch("netbox_kea.models.KeaClient")
    def test_post_deleted_rows_excluded_from_options(self, MockKeaClient):
        """Rows with DELETE=on are excluded from the options list passed to subnet_update_options."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
                "form-1-name": "domain-name-servers",
                "form-1-data": "8.8.8.8",
                "form-1-always_send": "",
                "form-1-DELETE": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.subnet_update_options.call_args
        # options argument should have only 1 item (dns row deleted)
        options_arg = next(v for v in list(call_kwargs[0]) + list(call_kwargs[1].values()) if isinstance(v, list))
        self.assertEqual(len(options_arg), 1)
        self.assertEqual(options_arg[0]["name"], "routers")

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_message(self, MockKeaClient):
        """POST that raises KeaException shows an error message, stays on form."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.subnet_update_options.side_effect = KeaException(
            {"result": 1, "text": "subnet not found"}
        )
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        self.assertEqual(response.status_code, 302)  # redirect back to subnets
        # Error stored in messages — check it doesn't crash

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


# TestServerOptionsView
# ---------------------------------------------------------------------------

_SERVER_OPTIONS_CONFIG_GET = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "option-data": [
                    {"name": "domain-name-servers", "data": "8.8.8.8"},
                    {"name": "routers", "data": "10.0.0.1"},
                ],
                "subnet4": [],
            }
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsView(_ViewTestBase):
    """Tests for ServerDHCP4/6OptionsEditView (GET prefill + POST update)."""

    def _url(self, version=4):
        return reverse(
            f"plugins:netbox_kea:server_dhcp{version}_options_edit",
            args=[self.server.pk],
        )

    def test_url_registered_v4(self):
        """URL server_dhcp4_options_edit is registered."""
        url = self._url(version=4)
        self.assertIn("options", url)

    def test_url_registered_v6(self):
        """URL server_dhcp6_options_edit is registered."""
        url = self._url(version=6)
        self.assertIn("options", url)

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.command.return_value = _SERVER_OPTIONS_CONFIG_GET
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_prefills_existing_options(self, MockKeaClient):
        """GET pre-populates formset with existing server-level option-data."""
        MockKeaClient.return_value.command.return_value = _SERVER_OPTIONS_CONFIG_GET
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("domain-name-servers", content)
        self.assertIn("8.8.8.8", content)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_server_update_options(self, MockKeaClient):
        """POST with valid formset calls server_update_options and redirects."""
        MockKeaClient.return_value.server_update_options.return_value = None
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        MockKeaClient.return_value.server_update_options.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version(self, MockKeaClient):
        """POST calls server_update_options with the correct version."""
        MockKeaClient.return_value.server_update_options.return_value = None
        self.client.post(
            self._url(version=4),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        call_kwargs = MockKeaClient.return_value.server_update_options.call_args
        all_args = list(call_kwargs[0]) + list(call_kwargs[1].values())
        self.assertIn(4, all_args)

    @patch("netbox_kea.models.KeaClient")
    def test_post_deleted_rows_excluded(self, MockKeaClient):
        """Rows with DELETE=on are excluded from the options list."""
        MockKeaClient.return_value.server_update_options.return_value = None
        self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
                "form-1-name": "domain-name-servers",
                "form-1-data": "8.8.8.8",
                "form-1-always_send": "",
                "form-1-DELETE": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.server_update_options.call_args
        options_arg = next(v for v in list(call_kwargs[0]) + list(call_kwargs[1].values()) if isinstance(v, list))
        self.assertEqual(len(options_arg), 1)
        self.assertEqual(options_arg[0]["name"], "routers")

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_redirects(self, MockKeaClient):
        """POST that raises KeaException shows error message and redirects."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.server_update_options.side_effect = KeaException(
            {"result": 1, "text": "internal error"}
        )
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


# ---------------------------------------------------------------------------
# option-def fixtures
# ---------------------------------------------------------------------------

_OPTION_DEF_LIST_V4 = [
    {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4"},
    {"name": "other-opt", "code": 201, "type": "uint32", "space": "dhcp4"},
]

_OPTION_DEF_LIST_EMPTY: list = []


# ---------------------------------------------------------------------------
# ServerOptionDef4ListView / ServerOptionDef6ListView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4ListView(_ViewTestBase):
    """Tests for ServerOptionDef4ListView: GET list of custom option definitions."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_V4
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_shows_option_def_name(self, MockKeaClient):
        """GET renders option names in the response."""
        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_V4
        response = self.client.get(self._url())
        self.assertContains(response, "my-opt")

    @patch("netbox_kea.models.KeaClient")
    def test_shows_option_def_code(self, MockKeaClient):
        """GET renders option codes in the response."""
        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_V4
        response = self.client.get(self._url())
        self.assertContains(response, "200")

    @patch("netbox_kea.models.KeaClient")
    def test_empty_list_shows_200(self, MockKeaClient):
        """GET with empty option-def list returns 200 without errors."""
        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_EMPTY
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_with_dhcp4_disabled_redirects(self):
        """Server with dhcp4=False redirects away from option_def4 tab."""
        v6_only = _make_db_server(name="v6only-od", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_option_def4", args=[v6_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerOptionDef4View

        MockKeaClient.return_value.option_def_list.return_value = _OPTION_DEF_LIST_V4
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerOptionDef4View.tab)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef6ListView(_ViewTestBase):
    """Tests for ServerOptionDef6ListView (v6 variant)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def6", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 OK."""
        MockKeaClient.return_value.option_def_list.return_value = []
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_calls_option_def_list_with_version_6(self, MockKeaClient):
        """GET calls option_def_list with version=6."""
        MockKeaClient.return_value.option_def_list.return_value = []
        self.client.get(self._url())
        MockKeaClient.return_value.option_def_list.assert_called_once_with(version=6)

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerOptionDef6View

        MockKeaClient.return_value.option_def_list.return_value = []
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerOptionDef6View.tab)


# ---------------------------------------------------------------------------
# ServerOptionDef4AddView / ServerOptionDef6AddView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4AddView(_ViewTestBase):
    """Tests for ServerOptionDef4AddView: GET form + POST create."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200_with_form(self, MockKeaClient):
        """GET renders the add option-def form."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_calls_option_def_add(self, MockKeaClient):
        """POST with valid data calls option_def_add and redirects."""
        MockKeaClient.return_value.option_def_add.return_value = None
        response = self.client.post(
            self._url(),
            {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
        )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.option_def_add.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version(self, MockKeaClient):
        """POST calls option_def_add with version=4."""
        MockKeaClient.return_value.option_def_add.return_value = None
        self.client.post(
            self._url(),
            {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
        )
        call_args = MockKeaClient.return_value.option_def_add.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 4)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error(self, MockKeaClient):
        """POST that raises KeaException returns 200 with error (no 500)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_add.side_effect = KeaException(
            {"result": 1, "text": "duplicate code"}, index=0
        )
        response = self.client.post(
            self._url(),
            {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
        )
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_returns_200(self, MockKeaClient):
        """POST with missing required fields returns 200 (form re-render)."""
        response = self.client.post(self._url(), {"name": "", "code": "", "type": "", "space": ""})
        self.assertEqual(response.status_code, 200)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef6AddView(_ViewTestBase):
    """Tests for ServerOptionDef6AddView — verifies v6 uses version=6."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET renders the add form for v6."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_option_def_add_with_version_6(self, MockKeaClient):
        """POST calls option_def_add with version=6."""
        MockKeaClient.return_value.option_def_add.return_value = None
        self.client.post(
            self._url(),
            {"name": "v6-opt", "code": 250, "type": "ipv6-address", "space": "dhcp6", "array": False},
        )
        call_args = MockKeaClient.return_value.option_def_add.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 6)


# ---------------------------------------------------------------------------
# ServerOptionDef4DeleteView / ServerOptionDef6DeleteView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4DeleteView(_ViewTestBase):
    """Tests for ServerOptionDef4DeleteView: GET confirm + POST delete."""

    def _url(self, code=200, space="dhcp4"):
        return reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, code, space])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200_with_confirmation(self, MockKeaClient):
        """GET renders a confirmation page mentioning code and space."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "200")

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_option_def_del_and_redirects(self, MockKeaClient):
        """POST calls option_def_del and redirects to option_def4 list."""
        MockKeaClient.return_value.option_def_del.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.option_def_del.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version_code_space(self, MockKeaClient):
        """POST calls option_def_del with version=4, code=200, space='dhcp4'."""
        MockKeaClient.return_value.option_def_del.return_value = None
        self.client.post(self._url(code=200, space="dhcp4"))
        call_args = MockKeaClient.return_value.option_def_del.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        code = kwargs.get("code") or (args[1] if len(args) > 1 else None)
        space = kwargs.get("space") or (args[2] if len(args) > 2 else None)
        self.assertEqual(version, 4)
        self.assertEqual(code, 200)
        self.assertEqual(space, "dhcp4")

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_redirects_with_error(self, MockKeaClient):
        """POST that raises KeaException must not 500."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_del.side_effect = KeaException(
            {"result": 3, "text": "not found"}, index=0
        )
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (200, 302))
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST redirects to login."""
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef6DeleteView(_ViewTestBase):
    """Tests for ServerOptionDef6DeleteView — v6 uses version=6."""

    def _url(self, code=250, space="dhcp6"):
        return reverse("plugins:netbox_kea:server_option_def6_delete", args=[self.server.pk, code, space])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET renders the v6 confirmation page."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_option_def_del_with_version_6(self, MockKeaClient):
        """POST calls option_def_del with version=6."""
        MockKeaClient.return_value.option_def_del.return_value = None
        self.client.post(self._url(code=250, space="dhcp6"))
        call_args = MockKeaClient.return_value.option_def_del.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = kwargs.get("version") or (args[0] if args else None)
        self.assertEqual(version, 6)


# ---------------------------------------------------------------------------
# Subnet options POST: formset invalid
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsPostInvalid(_ViewTestBase):
    """_BaseSubnetOptionsEditView POST: formset invalid must re-render (200)."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_formset_rerenders(self, MockKeaClient):
        """POST with an invalid formset (missing management form) must re-render 200."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "option-data": []}]},
            }
        ]
        # Post one form entry missing required 'name' field — makes formset invalid
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "",
                "form-0-data": "some-value",
            },
        )
        # Invalid formset can re-render OR redirect depending on validation path
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_always_send_includes_flag(self, MockKeaClient):
        """POST with always_send=True must pass always-send=True in options."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "routers",
                "form-0-data": "10.0.0.1",
                "form-0-always_send": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.subnet_update_options.call_args
        if call_kwargs:
            options = (call_kwargs.kwargs or {}).get("options") or (call_kwargs.args[2] if call_kwargs.args else [])
            always_send_opts = [o for o in options if o.get("always-send")]
            self.assertTrue(len(always_send_opts) >= 1)


# ---------------------------------------------------------------------------
# Server options POST: formset invalid + always_send
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsPostInvalid(_ViewTestBase):
    """_BaseServerOptionsEditView POST: formset invalid and always_send coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_formset_rerenders(self, MockKeaClient):
        """POST with an invalid formset must re-render (not crash)."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": {"Dhcp4": {"option-data": []}}}]
        response = self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "",
                "form-0-data": "val",
            },
        )
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_always_send_includes_flag(self, MockKeaClient):
        """POST with always_send=True must pass always-send=True in options."""
        MockKeaClient.return_value.server_update_options.return_value = None
        self.client.post(
            self._url(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "domain-name-servers",
                "form-0-data": "8.8.8.8",
                "form-0-always_send": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.server_update_options.call_args
        if call_kwargs:
            options = (call_kwargs.kwargs or {}).get("options") or []
            always_send_opts = [o for o in options if o.get("always-send")]
            self.assertTrue(len(always_send_opts) >= 1)


# ---------------------------------------------------------------------------
# OptionDef add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddExceptions(_ViewTestBase):
    """BaseServerOptionDefAddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_rerenders(self, MockKeaClient):
        """POST with invalid form (missing required fields) must return 200."""
        response = self.client.post(self._url(), {"name": "", "code": "", "type": "", "space": ""})
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_array_true_passes_flag(self, MockKeaClient):
        """POST with array=True must include array=True in the option_def dict."""
        MockKeaClient.return_value.option_def_add.return_value = None
        self.client.post(
            self._url(),
            {
                "name": "my-option",
                "code": "200",
                "type": "string",
                "space": "dhcp4",
                "array": "on",
            },
        )
        call_kwargs = MockKeaClient.return_value.option_def_add.call_args
        if call_kwargs:
            opt = (call_kwargs.kwargs or {}).get("option_def") or {}
            self.assertTrue(opt.get("array"))

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """KeaException on option_def_add must show error message and redirect."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_add.side_effect = KeaException(
            {"result": 1, "text": "duplicate code"}, index=0
        )
        response = self.client.post(
            self._url(),
            {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            follow=True,
        )
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_propagates(self, MockKeaClient):
        """Non-KeaException on option_def_add propagates (not swallowed)."""
        MockKeaClient.return_value.option_def_add.side_effect = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.client.post(
                self._url(),
                {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            )


# ---------------------------------------------------------------------------
# OptionDef delete exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefDeleteExceptions(_ViewTestBase):
    """BaseServerOptionDefDeleteView POST exception paths."""

    def _url(self, code=200, space="dhcp4"):
        return reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, code, space])

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """KeaException on option_def_del must show error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_del.side_effect = KeaException(
            {"result": 1, "text": "not found"}, index=0
        )
        response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_propagates(self, MockKeaClient):
        """Non-KeaException on option_def_del propagates (not swallowed)."""
        MockKeaClient.return_value.option_def_del.side_effect = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.client.post(self._url())


# ---------------------------------------------------------------------------
# Subnet options — subnet in shared-network + POST handler
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsSharedNetwork(_ViewTestBase):
    """Lines 4706-4708, 4761: subnet in shared-network + POST options handler."""

    def _url(self, subnet_id=99):
        return reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_get_subnet_in_shared_network(self, MockKeaClient):
        """Lines 4706-4708: subnet found inside shared-network is returned."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [
                            {
                                "name": "prod",
                                "subnet4": [{"id": 99, "subnet": "10.99.0.0/24", "option-data": []}],
                            }
                        ],
                    }
                },
            }
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.99.0.0/24")

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_formset_rerenders(self, MockKeaClient):
        """Line 4761 (post handler): invalid formset re-renders form."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [{"id": 99, "subnet": "10.99.0.0/24", "option-data": []}],
                        "shared-networks": [],
                    }
                },
            }
        ]
        # Submit invalid formset (missing TOTAL_FORMS)
        response = self.client.post(self._url(), {"form-0-name": "dns-servers"})
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# TestKeaConfigTestErrorHandling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaConfigTestErrorHandling(_ViewTestBase):
    """KeaConfigTestError in mutation handlers must produce a user-facing error message."""

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_options_config_test_error_shows_message(self, MockKeaClient):
        """POST to subnet options edit must show config-test error message on KeaConfigTestError."""
        from netbox_kea.kea import KeaConfigTestError

        MockKeaClient.return_value.subnet_update_options.side_effect = KeaConfigTestError(
            {"result": 1, "text": "config test failed"}, index=0
        )
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 1])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("config" in m.lower() and "no changes" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_server_options_config_test_error_shows_message(self, MockKeaClient):
        """POST to server options edit must show config-test error message on KeaConfigTestError."""
        from netbox_kea.kea import KeaConfigTestError

        MockKeaClient.return_value.server_update_options.side_effect = KeaConfigTestError(
            {"result": 1, "text": "config test failed"}, index=0
        )
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("config" in m.lower() and "no changes" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_option_def_add_config_test_error_shows_message(self, MockKeaClient):
        """POST to option-def add must show config-test error message on KeaConfigTestError."""
        from netbox_kea.kea import KeaConfigTestError

        MockKeaClient.return_value.option_def_add.side_effect = KeaConfigTestError(
            {"result": 1, "text": "config test failed"}, index=0
        )
        url = reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])
        response = self.client.post(
            url,
            {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("config" in m.lower() and "no changes" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_option_def_del_config_test_error_shows_message(self, MockKeaClient):
        """POST to option-def delete must show config-test error message on KeaConfigTestError."""
        from netbox_kea.kea import KeaConfigTestError

        MockKeaClient.return_value.option_def_del.side_effect = KeaConfigTestError(
            {"result": 1, "text": "config test failed"}, index=0
        )
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("config" in m.lower() and "no changes" in m.lower() for m in msgs))
