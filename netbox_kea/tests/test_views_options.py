# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Options-view tests for the netbox_kea plugin.

Covers the views in ``netbox_kea/views/options.py`` (e.g.
``ServerSubnetOptionsEditView``, ``ServerOptionDefAddView``,
``ServerOptionDef4DeleteView``, etc.) as well as the helper function
``_extract_identifier`` which lives in that module.

All Kea HTTP calls are mocked so no running Kea instance is required.
"""

from unittest.mock import patch

from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse

from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase

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
        self._assert_redirect_to_integer_pk(response)
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
        call_args = MockKeaClient.return_value.subnet_update_options.call_args
        self.assertIsNotNone(call_args)
        self.assertEqual(call_args.kwargs["version"], 4)  # version
        self.assertEqual(call_args.kwargs["subnet_id"], 42)  # subnet_id

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
        # options argument — use explicit keyword or positional lookup
        options_arg = call_kwargs.kwargs.get("options") or (call_kwargs.args[2] if len(call_kwargs.args) > 2 else [])
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
        self._assert_redirect_to_integer_pk(response)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    @patch("netbox_kea.models.KeaClient")
    def test_get_v6_returns_200(self, MockKeaClient):
        """GET for DHCPv6 subnet options returns 200 OK."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp6": {
                        "subnet6": [
                            {
                                "id": 42,
                                "subnet": "2001:db8::/64",
                                "option-data": [{"name": "dns-servers", "data": "2001:4860:4860::8888"}],
                            }
                        ]
                    }
                },
            }
        ]
        response = self.client.get(self._url(version=6, subnet_id=42))
        self.assertEqual(response.status_code, 200)
        self.assertIn("dns-servers", response.content.decode())

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version_and_subnet_id_v6(self, MockKeaClient):
        """POST for DHCPv6 subnet calls subnet_update_options with version=6 and subnet_id=42."""
        MockKeaClient.return_value.subnet_update_options.return_value = None
        self.client.post(
            self._url(version=6, subnet_id=42),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "dns-servers",
                "form-0-data": "2001:4860:4860::8888",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        call_args = MockKeaClient.return_value.subnet_update_options.call_args
        self.assertIsNotNone(call_args)
        self.assertEqual(call_args.kwargs["version"], 6)
        self.assertEqual(call_args.kwargs["subnet_id"], 42)


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
        self._assert_redirect_to_integer_pk(response)
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
        call_args = MockKeaClient.return_value.server_update_options.call_args
        self.assertIsNotNone(call_args)
        self.assertEqual(call_args.kwargs["version"], 4)  # version

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
        self.assertIsNotNone(call_kwargs, "server_update_options was not called")
        options_arg = (call_kwargs.kwargs or {}).get("options") or (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else []
        )
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
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    @patch("netbox_kea.models.KeaClient")
    def test_get_v6_returns_200(self, MockKeaClient):
        """GET for DHCPv6 server options returns 200 OK."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp6": {
                        "option-data": [{"name": "dns-servers", "data": "2001:4860:4860::8888"}],
                        "subnet6": [],
                    }
                },
            }
        ]
        response = self.client.get(self._url(version=6))
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_version_6(self, MockKeaClient):
        """POST for DHCPv6 server options calls server_update_options with version=6."""
        MockKeaClient.return_value.server_update_options.return_value = None
        self.client.post(
            self._url(version=6),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-name": "dns-servers",
                "form-0-data": "2001:4860:4860::8888",
                "form-0-always_send": "",
                "form-0-DELETE": "",
            },
        )
        call_args = MockKeaClient.return_value.server_update_options.call_args
        self.assertIsNotNone(call_args)
        self.assertEqual(call_args.kwargs["version"], 6)


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
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))
        MockKeaClient.return_value.option_def_add.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_returns_200(self, MockKeaClient):
        """POST with missing required fields returns 200 (form re-render)."""
        response = self.client.post(self._url(), {"name": "", "code": "", "type": "", "space": ""})
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.option_def_add.assert_not_called()

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
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))
        MockKeaClient.return_value.option_def_del.assert_called_once()

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
                "arguments": {"Dhcp4": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "option-data": []}]}},
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
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.subnet_update_options.assert_not_called()

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
        self.assertIsNotNone(call_kwargs, "subnet_update_options was not called")
        options = (call_kwargs.kwargs or {}).get("options") or (
            call_kwargs.args[2] if len(call_kwargs.args) > 2 else []
        )
        always_send_opts = [o for o in options if o.get("always-send")]
        self.assertGreaterEqual(len(always_send_opts), 1)


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
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.server_update_options.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_always_send_includes_flag(self, MockKeaClient):
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
        self.assertIsNotNone(call_kwargs, "server_update_options was not called")
        options = (call_kwargs.kwargs or {}).get("options") or (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else []
        )
        always_send_opts = [o for o in options if o.get("always-send")]
        self.assertGreaterEqual(len(always_send_opts), 1)


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
        MockKeaClient.return_value.option_def_add.assert_not_called()

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
        self.assertIsNotNone(call_kwargs, "option_def_add was not called")
        opt = (call_kwargs.kwargs or {}).get("option_def") or (call_kwargs.args[1] if len(call_kwargs.args) > 1 else {})
        self.assertIs(opt.get("array"), True)

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
        """Line 4761 (post handler): invalid formset re-renders form (subnet inside shared-networks)."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [
                            {"name": "net1", "subnet4": [{"id": 99, "subnet": "10.99.0.0/24", "option-data": []}]}
                        ],
                    }
                },
            }
        ]
        # Submit invalid formset (missing TOTAL_FORMS)
        response = self.client.post(self._url(), {"form-0-name": "dns-servers"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.99.0.0/24")
        MockKeaClient.return_value.subnet_update_options.assert_not_called()


# ---------------------------------------------------------------------------
# TestKeaConfigTestErrorHandling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaConfigTestErrorHandling(_ViewTestBase):
    """KeaConfigTestError in mutation handlers must produce a user-facing error message."""

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_options_config_test_error_shows_message(self, MockKeaClient):
        """POST to subnet options edit must show config-test error message on KeaConfigTestError."""
        from netbox_kea.kea import KeaConfigTestError, KeaException

        cause = KeaException({"result": 1, "text": "config test failed"})
        MockKeaClient.return_value.subnet_update_options.side_effect = KeaConfigTestError("dhcp4", cause)
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 1])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("config" in m.lower() and "no changes" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_server_options_config_test_error_shows_message(self, MockKeaClient):
        """POST to server options edit must show config-test error message on KeaConfigTestError."""
        from netbox_kea.kea import KeaConfigTestError, KeaException

        cause = KeaException({"result": 1, "text": "config test failed"})
        MockKeaClient.return_value.server_update_options.side_effect = KeaConfigTestError("dhcp4", cause)
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("config" in m.lower() and "no changes" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_option_def_add_config_test_error_shows_message(self, MockKeaClient):
        """POST to option-def add must show config-test error message on KeaConfigTestError."""
        from netbox_kea.kea import KeaConfigTestError, KeaException

        cause = KeaException({"result": 1, "text": "config test failed"})
        MockKeaClient.return_value.option_def_add.side_effect = KeaConfigTestError("dhcp4", cause)
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
        from netbox_kea.kea import KeaConfigTestError, KeaException

        cause = KeaException({"result": 1, "text": "config test failed"})
        MockKeaClient.return_value.option_def_del.side_effect = KeaConfigTestError("dhcp4", cause)
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("config" in m.lower() and "no changes" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Subnet options POST: PartialPersistError, TransportError, ValueError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsPartialPersistError(_ViewTestBase):
    """POST to subnet options edit raises PartialPersistError → warning message."""

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_shows_warning(self, MockKeaClient):
        """PartialPersistError on subnet_update_options must show a warning about config-write."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.subnet_update_options.side_effect = PartialPersistError(
            "dhcp4", Exception("write failed"), subnet_id=1
        )
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 1])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsTransportError(_ViewTestBase):
    """POST to subnet options edit raises ConnectionError → transport error message."""

    @patch("netbox_kea.models.KeaClient")
    def test_connection_error_shows_transport_message(self, MockKeaClient):
        """requests.ConnectionError on subnet_update_options must show 'Transport error' message."""
        import requests

        MockKeaClient.return_value.subnet_update_options.side_effect = requests.ConnectionError("down")
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 1])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsValueError(_ViewTestBase):
    """POST to subnet options edit raises ValueError → invalid config message."""

    @patch("netbox_kea.models.KeaClient")
    def test_value_error_shows_invalid_config_message(self, MockKeaClient):
        """ValueError on subnet_update_options must show 'Invalid Kea client configuration' message."""
        MockKeaClient.return_value.subnet_update_options.side_effect = ValueError("bad config")
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 1])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid kea client configuration" in m.lower() for m in msgs))
        self.assertFalse(any("bad config" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Server options POST: PartialPersistError, TransportError, ValueError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsPartialPersistError(_ViewTestBase):
    """POST to server options edit raises PartialPersistError → warning message."""

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_shows_warning(self, MockKeaClient):
        """PartialPersistError on server_update_options must show a warning about config-write."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.server_update_options.side_effect = PartialPersistError(
            "dhcp4", Exception("write failed"), subnet_id=None
        )
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsTransportError(_ViewTestBase):
    """POST to server options edit raises ConnectionError → transport error message."""

    @patch("netbox_kea.models.KeaClient")
    def test_connection_error_shows_transport_message(self, MockKeaClient):
        """requests.ConnectionError on server_update_options must show 'Transport error' message."""
        import requests

        MockKeaClient.return_value.server_update_options.side_effect = requests.ConnectionError("down")
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsValueError(_ViewTestBase):
    """POST to server options edit raises ValueError → invalid config message."""

    @patch("netbox_kea.models.KeaClient")
    def test_value_error_shows_invalid_config_message(self, MockKeaClient):
        """ValueError on server_update_options must show 'Invalid Kea client configuration' message."""
        MockKeaClient.return_value.server_update_options.side_effect = ValueError("bad config")
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid kea client configuration" in m.lower() for m in msgs))
        self.assertFalse(any("bad config" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Option-def add POST: PartialPersistError, TransportError, ValueError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddPartialPersistError(_ViewTestBase):
    """POST to option-def add raises PartialPersistError → warning message."""

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_shows_warning(self, MockKeaClient):
        """PartialPersistError on option_def_add must show a warning about config-write."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.option_def_add.side_effect = PartialPersistError(
            "dhcp4", Exception("write failed"), subnet_id=None
        )
        url = reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])
        response = self.client.post(
            url,
            {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddTransportError(_ViewTestBase):
    """POST to option-def add raises ConnectionError → transport error message."""

    @patch("netbox_kea.models.KeaClient")
    def test_connection_error_shows_transport_message(self, MockKeaClient):
        """requests.ConnectionError on option_def_add must show 'Transport error' message."""
        import requests

        MockKeaClient.return_value.option_def_add.side_effect = requests.ConnectionError("down")
        url = reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])
        response = self.client.post(
            url,
            {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddValueError(_ViewTestBase):
    """POST to option-def add raises ValueError → invalid config message."""

    @patch("netbox_kea.models.KeaClient")
    def test_value_error_shows_invalid_config_message(self, MockKeaClient):
        """ValueError on option_def_add must show 'Invalid Kea client configuration' message."""
        MockKeaClient.return_value.option_def_add.side_effect = ValueError("bad config")
        url = reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])
        response = self.client.post(
            url,
            {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid kea client configuration" in m.lower() for m in msgs))
        self.assertFalse(any("bad config" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Option-def delete POST: PartialPersistError, TransportError, ValueError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefDeletePartialPersistError(_ViewTestBase):
    """POST to option-def delete raises PartialPersistError → warning message."""

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_error_shows_warning(self, MockKeaClient):
        """PartialPersistError on option_def_del must show a warning about config-write."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.option_def_del.side_effect = PartialPersistError(
            "dhcp4", Exception("write failed"), subnet_id=None
        )
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefDeleteTransportError(_ViewTestBase):
    """POST to option-def delete raises ConnectionError → transport error message."""

    @patch("netbox_kea.models.KeaClient")
    def test_connection_error_shows_transport_message(self, MockKeaClient):
        """requests.ConnectionError on option_def_del must show 'Transport error' message."""
        import requests

        MockKeaClient.return_value.option_def_del.side_effect = requests.ConnectionError("down")
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefDeleteValueError(_ViewTestBase):
    """POST to option-def delete raises ValueError → invalid config message."""

    @patch("netbox_kea.models.KeaClient")
    def test_value_error_shows_invalid_config_message(self, MockKeaClient):
        """ValueError on option_def_del must show 'Invalid Kea client configuration' message."""
        MockKeaClient.return_value.option_def_del.side_effect = ValueError("bad config")
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid kea client configuration" in m.lower() for m in msgs))
        self.assertFalse(any("bad config" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# GET client errors: ValueError on get_client
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsGetClientError(_ViewTestBase):
    """GET to subnet options edit when get_client raises ValueError → redirect with error."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_client_value_error_redirects(self, MockKeaClient):
        """ValueError from get_client on GET must redirect with error message."""
        MockKeaClient.side_effect = ValueError("bad TLS config")
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 1])
        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("internal error" in m.lower() for m in msgs))
        self.assertFalse(any("bad tls config" in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsGetClientError(_ViewTestBase):
    """GET to server options edit when get_client raises ValueError → redirect with error."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_client_value_error_redirects(self, MockKeaClient):
        """ValueError from get_client on GET must redirect with error message."""
        MockKeaClient.side_effect = ValueError("bad TLS config")
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("internal error" in m.lower() for m in msgs))
        self.assertFalse(any("bad tls config" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Option-def list fetch error
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefListFetchError(_ViewTestBase):
    """GET to option-def list when option_def_list raises KeaException → 200 with empty list."""

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_returns_200_with_error_flag(self, MockKeaClient):
        """KeaException on option_def_list must return 200 with options_load_error=True."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.option_def_list.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        url = reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("options_load_error"))


# ---------------------------------------------------------------------------
# Combined status badge error
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedStatusBadgeError(_ViewTestBase):
    """GET to status badge when version-get raises → offline status."""

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_returns_200_with_offline(self, MockKeaClient):
        """KeaException on version-get must return 200 with offline status badges."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException({"result": 1, "text": "error"}, index=0)
        url = reverse("plugins:netbox_kea:combined_server_status_badge", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("offline", content.lower())
