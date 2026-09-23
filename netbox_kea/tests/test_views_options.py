# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Options-view tests for the netbox_kea plugin.

Covers the views in ``netbox_kea/views/options.py`` (e.g.
``ServerSubnetOptionsEditView``, ``ServerOptionDefAddView``,
``ServerOptionDef4DeleteView``, etc.).

These tests drive the **real** ``KeaClient``; only the HTTP boundary is stubbed
via ``kea_stub.stub_kea``, so the request payloads the views actually send to Kea
are exercised. Free Kea has no option-set hook, so every option-data / option-def
mutation is a read-modify-write cycle:

    config-get  →  config-test  →  config-set  →  config-write

(``config-write`` runs because ``persist_config`` defaults to True.) The old
mocked tests asserted on ``subnet_update_options.call_args``; these assert on the
**config-set body** the real read-modify-write pushes back
(``kea.bodies("config-set")[0]["arguments"]``), which proves the version, subnet,
and option payload end to end.

Error paths are driven through the real client too:
- ``KeaConfigTestError`` ← ``config-test`` returns result 1 (non-2 error);
- ``PartialPersistError`` ← ``config-write`` returns result 1 on a persisting op;
- generic ``KeaException`` ← ``config-get`` returns result 1;
- transport / ``ValueError`` / ``RuntimeError`` ← the failing command is registered
  as that exception instance (the stub raises it at the boundary);
- get-client ``ValueError`` ← a server built with ``client_cert_path`` but no key
  (``KeaClient.__init__`` rejects cert-without-key).
"""

import copy

import requests
from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse

from .kea_stub import _catalogue_responses_for_subnets, queued, stub_kea
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase

# ---------------------------------------------------------------------------
# config-get fixtures (read by the read-modify-write mutations and GET prefill)
# ---------------------------------------------------------------------------

_SUBNET4 = {
    "id": 42,
    "subnet": "10.0.0.0/24",
    "option-data": [
        {"name": "domain-name-servers", "data": "8.8.8.8"},
        {"name": "routers", "data": "10.0.0.1"},
    ],
}
_SUBNET6 = {
    "id": 42,
    "subnet": "2001:db8::/64",
    "option-data": [{"name": "dns-servers", "data": "2001:4860:4860::8888"}],
}
_OPTIONS_CONFIG_GET = [_catalogue_responses_for_subnets(4, [_SUBNET4])["config-get"]]
_OPTIONS_CONFIG_GET_V6 = [_catalogue_responses_for_subnets(6, [_SUBNET6])["config-get"]]
_EMPTY_OPTIONS_CONFIG_GET = [_catalogue_responses_for_subnets(4, [{**_SUBNET4, "option-data": []}])["config-get"]]
_EMPTY_OPTIONS_CONFIG_GET_V6 = [_catalogue_responses_for_subnets(6, [{**_SUBNET6, "option-data": []}])["config-get"]]
_EMPTY_SERVER_OPTIONS_CONFIG_GET = [_catalogue_responses_for_subnets(4, [])["config-get"]]
_EMPTY_SERVER_OPTIONS_CONFIG_GET_V6 = [_catalogue_responses_for_subnets(6, [])["config-get"]]

_SUBNET4_REDIRECT_IDENTITY = {
    "subnet4-list": {"result": 0, "arguments": {"subnets": [{"id": 42, "subnet": "10.0.0.0/24"}]}},
}

_SERVER_OPTIONS_CONFIG_GET = [
    _catalogue_responses_for_subnets(
        4,
        [],
        global_options=(
            {"name": "domain-name-servers", "data": "8.8.8.8"},
            {"name": "routers", "data": "10.0.0.1"},
        ),
    )["config-get"]
]

_SERVER_OPTIONS_CONFIG_GET_V6 = [
    _catalogue_responses_for_subnets(
        6,
        [],
        global_options=({"name": "dns-servers", "data": "2001:4860:4860::8888"},),
    )["config-get"]
]

_OPTION_DEF_LIST_V4 = [
    {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4"},
    {"name": "other-opt", "code": 201, "type": "uint32", "space": "dhcp4"},
]

_OPTION_DEF_LIST_EMPTY: list = []


# ---------------------------------------------------------------------------
# Stub builders (real KeaClient + HTTP-boundary stub)
# ---------------------------------------------------------------------------

_CONFIG_OK = {"result": 0}


def _option_def_config(defs, version=4):
    """Return one Server Configuration response with Option Definitions."""
    return [_catalogue_responses_for_subnets(version, [], option_definitions=tuple(defs))["config-get"]]


def _persist_stub(config_get, **overrides):
    """Stub the read-modify-write chain: config-get → config-test → config-set → config-write.

    *config_get* is deep-copied so the read-modify-write mutation (which edits the
    config in place) never corrupts the shared module-level fixture. Assert on the
    resulting config-set body via ``kea.bodies("config-set")[0]["arguments"]``.

    ``stat-lease{4,6}-get`` are pre-registered (harmless when unused) so tests that
    POST with ``follow=True`` and land on the subnets list — which enriches subnets
    with utilisation stats — render without tripping the strict stub.
    """
    base = {
        "config-get": copy.deepcopy(config_get),
        "config-test": _CONFIG_OK,
        "config-set": _CONFIG_OK,
        "config-write": _CONFIG_OK,
        "stat-lease4-get": {"result": 0, "arguments": {}},
        "stat-lease6-get": {"result": 0, "arguments": {}},
    }
    base.update(overrides)
    return stub_kea(base)


def _written_config(kea):
    """Return the config dict the real read-modify-write pushed to Kea via config-set."""
    bodies = kea.bodies("config-set")
    assert bodies, "config-set was never issued (read-modify-write did not complete)"
    return bodies[0]["arguments"]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsView(_ViewTestBase):
    """Tests for ServerSubnet4/6OptionsEditView (GET prefill + POST update)."""

    def _url(self, version=4, subnet_id=42):
        return reverse(
            f"plugins:netbox_kea:server_subnet{version}_options_edit",
            args=[self.server.pk, subnet_id],
        )

    def _post_data(self, name="routers", data="10.0.0.1", always_send="", delete=""):
        return {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-name": name,
            "form-0-data": data,
            "form-0-always_send": always_send,
            "form-0-DELETE": delete,
        }

    def test_url_registered_v4(self):
        """URL server_subnet4_options_edit is registered."""
        url = self._url(version=4)
        self.assertIn("options", url)

    def test_url_registered_v6(self):
        """URL server_subnet6_options_edit is registered."""
        url = self._url(version=6)
        self.assertIn("options", url)

    def test_get_returns_200(self):
        """GET returns 200 OK."""
        with stub_kea(_catalogue_responses_for_subnets(4, [_SUBNET4])):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_prefills_existing_options(self):
        """GET pre-populates formset with existing option-data from config-get."""
        with stub_kea(_catalogue_responses_for_subnets(4, [_SUBNET4])):
            response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("domain-name-servers", content)
        self.assertIn("8.8.8.8", content)

    def test_get_uses_typed_subnet_options_and_keeps_code_only_name_empty(self):
        subnet = {
            "id": 42,
            "subnet": "10.0.0.0/24",
            "option-data": [{"code": 222, "data": "opaque", "always-send": True}],
        }
        with stub_kea(_catalogue_responses_for_subnets(4, [subnet])):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["formset"].initial,
            [{"name": "", "data": "opaque", "always_send": True, "original_option": {"code": 222}}],
        )

    def test_get_renders_without_the_subnet_cmds_hook(self):
        """The editor reads the configuration snapshot, so a missing hook does not block it."""
        responses = _catalogue_responses_for_subnets(4, [_SUBNET4])
        responses["subnet4-get"] = {"result": 2, "text": "'subnet4-get' command not supported."}
        with stub_kea(responses) as kea:
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertIn("8.8.8.8", response.content.decode())
        self.assertNotIn("subnet4-get", kea.commands())

    def test_get_refuses_an_incomplete_subnet_instead_of_offering_a_filtered_list(self):
        """Saving a filtered list would delete the entry the parser omitted."""
        subnet = {
            "id": 42,
            "subnet": "10.0.0.0/24",
            "option-data": [{"name": "routers", "data": "10.0.0.1"}, {"data": "no identity"}],
        }
        with stub_kea(_catalogue_responses_for_subnets(4, [subnet])):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 302)
        texts = [str(message) for message in django_messages.get_messages(response.wsgi_request)]
        self.assertIn("Could not load subnet configuration from Kea. The form cannot be displayed.", texts)

    def test_get_reads_the_live_configuration_even_when_the_display_cache_is_warm(self):
        with stub_kea(_catalogue_responses_for_subnets(4, [_SUBNET4])) as kea:
            self.client.get(reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk]))
            self.client.get(self._url())
            self.client.get(self._url())

        self.assertEqual(kea.commands().count("config-get"), 3)

    def test_get_unavailable_configuration_shows_diagnostic_and_redirects(self):
        with stub_kea({"config-get": requests.ConnectionError("unreachable")}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 302)
        texts = [str(message) for message in django_messages.get_messages(response.wsgi_request)]
        self.assertIn("Kea Subnet configuration facts are unavailable.", texts)

    def test_post_calls_subnet_update_options(self):
        """POST with valid formset runs the read-modify-write and redirects."""
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET) as kea:
            response = self.client.post(self._url(), self._post_data())
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)
        self.assertIn("config-set", kea.commands())

    def test_post_passes_correct_version_and_subnet_id(self):
        """POST rewrites subnet 42's option-data in the DHCPv4 config (version + subnet_id)."""
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET) as kea:
            self.client.post(self._url(version=4, subnet_id=42), self._post_data())
        subnet = _written_config(kea)["Dhcp4"]["subnet4"][0]
        self.assertEqual(subnet["id"], 42)
        self.assertEqual([o["name"] for o in subnet["option-data"]], ["routers"])

    def test_post_deleted_rows_excluded_from_options(self):
        """Rows with DELETE=on are excluded from the option-data written back."""
        data = {
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
        }
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET) as kea:
            self.client.post(self._url(), data)
        opts = _written_config(kea)["Dhcp4"]["subnet4"][0]["option-data"]
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0]["name"], "routers")

    def test_post_kea_exception_shows_error_message(self):
        """A KeaException from the mutation shows an error message and redirects.

        With no subnet 42 in the returned config, the real ``subnet_update_options``
        raises ``KeaException`` ("subnet not found") before any config-test.
        """
        empty = _catalogue_responses_for_subnets(4, [])["config-get"]
        with stub_kea({"config-get": empty}):
            response = self.client.post(self._url(), self._post_data())
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_get_v6_returns_200(self):
        """GET for DHCPv6 subnet options returns 200 OK."""
        with stub_kea(_catalogue_responses_for_subnets(6, [_SUBNET6])):
            response = self.client.get(self._url(version=6, subnet_id=42))
        self.assertEqual(response.status_code, 200)
        self.assertIn("dns-servers", response.content.decode())

    def test_post_passes_correct_version_and_subnet_id_v6(self):
        """POST for a DHCPv6 subnet rewrites subnet 42's option-data in the DHCPv6 config."""
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET_V6) as kea:
            self.client.post(
                self._url(version=6, subnet_id=42),
                self._post_data(name="dns-servers", data="2001:4860:4860::8888"),
            )
        subnet = _written_config(kea)["Dhcp6"]["subnet6"][0]
        self.assertEqual(subnet["id"], 42)
        self.assertEqual([o["name"] for o in subnet["option-data"]], ["dns-servers"])


# ---------------------------------------------------------------------------
# TestServerOptionsView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsView(_ViewTestBase):
    """Tests for ServerDHCP4/6OptionsEditView (GET prefill + POST update)."""

    def _url(self, version=4):
        return reverse(
            f"plugins:netbox_kea:server_dhcp{version}_options_edit",
            args=[self.server.pk],
        )

    def _post_data(self, name="routers", data="10.0.0.1", always_send="", delete=""):
        return {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-name": name,
            "form-0-data": data,
            "form-0-always_send": always_send,
            "form-0-DELETE": delete,
        }

    def test_url_registered_v4(self):
        """URL server_dhcp4_options_edit is registered."""
        url = self._url(version=4)
        self.assertIn("options", url)

    def test_url_registered_v6(self):
        """URL server_dhcp6_options_edit is registered."""
        url = self._url(version=6)
        self.assertIn("options", url)

    def test_get_returns_200(self):
        """GET returns 200 OK."""
        with stub_kea({"config-get": _SERVER_OPTIONS_CONFIG_GET}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_prefills_existing_options(self):
        """GET pre-populates formset with existing server-level option-data."""
        with stub_kea({"config-get": _SERVER_OPTIONS_CONFIG_GET}):
            response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("domain-name-servers", content)
        self.assertIn("8.8.8.8", content)

    def test_get_unavailable_snapshot_shows_diagnostic_and_redirects(self):
        with stub_kea({"config-get": {"result": 1, "text": "read failed"}}):
            response = self.client.get(self._url(), follow=True)

        self.assertEqual(response.status_code, 200)
        message_text = [str(message) for message in response.context["messages"]]
        self.assertIn("Could not load server options from Kea. The form cannot be displayed.", message_text)
        self.assertTrue(any("configuration facts are unavailable" in message for message in message_text))

    def test_get_refuses_incomplete_options_instead_of_offering_a_filtered_list(self):
        """Saving a filtered list would delete the entry the parser omitted."""
        responses = _catalogue_responses_for_subnets(
            4,
            [],
            global_options=(
                {"name": "domain-name-servers", "data": "198.18.0.53"},
                {"data": "invalid without identity"},
            ),
        )
        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 302)
        texts = [str(message) for message in django_messages.get_messages(response.wsgi_request)]
        self.assertIn("Could not load server options from Kea. The form cannot be displayed.", texts)
        self.assertTrue(any("invalid Subnet option" in text for text in texts))

    def test_get_reads_the_live_configuration_even_when_the_display_cache_is_warm(self):
        with stub_kea(_catalogue_responses_for_subnets(4, [])) as kea:
            self.client.get(reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk]))
            self.client.get(self._url())
            self.client.get(self._url())

        self.assertEqual(kea.commands().count("config-get"), 3)

    def test_get_non_object_family_configuration_redirects_without_500(self):
        with stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp4": []}}}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 302)

    def test_post_calls_server_update_options(self):
        """POST with valid formset runs the read-modify-write and redirects."""
        with _persist_stub(_EMPTY_SERVER_OPTIONS_CONFIG_GET) as kea:
            response = self.client.post(self._url(), self._post_data())
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)
        self.assertIn("config-set", kea.commands())

    def test_post_passes_correct_version(self):
        """POST rewrites the DHCPv4 server-level option-data."""
        with _persist_stub(_EMPTY_SERVER_OPTIONS_CONFIG_GET) as kea:
            self.client.post(self._url(version=4), self._post_data())
        opts = _written_config(kea)["Dhcp4"]["option-data"]
        self.assertEqual([o["name"] for o in opts], ["routers"])

    def test_post_deleted_rows_excluded(self):
        """Rows with DELETE=on are excluded from the option-data written back."""
        data = {
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
        }
        with _persist_stub(_EMPTY_SERVER_OPTIONS_CONFIG_GET) as kea:
            self.client.post(self._url(), data)
        opts = _written_config(kea)["Dhcp4"]["option-data"]
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0]["name"], "routers")

    def test_post_kea_exception_redirects(self):
        """A KeaException from the mutation shows an error message and redirects.

        A config-get result 1 makes the real ``server_update_options`` raise
        ``KeaException`` before any write.
        """
        with stub_kea({"config-get": {"result": 1, "text": "internal error"}}):
            response = self.client.post(self._url(), self._post_data())
        self.assertEqual(response.status_code, 302)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_get_v6_returns_200(self):
        """GET for DHCPv6 server options returns 200 OK."""
        with stub_kea({"config-get": _SERVER_OPTIONS_CONFIG_GET_V6}):
            response = self.client.get(self._url(version=6))
        self.assertEqual(response.status_code, 200)

    def test_post_passes_version_6(self):
        """POST for DHCPv6 server options rewrites the DHCPv6 server-level option-data."""
        with _persist_stub(_EMPTY_SERVER_OPTIONS_CONFIG_GET_V6) as kea:
            self.client.post(self._url(version=6), self._post_data(name="dns-servers", data="2001:4860:4860::8888"))
        opts = _written_config(kea)["Dhcp6"]["option-data"]
        self.assertEqual([o["name"] for o in opts], ["dns-servers"])


# ---------------------------------------------------------------------------
# ServerOptionDef4ListView / ServerOptionDef6ListView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4ListView(_ViewTestBase):
    """Tests for ServerOptionDef4ListView: GET list of custom option definitions."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk])

    def test_get_returns_200(self):
        """GET returns 200 OK."""
        with stub_kea({"config-get": _option_def_config(_OPTION_DEF_LIST_V4)}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_shows_option_def_name(self):
        """GET renders option names in the response."""
        with stub_kea({"config-get": _option_def_config(_OPTION_DEF_LIST_V4)}):
            response = self.client.get(self._url())
        self.assertContains(response, "my-opt")

    def test_shows_option_def_code(self):
        """GET renders option codes in the response."""
        with stub_kea({"config-get": _option_def_config(_OPTION_DEF_LIST_V4)}):
            response = self.client.get(self._url())
        self.assertContains(response, "200")

    def test_incomplete_snapshot_warns_and_renders_typed_valid_definitions(self):
        responses = _catalogue_responses_for_subnets(
            4,
            [],
            option_definitions=(
                {
                    "name": "site-record",
                    "code": 222,
                    "type": "record",
                    "space": "dhcp4",
                    "array": True,
                    "encapsulate": "site-space",
                    "record-types": "uint16, string",
                },
                {"name": "invalid", "code": "not-an-integer", "type": "string", "space": "dhcp4"},
            ),
        )
        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["options_load_error"])
        definition = response.context["option_defs"][0]
        self.assertEqual(
            {key: definition[key] for key in ("code", "name", "space", "type", "array", "encapsulate", "record_types")},
            {
                "code": 222,
                "name": "site-record",
                "space": "dhcp4",
                "type": "record",
                "array": True,
                "encapsulate": "site-space",
                "record_types": ("uint16", "string"),
            },
        )
        self.assertEqual(len(response.context["option_defs"]), 1)
        warnings = [
            str(message) for message in response.context["messages"] if message.level == django_messages.WARNING
        ]
        self.assertTrue(any("invalid Option Definition" in message for message in warnings))

    def test_non_object_family_configuration_sets_load_error_without_500(self):
        with stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp4": []}}}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["options_load_error"])

    def test_empty_list_shows_200(self):
        """GET with empty option-def list returns 200 without errors."""
        with stub_kea({"config-get": _option_def_config(_OPTION_DEF_LIST_EMPTY)}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_with_dhcp4_disabled_redirects(self):
        """Server with dhcp4=False redirects away from option_def4 tab (before any Kea call)."""
        v6_only = _make_db_server(name="v6only-od", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_option_def4", args=[v6_only.pk])
        with stub_kea({}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_get_sets_tab_in_context(self):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views.options import ServerOptionDef4View

        with stub_kea({"config-get": _option_def_config(_OPTION_DEF_LIST_V4)}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerOptionDef4View.tab)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef6ListView(_ViewTestBase):
    """Tests for ServerOptionDef6ListView (v6 variant)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def6", args=[self.server.pk])

    def test_get_returns_200(self):
        """GET returns 200 OK."""
        with stub_kea({"config-get": _option_def_config([], version=6)}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_reads_family_6_server_configuration(self):
        """GET reads Server Configuration from the DHCPv6 service."""
        with stub_kea({"config-get": _option_def_config([], version=6)}) as kea:
            self.client.get(self._url())
        self.assertEqual(kea.bodies("config-get")[0].get("service"), ["dhcp6"])

    def test_get_sets_tab_in_context(self):
        """F2: option definitions render under the shared 'Config' tab."""
        from netbox_kea.views.options import _CONFIG_TAB

        with stub_kea({"config-get": _option_def_config([], version=6)}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], _CONFIG_TAB)


# ---------------------------------------------------------------------------
# ServerOptionDef4AddView / ServerOptionDef6AddView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4AddView(_ViewTestBase):
    """Tests for ServerOptionDef4AddView: GET form + POST create."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])

    def test_get_returns_200_with_form(self):
        """GET renders the add option-def form."""
        with stub_kea({}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_valid_calls_option_def_add(self):
        """POST with valid data appends the option-def and redirects."""
        with _persist_stub(_option_def_config([])) as kea:
            response = self.client.post(
                self._url(),
                {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
            )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        defs = _written_config(kea)["Dhcp4"]["option-def"]
        added = next(d for d in defs if d.get("code") == 200)
        self.assertEqual(added["name"], "my-opt")
        self.assertEqual(added["type"], "string")
        self.assertEqual(added["space"], "dhcp4")

    def test_post_passes_correct_version(self):
        """POST writes the new option-def into the DHCPv4 config."""
        with _persist_stub(_option_def_config([])) as kea:
            self.client.post(
                self._url(),
                {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
            )
        written = _written_config(kea)
        self.assertIn("Dhcp4", written)
        added = next(d for d in written["Dhcp4"]["option-def"] if d.get("code") == 200)
        self.assertEqual(added["name"], "my-opt")
        self.assertEqual(added["type"], "string")
        self.assertEqual(added["space"], "dhcp4")

    def test_post_kea_exception_shows_error(self):
        """A KeaException from the mutation shows an error (no 500)."""
        with stub_kea({"config-get": {"result": 1, "text": "duplicate code"}}) as kea:
            response = self.client.post(
                self._url(),
                {"name": "my-opt", "code": 200, "type": "string", "space": "dhcp4", "array": False},
            )
        self.assertIn(response.status_code, (200, 302))
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))
        self.assertIn("config-get", kea.commands())

    def test_post_invalid_form_returns_200(self):
        """POST with missing required fields returns 200 (form re-render), no Kea call."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"name": "", "code": "", "type": "", "space": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

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

    def test_get_returns_200(self):
        """GET renders the add form for v6."""
        with stub_kea({}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_calls_option_def_add_with_version_6(self):
        """POST writes the new option-def into the DHCPv6 config."""
        with _persist_stub(_option_def_config([], version=6)) as kea:
            self.client.post(
                self._url(),
                {"name": "v6-opt", "code": 250, "type": "ipv6-address", "space": "dhcp6", "array": False},
            )
        written = _written_config(kea)
        self.assertIn("Dhcp6", written)
        added = next(d for d in written["Dhcp6"]["option-def"] if d.get("code") == 250)
        self.assertEqual(added["name"], "v6-opt")
        self.assertEqual(added["type"], "ipv6-address")
        self.assertEqual(added["space"], "dhcp6")


# ---------------------------------------------------------------------------
# ServerOptionDef4DeleteView / ServerOptionDef6DeleteView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionDef4DeleteView(_ViewTestBase):
    """Tests for ServerOptionDef4DeleteView: GET confirm + POST delete."""

    def _url(self, code=200, space="dhcp4"):
        return reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, code, space])

    def test_get_returns_200_with_confirmation(self):
        """GET renders a confirmation page mentioning code and space (no Kea call)."""
        with stub_kea({}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "200")

    def test_post_calls_option_def_del_and_redirects(self):
        """POST removes the option-def and redirects to option_def4 list."""
        with _persist_stub(_option_def_config(_OPTION_DEF_LIST_V4)) as kea:
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn("config-set", kea.commands())

    def test_post_passes_correct_version_code_space(self):
        """POST removes exactly the (code=200, space=dhcp4) entry from the DHCPv4 config."""
        with _persist_stub(_option_def_config(_OPTION_DEF_LIST_V4)) as kea:
            self.client.post(self._url(code=200, space="dhcp4"))
        defs = _written_config(kea)["Dhcp4"]["option-def"]
        codes = [d.get("code") for d in defs]
        self.assertNotIn(200, codes)
        self.assertIn(201, codes)  # the other def is untouched

    def test_post_kea_exception_redirects_with_error(self):
        """A KeaException from the mutation must not 500 (shows error, redirects).

        A config-get result 1 makes the real ``option_def_del`` raise ``KeaException``.
        """
        with stub_kea({"config-get": {"result": 1, "text": "not found"}}) as kea:
            response = self.client.post(self._url())
        self.assertIn(response.status_code, (200, 302))
        self._assert_no_none_pk_redirect(response)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))
        self.assertIn("config-get", kea.commands())

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

    def test_get_returns_200(self):
        """GET renders the v6 confirmation page (no Kea call)."""
        with stub_kea({}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_calls_option_def_del_with_version_6(self):
        """POST removes the (code=250, space=dhcp6) entry from the DHCPv6 config."""
        defs = [{"name": "v6-opt", "code": 250, "type": "ipv6-address", "space": "dhcp6"}]
        with _persist_stub(_option_def_config(defs, version=6)) as kea:
            self.client.post(self._url(code=250, space="dhcp6"))
        written = _written_config(kea)
        self.assertIn("Dhcp6", written)
        self.assertNotIn(250, [d.get("code") for d in written["Dhcp6"]["option-def"]])


# ---------------------------------------------------------------------------
# Subnet options POST: formset invalid
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsPostInvalid(_ViewTestBase):
    """_BaseSubnetOptionsEditView POST: formset invalid must re-render (200)."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, subnet_id])

    def test_post_invalid_formset_rerenders(self):
        """POST with an invalid formset must re-render 200 without mutating."""
        subnet = {"id": 42, "subnet": "10.0.0.0/24", "option-data": []}
        with stub_kea(_catalogue_responses_for_subnets(4, [subnet])) as kea:
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
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("config-set", kea.commands())

    def test_post_with_always_send_includes_flag(self):
        """POST with always_send=True writes always-send=True into the option-data."""
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET) as kea:
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
        opts = _written_config(kea)["Dhcp4"]["subnet4"][0]["option-data"]
        self.assertGreaterEqual(len([o for o in opts if o.get("always-send")]), 1)


# ---------------------------------------------------------------------------
# Server options POST: formset invalid + always_send
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsPostInvalid(_ViewTestBase):
    """_BaseServerOptionsEditView POST: formset invalid and always_send coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])

    def test_post_invalid_formset_rerenders(self):
        """POST with an invalid formset must re-render (not crash)."""
        with stub_kea(_catalogue_responses_for_subnets(4, [])) as kea:
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
        self.assertNotIn("config-set", kea.commands())

    def test_post_with_always_send_includes_flag(self):
        """POST with always_send=True writes always-send=True into the server option-data."""
        with _persist_stub(_EMPTY_SERVER_OPTIONS_CONFIG_GET) as kea:
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
        opts = _written_config(kea)["Dhcp4"]["option-data"]
        self.assertGreaterEqual(len([o for o in opts if o.get("always-send")]), 1)


# ---------------------------------------------------------------------------
# OptionDef add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddExceptions(_ViewTestBase):
    """BaseServerOptionDefAddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])

    def test_post_invalid_form_rerenders(self):
        """POST with invalid form (missing required fields) must return 200, no Kea call."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"name": "", "code": "", "type": "", "space": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_post_with_array_true_passes_flag(self):
        """POST with array=True writes array=True into the option-def."""
        with _persist_stub(_option_def_config([])) as kea:
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
        added = next(d for d in _written_config(kea)["Dhcp4"]["option-def"] if d.get("code") == 200)
        self.assertIs(added.get("array"), True)
        self.assertEqual(added["name"], "my-option")
        self.assertEqual(added["type"], "string")
        self.assertEqual(added["space"], "dhcp4")

    def test_post_kea_exception_shows_error_and_redirects(self):
        """KeaException on the mutation must show error message and redirect."""
        with stub_kea({"config-get": {"result": 1, "text": "duplicate code"}}):
            response = self.client.post(
                self._url(),
                {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
                follow=True,
            )
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_post_generic_exception_propagates(self):
        """A non-Kea error (RuntimeError) propagates (not swallowed by the mutation handler)."""
        with stub_kea({"config-get": RuntimeError("crash")}), self.assertRaises(RuntimeError):
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

    def test_post_kea_exception_shows_error_and_redirects(self):
        """KeaException on the mutation must show error message."""
        with stub_kea({"config-get": {"result": 1, "text": "not found"}}):
            response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_post_generic_exception_propagates(self):
        """A non-Kea error (RuntimeError) propagates (not swallowed)."""
        with stub_kea({"config-get": RuntimeError("crash")}), self.assertRaises(RuntimeError):
            self.client.post(self._url())


# ---------------------------------------------------------------------------
# Subnet options — subnet in shared-network + POST handler
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsSharedNetwork(_ViewTestBase):
    """Subnet found inside a shared-network + invalid-POST re-render."""

    def _url(self, subnet_id=99):
        return reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, subnet_id])

    _SUBNET = {"id": 99, "subnet": "10.99.0.0/24", "option-data": []}

    def test_get_subnet_in_shared_network(self):
        """A subnet found inside a shared-network is located and rendered."""
        with stub_kea(
            _catalogue_responses_for_subnets(4, [], shared_networks=[{"name": "sn", "subnet4": [self._SUBNET]}])
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.99.0.0/24")

    def test_post_invalid_formset_rerenders(self):
        """Invalid formset re-renders the form (subnet inside shared-networks), no mutation."""
        with stub_kea(
            _catalogue_responses_for_subnets(4, [], shared_networks=[{"name": "sn", "subnet4": [self._SUBNET]}])
        ) as kea:
            response = self.client.post(self._url(), {"form-0-name": "dns-servers"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.99.0.0/24")
        self.assertNotIn("config-set", kea.commands())


# ---------------------------------------------------------------------------
# TestKeaConfigTestErrorHandling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestKeaConfigTestErrorHandling(_ViewTestBase):
    """A config-test failure (KeaConfigTestError) must produce a user-facing error message.

    Driven by registering ``config-test`` → result 1, so the real ``_apply_config``
    raises ``KeaConfigTestError`` before ``config-set``.
    """

    _CONFIG_TEST_FAILS = {"config-test": {"result": 1, "text": "config test failed"}}

    def _assert_config_error(self, response):
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("config" in m.lower() and "no changes" in m.lower() for m in msgs))

    def test_subnet_options_config_test_error_shows_message(self):
        """POST to subnet options edit shows the config-test error message."""
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 42])
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET, **_SUBNET4_REDIRECT_IDENTITY, **self._CONFIG_TEST_FAILS):
            response = self.client.post(
                url,
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
                follow=True,
            )
        self._assert_config_error(response)

    def test_server_options_config_test_error_shows_message(self):
        """POST to server options edit shows the config-test error message."""
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        with _persist_stub(_EMPTY_SERVER_OPTIONS_CONFIG_GET, **self._CONFIG_TEST_FAILS):
            response = self.client.post(
                url,
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
                follow=True,
            )
        self._assert_config_error(response)

    def test_option_def_add_config_test_error_shows_message(self):
        """POST to option-def add shows the config-test error message."""
        url = reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])
        with _persist_stub(_option_def_config([]), **self._CONFIG_TEST_FAILS):
            response = self.client.post(
                url,
                {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
                follow=True,
            )
        self._assert_config_error(response)

    def test_option_def_del_config_test_error_shows_message(self):
        """POST to option-def delete shows the config-test error message."""
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        with _persist_stub(_option_def_config(_OPTION_DEF_LIST_V4), **self._CONFIG_TEST_FAILS):
            response = self.client.post(url, follow=True)
        self._assert_config_error(response)


# ---------------------------------------------------------------------------
# Subnet options POST: PartialPersistError, TransportError, ValueError
# ---------------------------------------------------------------------------

_SENTINEL_URL = "https://kea-internal.example.invalid:8443"


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsPartialPersistError(_ViewTestBase):
    """POST to subnet options edit: config-write fails → PartialPersistError → warning."""

    def test_partial_persist_error_shows_warning(self):
        """A config-write failure on a persisting op surfaces a warning (change unpersisted)."""
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 42])
        applied_config = copy.deepcopy(_OPTIONS_CONFIG_GET)
        applied_config[0]["arguments"]["Dhcp4"]["subnet4"][0]["option-data"] = [{"name": "routers", "data": "10.0.0.1"}]
        with _persist_stub(
            queued(_EMPTY_OPTIONS_CONFIG_GET, applied_config),
            **_SUBNET4_REDIRECT_IDENTITY,
            **{"config-write": {"result": 1, "text": "write failed"}},
        ):
            response = self.client.post(
                url,
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
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsTransportError(_ViewTestBase):
    """POST to subnet options edit: config-get raises ConnectionError → transport error message."""

    def test_connection_error_shows_transport_message(self):
        """A persistent transport failure redirects and reports the mutation error."""
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 42])
        with _persist_stub(
            requests.ConnectionError(f"{_SENTINEL_URL} refused connection"),
            **_SUBNET4_REDIRECT_IDENTITY,
        ):
            response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.redirect_chain)
        self.assertNotContains(response, "DDNS: recovered.example.invalid")
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))
        self.assertFalse(any(_SENTINEL_URL.lower() in m.lower() for m in msgs))

    def test_connection_error_recovers_for_redirect_display(self):
        """A transport error surfaces a generic message and never leaks the internal URL."""
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 42])
        recovered = copy.deepcopy(_OPTIONS_CONFIG_GET)
        recovered[0]["arguments"]["Dhcp4"]["subnet4"][0]["ddns-qualifying-suffix"] = "recovered.example.invalid"
        with _persist_stub(
            queued(requests.ConnectionError(f"{_SENTINEL_URL} refused connection"), recovered),
            **_SUBNET4_REDIRECT_IDENTITY,
        ):
            response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DDNS: recovered.example.invalid")
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))
        self.assertFalse(any(_SENTINEL_URL.lower() in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetOptionsValueError(_ViewTestBase):
    """POST to subnet options edit: config-get raises ValueError → invalid config message."""

    def test_value_error_shows_invalid_config_message(self):
        """A persistent parse failure redirects and reports the mutation error."""
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 42])
        with _persist_stub(ValueError("bad config"), **_SUBNET4_REDIRECT_IDENTITY):
            response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.redirect_chain)
        self.assertNotContains(response, "DDNS: recovered.example.invalid")
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid kea client configuration" in m.lower() for m in msgs))
        self.assertFalse(any("bad config" in m.lower() for m in msgs))

    def test_value_error_recovers_for_redirect_display(self):
        """A ValueError surfaces a generic message and never leaks its detail."""
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 42])
        recovered = copy.deepcopy(_OPTIONS_CONFIG_GET)
        recovered[0]["arguments"]["Dhcp4"]["subnet4"][0]["ddns-qualifying-suffix"] = "recovered.example.invalid"
        with _persist_stub(queued(ValueError("bad config"), recovered), **_SUBNET4_REDIRECT_IDENTITY):
            response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DDNS: recovered.example.invalid")
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid kea client configuration" in m.lower() for m in msgs))
        self.assertFalse(any("bad config" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Server options POST: PartialPersistError, TransportError, ValueError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsPartialPersistError(_ViewTestBase):
    """POST to server options edit: config-write fails → PartialPersistError → warning."""

    def test_partial_persist_error_shows_warning_without_success(self):
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        with _persist_stub(_EMPTY_SERVER_OPTIONS_CONFIG_GET, **{"config-write": {"result": 1, "text": "write failed"}}):
            response = self.client.post(
                url,
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
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertIn(
            "Change applied but may not survive a Kea restart (config-write failed).",
            [str(message) for message in msgs if message.level == django_messages.WARNING],
        )
        self.assertNotIn(
            "DHCPv4 server options updated.",
            [str(message) for message in msgs if message.level == django_messages.SUCCESS],
        )

    def test_lost_config_set_reply_is_not_reported_as_applied(self):
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        with _persist_stub(
            _EMPTY_SERVER_OPTIONS_CONFIG_GET, **{"config-set": requests.ConnectionError("reply lost")}
        ) as kea:
            response = self.client.post(
                url,
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
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("config-write", kea.commands())
        texts = [str(message) for message in response.context["messages"]]
        self.assertFalse(any("applied" in text for text in texts), texts)
        self.assertIn("Kea did not confirm the change. Check the server configuration before retrying.", texts)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsTransportError(_ViewTestBase):
    """POST to server options edit: config-get raises ConnectionError → transport error message."""

    def test_connection_error_shows_transport_message(self):
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        with stub_kea({"config-get": requests.ConnectionError(f"{_SENTINEL_URL} refused connection")}):
            response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))
        self.assertFalse(any(_SENTINEL_URL.lower() in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsValueError(_ViewTestBase):
    """POST to server options edit: config-get raises ValueError → invalid config message."""

    def test_value_error_shows_invalid_config_message(self):
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])
        with stub_kea({"config-get": ValueError("bad config")}):
            response = self.client.post(url, {"form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid kea client configuration" in m.lower() for m in msgs))
        self.assertFalse(any("bad config" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Option-def add POST: PartialPersistError, TransportError, ValueError
# ---------------------------------------------------------------------------

_OPTION_DEF_ADD_POST = {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddPartialPersistError(_ViewTestBase):
    """POST to option-def add: config-write fails → PartialPersistError → warning."""

    def test_partial_persist_error_shows_warning(self):
        url = reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])
        with _persist_stub(_option_def_config([]), **{"config-write": {"result": 1, "text": "write failed"}}):
            response = self.client.post(url, _OPTION_DEF_ADD_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddTransportError(_ViewTestBase):
    """POST to option-def add: config-get raises ConnectionError → transport error message."""

    def test_connection_error_shows_transport_message(self):
        url = reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])
        with stub_kea({"config-get": requests.ConnectionError(f"{_SENTINEL_URL} refused connection")}):
            response = self.client.post(url, _OPTION_DEF_ADD_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))
        self.assertFalse(any(_SENTINEL_URL.lower() in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefAddValueError(_ViewTestBase):
    """POST to option-def add: config-get raises ValueError → invalid config message."""

    def test_value_error_shows_invalid_config_message(self):
        url = reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk])
        with stub_kea({"config-get": ValueError("bad config")}):
            response = self.client.post(url, _OPTION_DEF_ADD_POST, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid kea client configuration" in m.lower() for m in msgs))
        self.assertFalse(any("bad config" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Option-def delete POST: PartialPersistError, TransportError, ValueError
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefDeletePartialPersistError(_ViewTestBase):
    """POST to option-def delete: config-write fails → PartialPersistError → warning."""

    def test_partial_persist_error_shows_warning(self):
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        with _persist_stub(
            _option_def_config(_OPTION_DEF_LIST_V4), **{"config-write": {"result": 1, "text": "write failed"}}
        ):
            response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefDeleteTransportError(_ViewTestBase):
    """POST to option-def delete: config-get raises ConnectionError → transport error message."""

    def test_connection_error_shows_transport_message(self):
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        with stub_kea({"config-get": requests.ConnectionError(f"{_SENTINEL_URL} refused connection")}):
            response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("transport error" in m.lower() for m in msgs))
        self.assertFalse(any(_SENTINEL_URL.lower() in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefDeleteValueError(_ViewTestBase):
    """POST to option-def delete: config-get raises ValueError → invalid config message."""

    def test_value_error_shows_invalid_config_message(self):
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"])
        with stub_kea({"config-get": ValueError("bad config")}):
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

    def test_get_client_value_error_redirects(self):
        """A get_client ValueError (cert-without-key) redirects with a generic message."""
        bad = _make_db_server(name="badtls-subnet", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[bad.pk, 1])
        with stub_kea({}):
            response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("could not load subnet configuration" in m.lower() for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionsGetClientError(_ViewTestBase):
    """GET to server options edit when get_client raises ValueError → redirect with error."""

    def test_get_client_value_error_redirects(self):
        """A get_client ValueError (cert-without-key) redirects with a generic message."""
        bad = _make_db_server(name="badtls-server", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[bad.pk])
        with stub_kea({}):
            response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("could not load server options" in m.lower() for m in msgs))
        self.assertTrue(any("configuration facts are unavailable" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Option-def list fetch error
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionDefListFetchError(_ViewTestBase):
    """GET to option-def list when config-get fails → 200 with options_load_error=True."""

    def test_kea_exception_returns_200_with_error_flag(self):
        """A KeaException while fetching the option-def list yields 200 + options_load_error."""
        url = reverse("plugins:netbox_kea:server_option_def4", args=[self.server.pk])
        with stub_kea({"config-get": {"result": 1, "text": "error"}}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("options_load_error"))
        message_text = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("configuration facts are unavailable" in message for message in message_text))


# ---------------------------------------------------------------------------
# Combined status badge error
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedStatusBadgeError(_ViewTestBase):
    """GET to status badge when version-get fails → offline status."""

    def test_kea_exception_returns_200_with_offline(self):
        """A KeaException on version-get yields 200 with offline status badges."""
        url = reverse("plugins:netbox_kea:combined_server_status_badge", args=[self.server.pk])
        with stub_kea({"version-get": {"result": 1, "text": "error"}}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("offline", response.content.decode().lower())


class TestConfigurationOptionIdentity(_ViewTestBase):
    def test_concurrent_option_addition_aborts_before_configuration_write(self):
        original = [{"code": 6, "data": "198.18.0.53"}]
        added = {"code": 42, "data": "198.18.0.123", "never-send": True}
        for scope in ("server", "subnet"):
            for delete in (False, True):
                with self.subTest(scope=scope, delete=delete):
                    with _persist_stub(self._config(scope, original)):
                        data = self._submitted(self.client.get(self._url(scope)))
                    if delete:
                        data["form-0-DELETE"] = "on"
                    with _persist_stub(self._config(scope, [*original, added])) as kea:
                        response = self.client.post(self._url(scope), data)
                    self.assertNotIn("config-test", kea.commands())
                    self.assertNotIn("config-set", kea.commands())
                    messages = [str(message) for message in django_messages.get_messages(response.wsgi_request)]
                    self.assertIn("DHCP Options changed or are ambiguous. Reload the form before saving.", messages)

    def test_class_specific_options_keep_distinct_values_and_metadata(self):
        options = [
            {
                "code": 6,
                "space": "dhcp4",
                "data": "198.18.0.53",
                "client-classes": ["group-a", "group-b"],
                "csv-format": True,
            },
            {"code": 6, "space": "dhcp4", "data": "198.18.0.54", "client-classes": ["group-c"], "always-send": True},
            {"code": 6, "space": "dhcp4", "data": "198.18.0.55"},
        ]
        for scope in ("server", "subnet"):
            with self.subTest(scope=scope):
                with _persist_stub(self._config(scope, options)):
                    data = self._submitted(self.client.get(self._url(scope)))
                data["form-1-data"] = "198.18.0.56"
                live = copy.deepcopy(options)
                live[0]["client-classes"].reverse()
                with _persist_stub(self._config(scope, live)) as kea:
                    post = self.client.post(self._url(scope), data)
                self.assertEqual(post.status_code, 302)
                written = _written_config(kea)["Dhcp4"]
                if scope == "subnet":
                    written = written["subnet4"][0]
                expected = copy.deepcopy(live)
                expected[1]["data"] = "198.18.0.56"
                self.assertEqual(written["option-data"], expected)

    def test_coded_option_name_cannot_change_to_an_incompatible_option(self):
        for scope in ("server", "subnet"):
            for name in ("routers", None):
                option = {"code": 3, "space": "dhcp4", "data": "198.18.0.1", "csv-format": True}
                if name is not None:
                    option["name"] = name
                with self.subTest(scope=scope, name=name), _persist_stub(self._config(scope, [option])) as kea:
                    data = self._submitted(self.client.get(self._url(scope)))
                    data["form-0-name"] = "domain-name-servers"
                    post = self.client.post(self._url(scope), data)
                    self.assertNotIn("config-test", kea.commands())
                    self.assertNotIn("config-set", kea.commands())
                    messages = [str(message) for message in django_messages.get_messages(post.wsgi_request)]
                    self.assertIn(
                        "A coded DHCP Option cannot be renamed. Delete it and add a new option instead.", messages
                    )

    def _url(self, scope):
        if scope == "subnet":
            return reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 42])
        return reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk])

    def _config(self, scope, options):
        body = (
            {"option-data": options}
            if scope == "server"
            else {"subnet4": [{"id": 42, "subnet": "198.18.0.0/24", "option-data": options}]}
        )
        return {"result": 0, "arguments": {"Dhcp4": body}}

    def _submitted(self, response):
        forms = response.context["formset"]
        data = {"form-TOTAL_FORMS": str(len(forms.initial)), "form-INITIAL_FORMS": str(len(forms.initial))}
        for index, form in enumerate(forms.forms[: len(forms.initial)]):
            for field in form:
                value = field.value()
                if value is not None and value is not False:
                    data[f"form-{index}-{field.name}"] = value
        return data

    def test_round_trip_preserves_identity_and_unexposed_metadata(self):
        options = [
            {
                "code": 224,
                "space": "vendor-example",
                "data": "aabb",
                "csv-format": False,
                "never-send": True,
                "always-send": False,
                "user-context": {"note": "keep"},
            },
            {"name": "routers", "code": 3, "space": "dhcp4", "data": "198.18.0.1", "csv-format": True},
            {"code": 6, "never-send": True},
        ]
        for scope in ("server", "subnet"):
            with self.subTest(scope=scope), _persist_stub(self._config(scope, options)) as kea:
                get = self.client.get(self._url(scope))
                data = self._submitted(get)
                data["form-1-data"] = "198.18.0.2"
                post = self.client.post(self._url(scope), data)
                self.assertEqual(post.status_code, 302)
                written = _written_config(kea)["Dhcp4"]
                if scope == "subnet":
                    written = written["subnet4"][0]
                expected = copy.deepcopy(options)
                expected[1]["data"] = "198.18.0.2"
                self.assertEqual(written["option-data"], expected)

    def test_reordered_live_options_keep_their_own_metadata_and_allow_deletion(self):
        options = [
            {"name": "routers", "data": "198.18.0.1", "csv-format": True},
            {"code": 6, "data": "198.18.0.53", "never-send": True},
        ]
        for scope in ("server", "subnet"):
            with self.subTest(scope=scope):
                with _persist_stub(self._config(scope, options)):
                    data = self._submitted(self.client.get(self._url(scope)))
                data["form-0-DELETE"] = "on"
                with _persist_stub(self._config(scope, options[::-1])) as kea:
                    post = self.client.post(self._url(scope), data)
                self.assertEqual(post.status_code, 302)
                written = _written_config(kea)["Dhcp4"]
                if scope == "subnet":
                    written = written["subnet4"][0]
                self.assertEqual(written["option-data"], [options[1]])

    def test_missing_ambiguous_and_duplicate_targets_abort_before_write(self):
        options = [{"name": "routers", "data": "198.18.0.1"}]
        for live in ([], options * 2):
            with self.subTest(live=live):
                with _persist_stub(self._config("server", options)):
                    data = self._submitted(self.client.get(self._url("server")))
                with _persist_stub(self._config("server", live)) as kea:
                    post = self.client.post(self._url("server"), data)
                self.assertNotIn("config-test", kea.commands())
                self.assertNotIn("config-set", kea.commands())
                messages = [str(message) for message in django_messages.get_messages(post.wsgi_request)]
                self.assertIn("DHCP Options changed or are ambiguous. Reload the form before saving.", messages)
        with _persist_stub(self._config("server", options)):
            data = self._submitted(self.client.get(self._url("server")))
        data.update(
            {key.replace("form-0-", "form-1-"): value for key, value in list(data.items()) if key.startswith("form-0-")}
        )
        data["form-TOTAL_FORMS"] = "2"
        data["form-INITIAL_FORMS"] = "2"
        with _persist_stub(self._config("server", options)) as kea:
            self.client.post(self._url("server"), data)
            self.assertNotIn("config-set", kea.commands())
