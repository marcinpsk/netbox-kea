# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Options-view tests for the netbox_kea plugin.

Covers the views in ``netbox_kea/views/options.py`` (e.g.
``ServerSubnetOptionsEditView``, ``ServerOptionDefAddView``,
``ServerOptionDef4DeleteView``, etc.).

These tests drive the **real** ``KeaClient`` through ``config_write``; only the
HTTP boundary is stubbed via ``kea_stub.stub_kea``, so the request payloads the
views actually send to Kea are exercised. Free Kea has no option-set hook, so
every option-data / option-def change is a read-modify-write:

    config-get  →  config-test  →  config-set  →  persist step

A Subnet options change first opens the Subnet Catalogue scope
(``subnet{v}-list`` and ``config-get``), so ``_persist_stub`` also answers
``subnet{v}-list`` from the Subnets in its ``config-get`` reply. The tests assert
on the **config-set body** (``kea.bodies("config-set")[0]["arguments"]``), which
proves the version, the target, and the option payload end to end.

``TestOptionChangeMessages`` posts once per Configuration Change outcome and per
rejection; the Kea reply of each case lives in ``utils._read_modify_write_cases``.
"""

import copy
import html
import json
import re

import requests
from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse

from netbox_kea import server_configuration

from .kea_stub import _catalogue_responses_for_subnets, _subnet_list, stub_kea
from .utils import _PLUGINS_CONFIG, _make_db_server, _ReadModifyWriteMessages, _ViewTestBase

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
_PERSIST = ["config-get", "config-test", "config-write"]


def _option_def_config(defs, version=4):
    """Return one Server Configuration response with Option Definitions."""
    return [_catalogue_responses_for_subnets(version, [], option_definitions=tuple(defs))["config-get"]]


def _identities(config_get) -> dict:
    """The ``subnet{v}-list`` reply that matches the Subnets of one config-get reply, for the Subnet scope."""
    reply = config_get[0] if isinstance(config_get, list) else config_get
    for family in (4, 6):
        daemon = reply.get("arguments", {}).get(f"Dhcp{family}")
        if isinstance(daemon, dict):
            key = f"subnet{family}"
            subnets = [{"id": s["id"], "subnet": s["subnet"]} for s in daemon.get(key, [])]
            for network in daemon.get("shared-networks", []):
                subnets += [
                    {"id": s["id"], "subnet": s["subnet"], "shared-network-name": network["name"]}
                    for s in network.get(key, [])
                ]
            return {f"{key}-list": _subnet_list(family, subnets)}
    return {}


def _persist_stub(config_get, **overrides):
    """Stub the read-modify-write chain: config-get → config-test → config-set → config-write.

    *config_get* is deep-copied so the read-modify-write mutation (which edits the
    config in place) never corrupts the shared module-level fixture. Assert on the
    resulting config-set body via ``kea.bodies("config-set")[0]["arguments"]``.
    ``subnet{v}-list`` answers from the Subnets of *config_get*, so a Subnet options change can open its scope.

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
        **_identities(config_get),
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

    def _post_data(self, name="routers", data="10.0.0.1", always_send="", delete="", cidr="10.0.0.0/24"):
        return {
            "subnet_cidr": cidr,
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

    def test_get_carries_the_subnet_cidr_that_the_page_shows(self):
        with stub_kea(_catalogue_responses_for_subnets(4, [{**_SUBNET4, "subnet": "10.0.0.5/24"}])):
            response = self.client.get(self._url())
        self.assertContains(response, '<input type="hidden" name="subnet_cidr" value="10.0.0.5/24"', html=False)

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
            [
                {
                    "name": "",
                    "data": "opaque",
                    "always_send": True,
                    "original_option": {"code": 222, "data": "opaque", "always-send": True},
                }
            ],
        )
        hidden = re.search(r'name="form-0-original_option" value="([^"]*)"', response.content.decode())
        self.assertEqual(json.loads(html.unescape(hidden[1])), {"code": 222, "data": "opaque", "always-send": True})

    def test_get_refuses_a_subnet_that_kea_did_not_verify(self):
        """A Subnet options change needs a Verified Subnet, so the form is not offered without one."""
        responses = _catalogue_responses_for_subnets(4, [_SUBNET4])
        responses["subnet4-list"] = {"result": 2, "text": "'subnet4-list' command not supported."}
        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
        self.assertIn(
            (
                django_messages.ERROR,
                (
                    "Kea did not confirm the identity of Subnet 42, because the subnet_cmds hook is not loaded. "
                    "NetBox changes the DHCP Options of a Subnet only after Kea confirms its identity."
                ),
            ),
            self._messages(response),
        )

    def test_post_without_the_subnet_cmds_hook_is_rejected_before_config_set(self):
        responses = {"subnet4-list": {"result": 2, "text": "'subnet4-list' command not supported."}}
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET, **responses) as kea:
            response = self.client.post(self._url(), self._post_data())

        self._assert_redirect_to_integer_pk(response)
        self.assertEqual(
            self._messages(response),
            [
                (
                    django_messages.ERROR,
                    (
                        "The change was not sent to Kea. NetBox could not confirm Kea's Subnet list, so it did not "
                        "send the change. Try again later."
                    ),
                )
            ],
        )
        self.assertNotIn("config-set", kea.commands())

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
            warm = kea.commands().count("config-get")
            self.client.get(self._url())

        self.assertEqual(kea.commands().count("config-get"), warm + 1)

    def test_get_unavailable_configuration_shows_diagnostic_and_redirects(self):
        unreachable = requests.ConnectionError("unreachable")
        with stub_kea({"subnet4-list": unreachable, "config-get": unreachable}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 302)
        texts = [str(message) for message in django_messages.get_messages(response.wsgi_request)]
        self.assertIn("Kea configuration facts are unavailable.", texts)
        self.assertIn(
            "Kea did not confirm the identity of Subnet 42. "
            "NetBox changes the DHCP Options of a Subnet only after Kea confirms its identity.",
            texts,
        )

    def test_post_runs_the_read_modify_write_on_the_verified_subnet(self):
        """POST opens the Subnet scope, then runs the read-modify-write and redirects."""
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET) as kea:
            response = self.client.post(self._url(), self._post_data())
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)
        self.assertEqual(
            kea.commands(),
            ["subnet4-list", "config-get", "config-get", "config-test", "config-set", *_PERSIST],
        )

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
            "subnet_cidr": "10.0.0.0/24",
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

    def _messages(self, response) -> list:
        return [(m.level, str(m)) for m in django_messages.get_messages(response.wsgi_request)]

    def test_post_for_a_subnet_that_changed_asks_for_a_reload(self):
        """Subnet 42 now names another network, or is gone, so the change reaches no Subnet."""
        reload = (
            "The change was not sent to Kea. Subnet 42 (10.0.0.0/24) changed in Kea. Reload the page and try again."
        )
        for cidr in ("10.0.9.0/24", None):
            with self.subTest(cidr=cidr):
                self._fresh_client()
                subnets = [] if cidr is None else [{**_SUBNET4, "subnet": cidr}]
                with _persist_stub([_catalogue_responses_for_subnets(4, subnets)["config-get"]]) as kea:
                    response = self.client.post(self._url(), self._post_data())
                self._assert_redirect_to_integer_pk(response)
                self.assertEqual(self._messages(response), [(django_messages.ERROR, reload)])
                self.assertEqual(kea.commands(), ["subnet4-list", "config-get"])

    def test_post_without_a_valid_subnet_cidr_sends_nothing(self):
        missing = (
            "The page did not send a valid Subnet CIDR, so nothing was sent to Kea. Reload the page and try again."
        )
        for cidr in ("", "2001:db8::/64", "not-a-cidr"):
            with self.subTest(cidr=cidr):
                self._fresh_client()
                with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET) as kea:
                    response = self.client.post(self._url(), self._post_data(cidr=cidr))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self._messages(response), [(django_messages.ERROR, missing)])
                self.assertEqual(kea.commands(), [])

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
                self._post_data(name="dns-servers", data="2001:4860:4860::8888", cidr="2001:db8::/64"),
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

    def test_get_help_names_each_command_of_the_change(self):
        with stub_kea({"config-get": _SERVER_OPTIONS_CONFIG_GET}):
            response = self.client.get(self._url())
        self._assert_help_names_the_read_modify_write(response)

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
        self.assertIn("Kea returned an invalid DHCP Option.", texts)

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

    def test_post_runs_the_read_modify_write(self):
        """POST with valid formset runs the read-modify-write and redirects."""
        with _persist_stub(_EMPTY_SERVER_OPTIONS_CONFIG_GET) as kea:
            response = self.client.post(self._url(), self._post_data())
        self.assertEqual(response.status_code, 302)
        self._assert_redirect_to_integer_pk(response)
        self.assertEqual(kea.commands(), ["config-get", "config-test", "config-set", *_PERSIST])

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
        """A config-get failure result is not sent, and shows an error message."""
        with stub_kea({"config-get": {"result": 1, "text": "internal error"}}):
            response = self.client.post(self._url(), self._post_data())
        self.assertEqual(response.status_code, 302)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_post_non_object_family_configuration_redirects_without_500(self):
        with stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp4": []}}}) as kea:
            response = self.client.post(self._url(), self._post_data())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands(), ["config-get"])
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

    def test_malformed_definitions_are_dropped_with_a_warning(self):
        valid = {"name": "site", "code": 222, "type": "record", "space": "dhcp4", "record-types": "uint16, string"}
        cases = (
            ({"site": valid}, "Kea returned a non-list Option Definition collection."),
            ([{**valid, "array": "yes"}], "Kea returned an invalid Option Definition."),
            ([{**valid, "encapsulate": 7}], "Kea returned an invalid Option Definition."),
            ([{**valid, "record-types": "uint16,,string"}], "Kea returned an invalid Option Definition."),
        )
        for definitions, warning in cases:
            with self.subTest(definitions=definitions):
                server_configuration.invalidate(self.server, 4)
                config = {"result": 0, "arguments": {"Dhcp4": {"option-def": definitions}}}
                with stub_kea({"config-get": config}):
                    response = self.client.get(self._url())

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["option_defs"], [])
                warnings = [
                    str(message) for message in response.context["messages"] if message.level == django_messages.WARNING
                ]
                self.assertIn(warning, warnings)

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

    def test_post_valid_adds_the_option_definition(self):
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

    def test_post_adds_the_option_definition_to_dhcp6(self):
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

    def test_post_deletes_the_option_definition_and_redirects(self):
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
        """A config-get failure result is not sent, and shows an error message."""
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

    def test_post_deletes_the_option_definition_from_dhcp6(self):
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
        with stub_kea({}) as kea:
            response = self.client.post(
                self._url(),
                {
                    "subnet_cidr": "10.0.0.0/24",
                    "form-TOTAL_FORMS": "1",
                    "form-INITIAL_FORMS": "0",
                    "form-MIN_NUM_FORMS": "0",
                    "form-MAX_NUM_FORMS": "1000",
                    "form-0-name": "",
                    "form-0-data": "some-value",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<input type="hidden" name="subnet_cidr" value="10.0.0.0/24"', html=False)
        self.assertEqual(kea.commands(), [])

    def test_post_with_always_send_includes_flag(self):
        """POST with always_send=True writes always-send=True into the option-data."""
        with _persist_stub(_EMPTY_OPTIONS_CONFIG_GET) as kea:
            self.client.post(
                self._url(),
                {
                    "subnet_cidr": "10.0.0.0/24",
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

    def test_post_new_row_without_data_rerenders_with_a_data_error(self):
        """A new option row needs a value; only an existing option may keep its data empty."""
        with stub_kea(_catalogue_responses_for_subnets(4, [])) as kea:
            response = self.client.post(
                self._url(),
                {
                    "form-TOTAL_FORMS": "1",
                    "form-INITIAL_FORMS": "0",
                    "form-MIN_NUM_FORMS": "0",
                    "form-MAX_NUM_FORMS": "1000",
                    "form-0-name": "routers",
                    "form-0-data": "",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["formset"].forms[0].errors["data"], ["This field is required."])
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
        responses = _catalogue_responses_for_subnets(4, [], shared_networks=[{"name": "sn", "subnet4": [self._SUBNET]}])
        responses["subnet4-list"] = _identities(responses["config-get"])["subnet4-list"]
        with stub_kea(responses):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.99.0.0/24")

    def test_post_invalid_formset_rerenders(self):
        """Invalid formset re-renders the form with the posted Subnet CIDR, and reads nothing from Kea."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"subnet_cidr": "10.99.0.0/24", "form-0-name": "dns-servers"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.99.0.0/24")
        self.assertEqual(kea.commands(), [])

    def test_post_changes_the_options_of_a_subnet_in_a_shared_network(self):
        config = _catalogue_responses_for_subnets(4, [], shared_networks=[{"name": "sn", "subnet4": [self._SUBNET]}])
        data = {
            "subnet_cidr": "10.99.0.0/24",
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-0-name": "routers",
            "form-0-data": "10.99.0.1",
        }
        with _persist_stub([config["config-get"]]) as kea:
            response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        member = _written_config(kea)["Dhcp4"]["shared-networks"][0]["subnet4"][0]
        self.assertEqual(member["option-data"], [{"name": "routers", "data": "10.99.0.1"}])


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
        self.assertTrue(any("did not confirm the identity of subnet 1." in m.lower() for m in msgs))


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

    def test_a_stale_value_in_the_form_is_a_conflict_before_any_write(self):
        seen = [{"code": 6, "data": "198.18.0.53"}, {"code": 42, "data": "198.18.0.123"}]
        live = [{"code": 6, "data": "198.18.0.54"}, seen[1]]
        for scope in ("server", "subnet"):
            with self.subTest(scope=scope):
                with _persist_stub(self._config(scope, seen)):
                    data = self._submitted(self.client.get(self._url(scope)))
                data["form-1-data"] = "198.18.0.124"
                with _persist_stub(self._config(scope, live)) as kea:
                    response = self.client.post(self._url(scope), data)
                self.assertEqual(response.status_code, 302)
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

    def test_a_tampered_original_identity_is_a_form_error_before_any_write(self):
        options = [{"code": 6, "space": "dhcp4", "data": "198.18.0.53", "client-classes": ["group-a"]}]
        for classes in ("group-a", [""]):
            with self.subTest(classes=classes):
                with _persist_stub(self._config("server", options)):
                    data = self._submitted(self.client.get(self._url("server")))
                data["form-0-original_option"] = json.dumps({**options[0], "client-classes": classes})
                with _persist_stub(self._config("server", options)) as kea:
                    post = self.client.post(self._url("server"), data)
                self.assertEqual(post.status_code, 200)
                self.assertEqual(
                    post.context["formset"].forms[0].errors["original_option"],
                    ["Invalid original DHCP Option identity."],
                )
                self.assertNotIn("config-set", kea.commands())

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
        # The hidden Subnet CIDR field, as a browser posts it.
        data.update(re.findall(r'name="(subnet_cidr)" value="([^"]*)"', response.content.decode()))
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


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOptionChangeMessages(_ReadModifyWriteMessages):
    """One POST per Configuration Change outcome and per rejection, for each options view."""

    _ROW = {
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "0",
        "form-0-name": "routers",
        "form-0-data": "10.0.0.1",
    }

    def test_subnet_options(self):
        responses = {**_identities(_EMPTY_OPTIONS_CONFIG_GET), "config-get": _EMPTY_OPTIONS_CONFIG_GET}
        self._assert_one_message_per_case(
            reverse("plugins:netbox_kea:server_subnet4_options_edit", args=[self.server.pk, 42]),
            {**self._ROW, "subnet_cidr": "10.0.0.0/24"},
            responses,
            "Subnet 42 options updated.",
        )

    def test_server_options(self):
        self._assert_one_message_per_case(
            reverse("plugins:netbox_kea:server_dhcp4_options_edit", args=[self.server.pk]),
            self._ROW,
            {"config-get": _EMPTY_SERVER_OPTIONS_CONFIG_GET},
            "DHCPv4 server options updated.",
        )

    def test_option_definition_add(self):
        self._assert_one_message_per_case(
            reverse("plugins:netbox_kea:server_option_def4_add", args=[self.server.pk]),
            {"name": "my-opt", "code": "200", "type": "string", "space": "dhcp4"},
            {"config-get": _option_def_config([])},
            "Option definition 'my-opt' (code 200) added.",
        )

    def test_option_definition_delete(self):
        self._assert_one_message_per_case(
            reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 200, "dhcp4"]),
            {},
            {"config-get": _option_def_config(_OPTION_DEF_LIST_V4)},
            "Option definition code=200 space=dhcp4 deleted.",
        )

    def test_option_definition_delete_of_a_missing_definition_is_not_sent(self):
        url = reverse("plugins:netbox_kea:server_option_def4_delete", args=[self.server.pk, 250, "dhcp4"])
        with _persist_stub(_option_def_config(_OPTION_DEF_LIST_V4)) as kea:
            response = self.client.post(url)
        self.assertEqual(
            [(m.level, str(m)) for m in django_messages.get_messages(response.wsgi_request)],
            [
                (
                    django_messages.ERROR,
                    "The change was not sent to Kea. Option Definition 250 in space 'dhcp4' not found.",
                )
            ],
        )
        self.assertEqual(kea.commands(), ["config-get"])
