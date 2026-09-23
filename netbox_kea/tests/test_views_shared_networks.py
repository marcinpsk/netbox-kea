# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Shared-network view tests for the netbox_kea plugin.

Covers the views in ``netbox_kea/views/shared_networks.py`` (the read-only
shared-networks list tabs plus the add / delete / edit views).

These tests drive the **real** ``KeaClient``; only the HTTP boundary is stubbed
via ``kea_stub.stub_kea``, so the request payloads the views actually send to Kea
are exercised and can be asserted on.

Command chains (all issued through the real client):

* **list** (``ServerSharedNetworks{4,6}View``): a single ``config-get`` per GET.
* **add** (``network_add``): ``network{v}-add`` then ``_persist_config``
  (``config-get`` → ``config-test`` → ``config-write``; ``persist_config``
  defaults True).
* **delete** (``network_del``): ``network{v}-del`` then the same persist chain.
* **edit** (``network_update``): the POST verifies a live Server Configuration,
  then ``network_update`` runs its read-modify-write cycle. The resulting body
  proves the version, network, and DHCP Options end to end.

Error paths are driven through the real client:

* ``KeaException`` ← a mutation command returns ``{"result": 1}``;
* ``KeaConfigTestError`` (a ``KeaException`` subclass) ← ``config-test`` returns
  result 1 during ``network_update``;
* ``PartialPersistError`` ← ``config-write`` returns result 1 on a persisting op;
* transport error ← the failing command is registered as a
  ``requests.RequestException`` instance (raised at the HTTP boundary).
"""

import copy

import requests
from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse

from .kea_stub import _catalogue_responses_for_subnets, queued, stub_kea
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase

_CONFIG_OK = {"result": 0}

# ---------------------------------------------------------------------------
# config-get fixtures for the shared-networks LIST views
# ---------------------------------------------------------------------------

_SHARED_NETWORKS_CONFIG_V4 = _catalogue_responses_for_subnets(
    4,
    [{"id": 1, "subnet": "192.168.0.0/24"}],
    shared_networks=[
        {
            "name": "net-alpha",
            "description": "Alpha test network",
            "subnet4": [
                {"id": 10, "subnet": "10.0.0.0/24"},
                {"id": 11, "subnet": "10.0.1.0/24"},
            ],
        }
    ],
)["config-get"]

_SHARED_NETWORKS_CONFIG_V6 = _catalogue_responses_for_subnets(
    6,
    [],
    shared_networks=[
        {
            "name": "net-beta",
            "description": "",
            "subnet6": [{"id": 20, "subnet": "2001:db8::/48"}],
        }
    ],
)["config-get"]

# A config with an empty shared-networks list (used by not-found / abort paths).
_EMPTY_SN_CONFIG_V4 = _catalogue_responses_for_subnets(4, [])["config-get"]


# ---------------------------------------------------------------------------
# Stub builders (real KeaClient + HTTP-boundary stub)
# ---------------------------------------------------------------------------


def _sn_config(version=4, name="prod-net", description="", option_data=None, subnets=None):
    """config-get payload exposing a single shared network under ``Dhcp{version}``."""
    subnet_key = f"subnet{version}"
    network: dict = {"name": name, "description": description, subnet_key: list(subnets or [])}
    if option_data is not None:
        network["option-data"] = option_data
    return _catalogue_responses_for_subnets(version, [], shared_networks=[network])["config-get"]


def _mutate_stub(command, response=_CONFIG_OK, **overrides):
    """Stub a ``network{v}-add``/``-del`` mutation plus its ``_persist_config`` chain.

    ``network_add``/``network_del`` issue the mutation command then
    ``_persist_config`` (``config-get`` → ``config-test`` → ``config-write``,
    because ``persist_config`` defaults True). When *response* carries an error
    (or is an exception), the mutation raises before persistence, so the persist
    registrations simply go unused.
    """
    version = 4 if command.startswith("network4") else 6
    base = {
        command: response,
        "config-get": _catalogue_responses_for_subnets(version, [])["config-get"],
        "config-test": _CONFIG_OK,
        "config-write": _CONFIG_OK,
    }
    base.update(overrides)
    return stub_kea(base)


def _edit_stub(config_get, **overrides):
    """Stub the shared-network edit read-modify-write chain.

    The edit POST verifies the live Server Configuration before ``network_update``
    runs the write cycle. *config_get* is deep-copied because ``network_update``
    mutates its fetched configuration in place.
    """
    base = {
        "config-get": copy.deepcopy(config_get),
        "config-test": _CONFIG_OK,
        "config-set": _CONFIG_OK,
        "config-write": _CONFIG_OK,
    }
    base.update(overrides)
    return stub_kea(base)


def _written_sn(kea, version=4):
    """Return the shared-network dict ``network_update`` pushed back via config-set."""
    bodies = kea.bodies("config-set")
    assert bodies, "config-set was never issued (network_update did not complete)"
    return bodies[0]["arguments"][f"Dhcp{version}"]["shared-networks"][0]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetworks4View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/shared_networks4/"""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_networks4", args=[self.server.pk])

    def test_get_returns_200(self):
        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V4}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), ["config-get"])

    def test_shows_shared_network_name(self):
        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V4}):
            response = self.client.get(self._url())
        self.assertContains(response, "net-alpha")

    def test_shows_subnet_count(self):
        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V4}):
            response = self.client.get(self._url())
        # 2 subnets in net-alpha — check the Subnets column header is present
        self.assertContains(response, "Subnets")

    def test_shows_subnet_cidrs(self):
        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V4}):
            response = self.client.get(self._url())
        self.assertContains(response, "10.0.0.0/24")
        self.assertContains(response, "10.0.1.0/24")

    def test_empty_table_when_no_shared_networks(self):
        with stub_kea({"config-get": _EMPTY_SN_CONFIG_V4}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "net-alpha")
        self.assertEqual(list(response.context["messages"]), [])

    def test_kea_error_shows_diagnostic_and_keeps_page_available(self):
        with stub_kea({"config-get": {"result": 1, "text": "config-get failed"}}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea Subnet configuration facts are unavailable.")
        self.assertTrue(any(message.level == django_messages.ERROR for message in response.context["messages"]))

    def test_unreachable_server_shows_diagnostic_and_keeps_page_available(self):
        with stub_kea({"config-get": requests.ConnectionError("unreachable")}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea Subnet configuration facts are unavailable.")
        self.assertTrue(any(message.level == django_messages.ERROR for message in response.context["messages"]))

    def test_non_object_family_configuration_shows_diagnostic_instead_of_raising(self):
        with stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp4": []}}}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea did not return a Dhcp4 configuration object.")

    def test_empty_network_appears_in_both_lists_and_subnet_add_choices(self):
        network = {"name": "empty-clients", "description": "No members yet", "subnet4": []}
        responses = _catalogue_responses_for_subnets(4, [], shared_networks=[network])

        with stub_kea(responses) as kea:
            server_list = self.client.get(self._url())
            combined_list = self.client.get(
                reverse("plugins:netbox_kea:combined_shared_networks4"), {"server": self.server.pk}
            )
            subnet_add = self.client.get(reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk]))

        self.assertContains(server_list, "empty-clients")
        self.assertContains(combined_list, "empty-clients")
        self.assertIn(("empty-clients", "empty-clients"), subnet_add.context["form"].fields["shared_network"].choices)
        self.assertEqual(kea.commands().count("config-get"), 1)

    def test_incomplete_snapshot_shows_valid_network_and_warning(self):
        responses = _catalogue_responses_for_subnets(
            4,
            [],
            shared_networks=[{"name": "clients", "subnet4": []}, None],
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "clients")
        self.assertContains(response, "Kea returned a non-object Shared Network.")
        self.assertTrue(any(message.level == django_messages.WARNING for message in response.context["messages"]))

    def test_get_with_dhcp4_disabled_redirects(self):
        v6_only = _make_db_server(name="v6-only-sn", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[v6_only.pk])
        # v4→v6 redirect happens before any client is built, so no Kea traffic.
        with stub_kea({}) as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertEqual(kea.commands(), [])
        # Merged-tab contract: a v6-only server's v4 shared-networks URL redirects to
        # the v6 route (not the server detail page), mirroring leases4/subnets4.
        self.assertEqual(
            response.url,
            reverse("plugins:netbox_kea:server_shared_networks6", args=[v6_only.pk]),
        )

    def test_get_sets_tab_in_context(self):
        """F2: shared networks render under the shared 'Subnets' tab."""
        from netbox_kea.views.subnets import _SUBNETS_TAB

        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V4}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], _SUBNETS_TAB)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetworks6View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/shared_networks6/"""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_networks6", args=[self.server.pk])

    def test_get_returns_200(self):
        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V6}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), ["config-get"])

    def test_shows_shared_network_name(self):
        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V6}):
            response = self.client.get(self._url())
        self.assertContains(response, "net-beta")

    def test_non_object_family_configuration_shows_diagnostic_instead_of_raising(self):
        with stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp6": []}}}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea did not return a Dhcp6 configuration object.")

    def test_shows_subnet_cidrs(self):
        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V6}):
            response = self.client.get(self._url())
        self.assertContains(response, "2001:db8::/48")

    def test_get_sets_tab_in_context(self):
        """F2: shared networks render under the shared 'Subnets' tab."""
        from netbox_kea.views.subnets import _SUBNETS_TAB

        with stub_kea({"config-get": _SHARED_NETWORKS_CONFIG_V6}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], _SUBNETS_TAB)


# ---------------------------------------------------------------------------
# Shared Network Add / Delete views
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4AddView(_ViewTestBase):
    """Tests for ServerSharedNetwork4AddView: GET form + POST create."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_network4_add", args=[self.server.pk])

    def test_get_returns_200_with_form(self):
        """GET must render the add-network form with status 200 (no Kea traffic)."""
        with stub_kea({}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_post_valid_creates_network(self):
        """POST with valid name issues network4-add and redirects."""
        with _mutate_stub("network4-add") as kea:
            response = self.client.post(self._url(), {"name": "net-prod"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn("network4-add", kea.commands())

    def test_post_calls_network_add_with_correct_version(self):
        """POST must send network4-add to the dhcp4 service with the network name."""
        with _mutate_stub("network4-add") as kea:
            self.client.post(self._url(), {"name": "net-prod"})
        body = kea.bodies("network4-add")[0]
        self.assertEqual(body["service"], ["dhcp4"])
        self.assertEqual(body["arguments"]["shared-networks"][0]["name"], "net-prod")

    def test_post_empty_name_shows_form_errors(self):
        """POST with empty name must re-render form (no Kea call)."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_post_kea_exception_shows_error_and_redirects(self):
        """POST that raises KeaException must redirect with an error (no 500)."""
        with _mutate_stub("network4-add", response={"result": 1, "text": "subnet_cmds not loaded"}):
            response = self.client.post(self._url(), {"name": "net-prod"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url(), {"name": "net-x"})
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork6AddView(_ViewTestBase):
    """Tests for ServerSharedNetwork6AddView — verifies v6 variant uses the dhcp6 service."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_network6_add", args=[self.server.pk])

    def test_get_returns_200(self):
        """GET must render the add-network form with status 200."""
        with stub_kea({}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_calls_network_add_with_version_6(self):
        """POST must send network6-add to the dhcp6 service."""
        with _mutate_stub("network6-add") as kea:
            self.client.post(self._url(), {"name": "net6-prod"})
        body = kea.bodies("network6-add")[0]
        self.assertEqual(body["service"], ["dhcp6"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4DeleteView(_ViewTestBase):
    """Tests for ServerSharedNetwork4DeleteView: GET confirm + POST delete."""

    def _url(self, name="net-alpha"):
        return reverse("plugins:netbox_kea:server_shared_network4_delete", args=[self.server.pk, name])

    def test_get_returns_200_with_confirmation_page(self):
        """GET must render a confirmation page mentioning the network name (no Kea)."""
        with stub_kea({}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "net-alpha")
        self.assertEqual(kea.commands(), [])

    def test_post_calls_network_del_and_redirects(self):
        """POST must issue network4-del and redirect to the shared networks tab."""
        with _mutate_stub("network4-del") as kea:
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn("network4-del", kea.commands())

    def test_post_passes_correct_version_and_name(self):
        """POST must send network4-del to dhcp4 with the correct network name."""
        with _mutate_stub("network4-del") as kea:
            self.client.post(self._url(name="net-alpha"))
        body = kea.bodies("network4-del")[0]
        self.assertEqual(body["service"], ["dhcp4"])
        self.assertEqual(body["arguments"], {"name": "net-alpha"})

    def test_post_kea_exception_redirects_with_error(self):
        """POST that raises KeaException must redirect with an error (no 500)."""
        with _mutate_stub("network4-del", response={"result": 1, "text": "network not found"}):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork6DeleteView(_ViewTestBase):
    """Tests for ServerSharedNetwork6DeleteView — verifies v6 variant uses the dhcp6 service."""

    def _url(self, name="net-beta"):
        return reverse("plugins:netbox_kea:server_shared_network6_delete", args=[self.server.pk, name])

    def test_get_returns_200(self):
        """GET must render confirmation page with status 200."""
        with stub_kea({}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_calls_network_del_with_version_6(self):
        """POST must send network6-del to the dhcp6 service."""
        with _mutate_stub("network6-del") as kea:
            self.client.post(self._url(name="net-beta"))
        body = kea.bodies("network6-del")[0]
        self.assertEqual(body["service"], ["dhcp6"])


# ─────────────────────────────────────────────────────────────────────────────
# TestServerSharedNetwork4EditView (F2b)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4EditView(_ViewTestBase):
    """Tests for ServerSharedNetwork4EditView: GET form + POST update."""

    def _url(self, name="prod-net"):
        return reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, name])

    def _post_data(self, **overrides):
        data = {
            "name": "prod-net",
            "description": "x",
            "interface": "",
            "relay_addresses": "",
            "dns_servers": "",
            "ntp_servers": "",
        }
        data.update(overrides)
        return data

    def test_get_returns_200(self):
        """GET renders the edit form with status 200."""
        with stub_kea({"config-get": _sn_config(4, "prod-net", description="Old", option_data=[])}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_reads_the_live_configuration_even_when_the_display_cache_is_warm(self):
        """The edit form is a read-modify-write prefill, so it must not serve the display cache."""
        responses = _catalogue_responses_for_subnets(
            4,
            [],
            shared_networks=[{"name": "prod-net", "description": "Old", "subnet4": []}],
        )

        with stub_kea(responses) as kea:
            self.client.get(reverse("plugins:netbox_kea:server_shared_networks4", args=[self.server.pk]))
            first = self.client.get(self._url())
            second = self.client.get(self._url())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(kea.commands().count("config-get"), 3)

    def test_get_prefills_a_code_only_dns_option_and_an_unrelated_save_keeps_it(self):
        """A DNS option written by code must round-trip through the form."""
        config = _sn_config(
            4,
            "prod-net",
            option_data=[
                {"code": 6, "data": "198.18.0.53"},
                {"name": "domain-name-servers", "space": "vendor-4491", "data": "198.18.0.99"},
            ],
        )
        with _edit_stub(config) as kea:
            response = self.client.get(self._url())
            initial = response.context["form"].initial
            self.assertEqual(initial["dns_servers"], "198.18.0.53")
            self.client.post(self._url(), self._post_data(description="Renamed", dns_servers=initial["dns_servers"]))

        self.assertEqual(
            _written_sn(kea)["option-data"],
            [
                {"name": "domain-name-servers", "space": "vendor-4491", "data": "198.18.0.99"},
                {"code": 6, "data": "198.18.0.53"},
            ],
        )

    def test_unchanged_csv_preserves_raw_data_and_flags(self):
        options = [
            {"code": 6, "data": "198.18.0.53, 198.18.0.54", "csv-format": True},
            {"code": 42, "data": "198.18.0.123, 198.18.0.124", "csv-format": True},
        ]
        with _edit_stub(_sn_config(4, "prod-net", option_data=options)) as kea:
            response = self.client.get(self._url())
            initial = response.context["form"].initial
            post = self.client.post(
                self._url(),
                self._post_data(
                    description="Renamed", dns_servers=initial["dns_servers"], ntp_servers=initial["ntp_servers"]
                ),
            )
        self.assertEqual(post.status_code, 302)
        self.assertEqual(_written_sn(kea)["option-data"], options)

    def test_an_unrelated_save_keeps_a_dns_suppression_entry(self):
        """A never-send entry shows no value in the form, so an empty field must not delete it."""
        config = _sn_config(4, "prod-net", option_data=[{"code": 6, "never-send": True}])
        with _edit_stub(config) as kea:
            response = self.client.get(self._url())
            self.assertEqual(response.context["form"].initial["dns_servers"], "")
            self.client.post(self._url(), self._post_data(description="Renamed"))

        self.assertEqual(_written_sn(kea)["option-data"], [{"code": 6, "never-send": True}])

    def test_an_unrelated_save_keeps_never_send_next_to_a_value(self):
        """The form edits the DNS value only; a delivery flag beside it survives an unchanged save."""
        option = {"code": 6, "data": "198.18.0.53", "never-send": True}
        with _edit_stub(_sn_config(4, "prod-net", option_data=[option])) as kea:
            response = self.client.get(self._url())
            initial = response.context["form"].initial
            self.assertEqual(initial["dns_servers"], "198.18.0.53")
            self.client.post(self._url(), self._post_data(description="Renamed", dns_servers=initial["dns_servers"]))

        self.assertEqual(_written_sn(kea)["option-data"], [option])

    def test_a_binary_dns_entry_stays_out_of_the_form_and_survives_an_unrelated_save(self):
        """The form cannot show hexadecimal data, so the field is empty and the entry is kept."""
        binary = {"code": 6, "data": "C6120035", "csv-format": False}
        with _edit_stub(_sn_config(4, "prod-net", option_data=[binary])) as kea:
            response = self.client.get(self._url())
            self.assertEqual(response.context["form"].initial["dns_servers"], "")
            post = self.client.post(self._url(), self._post_data(description="Renamed"))

        self.assertEqual(post.status_code, 302)
        self.assertEqual(_written_sn(kea)["option-data"], [binary])

    def test_null_option_data_refuses_the_edit_form_and_the_update(self):
        """A network whose option-data failed to parse is not editable through this form."""
        config = _sn_config(4, "prod-net", option_data=None)
        config["arguments"]["Dhcp4"]["shared-networks"][0]["option-data"] = None
        with _edit_stub(config) as kea:
            get = self.client.get(self._url())
            post = self.client.post(self._url(), self._post_data(description="Renamed"))

        self.assertEqual(get.status_code, 302)
        self.assertEqual(post.status_code, 200)
        self.assertContains(post, "Could not reload")
        self.assertNotIn("config-set", kea.commands())

    def test_post_valid_calls_network_update_and_redirects(self):
        """POST with valid data runs the read-modify-write cycle and redirects."""
        with _edit_stub(_sn_config(4, "prod-net")) as kea:
            response = self.client.post(self._url(), self._post_data(description="Updated description"))
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        # config-set proves network_update completed the read-modify-write cycle.
        self.assertIn("config-set", kea.commands())
        self.assertEqual(_written_sn(kea, 4)["description"], "Updated description")

    def test_post_passes_version_4_to_network_update(self):
        """POST must issue the config-set to the dhcp4 service."""
        with _edit_stub(_sn_config(4, "prod-net")) as kea:
            self.client.post(self._url(), self._post_data())
        self.assertEqual(kea.bodies("config-set")[0]["service"], ["dhcp4"])

    def test_post_kea_exception_shows_error_and_redirects(self):
        """config-test failure surfaces a generic error and must not leak raw Kea text."""
        with _edit_stub(_sn_config(4, "prod-net"), **{"config-test": {"result": 1, "text": "config error"}}):
            response = self.client.post(self._url(), self._post_data(), follow=True)
        self.assertEqual(response.status_code, 200)
        self._assert_no_none_pk_redirect(response)
        messages_list = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.ERROR for m in messages_list),
            f"Expected an ERROR message; got: {[(m.level, m.message) for m in messages_list]}",
        )
        # Raw Kea error text must not appear in either rendered response or queued messages
        self.assertNotIn(b"config error", response.content)
        for m in messages_list:
            self.assertNotIn("config error", m.message, f"Raw Kea error text leaked into message: {m.message}")

    def test_post_partial_persist_error_shows_warning(self):
        """config-write failure (PartialPersistError) redirects with a warning (no 500)."""
        with _edit_stub(_sn_config(4, "prod-net"), **{"config-write": {"result": 1, "text": "write failed"}}):
            response = self.client.post(self._url(), self._post_data(), follow=True)
        self.assertEqual(response.status_code, 200)
        messages_list = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.WARNING for m in messages_list),
            f"Expected a WARNING message; got: {[(m.level, m.message) for m in messages_list]}",
        )
        self.assertIn(
            "Change applied but may not survive a Kea restart (config-write failed).",
            [str(message) for message in messages_list],
        )
        self.assertFalse(any("Kea did not confirm the change" in str(message) for message in messages_list))

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url(), {"name": "prod-net"})
        self.assertIn(response.status_code, (302, 403))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork6EditView(_ViewTestBase):
    """Tests for ServerSharedNetwork6EditView — verifies the dhcp6 service."""

    def _url(self, name="prod-net6"):
        return reverse("plugins:netbox_kea:server_shared_network6_edit", args=[self.server.pk, name])

    def test_get_returns_200(self):
        """GET returns 200 for DHCPv6 edit view."""
        with stub_kea({"config-get": _sn_config(6, "prod-net6", option_data=[])}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_calls_network_update_with_version_6(self):
        """POST must issue the config-set to the dhcp6 service."""
        with _edit_stub(_sn_config(6, "prod-net6")) as kea:
            self.client.post(
                self._url(),
                {
                    "name": "prod-net6",
                    "description": "v6 net",
                    "interface": "",
                    "relay_addresses": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertEqual(kea.bodies("config-set")[0]["service"], ["dhcp6"])


# ---------------------------------------------------------------------------
# Tests for shared network POST — option-data preservation
# ---------------------------------------------------------------------------


class TestSharedNetworkEditAmbiguousWrite(_ViewTestBase):
    def test_unconfirmed_config_set_never_claims_the_change_was_applied(self):
        for version in (4, 6):
            for error in (
                requests.ConnectionError("connection refused"),
                requests.Timeout("reply lost"),
                ValueError("bad JSON"),
                [],
                [{}],
                [None],
                [{"result": 0}, {"result": 0}],
                [{"result": False}],
            ):
                with self.subTest(version=version, response=repr(error)):
                    url = reverse(
                        f"plugins:netbox_kea:server_shared_network{version}_edit", args=[self.server.pk, "clients"]
                    )
                    with _edit_stub(_sn_config(version, "clients"), **{"config-set": error}) as kea:
                        response = self.client.post(
                            url,
                            {
                                "name": "clients",
                                "description": "Updated",
                                "interface": "",
                                "relay_addresses": "",
                                "dns_servers": "",
                                "ntp_servers": "",
                            },
                            follow=True,
                        )
                    self.assertEqual(response.status_code, 200)
                    messages = list(django_messages.get_messages(response.wsgi_request))
                    self.assertEqual(
                        [(message.level, str(message)) for message in messages],
                        [
                            (
                                django_messages.WARNING,
                                "Kea did not confirm the change. Check the server configuration before retrying.",
                            )
                        ],
                    )
                    self.assertIn("config-set", kea.commands())
                    self.assertNotIn("config-write", kea.commands())

    def test_malformed_validation_and_persistence_replies_keep_their_phase_meaning(self):
        for version in (4, 6):
            for phase in ("config-test", "config-write"):
                for payload in ([], [{}], [None], [{"result": 2}, {"result": 0}], [{"result": False}]):
                    with self.subTest(version=version, phase=phase, payload=repr(payload)):
                        url = reverse(
                            f"plugins:netbox_kea:server_shared_network{version}_edit", args=[self.server.pk, "clients"]
                        )
                        with _edit_stub(_sn_config(version, "clients"), **{phase: payload}) as kea:
                            response = self.client.post(url, {"name": "clients", "description": "Updated"}, follow=True)
                        self.assertEqual(response.status_code, 200)
                        messages = list(django_messages.get_messages(response.wsgi_request))
                        self.assertFalse(any(message.level == django_messages.SUCCESS for message in messages))
                        if phase == "config-test":
                            self.assertNotIn("config-set", kea.commands())
                            self.assertNotIn("config-write", kea.commands())
                            self.assertTrue(any(message.level == django_messages.ERROR for message in messages))
                        else:
                            self.assertIn("config-set", kea.commands())
                            self.assertEqual(
                                [(message.level, str(message)) for message in messages],
                                [
                                    (
                                        django_messages.WARNING,
                                        "Change applied but may not survive a Kea restart (config-write failed).",
                                    )
                                ],
                            )

    def test_hook_mutation_rejects_malformed_persistence_phase_replies(self):
        for version in (4, 6):
            for phase in ("config-test", "config-write"):
                for payload in ([], [{}], [None], [{"result": 2}, {"result": 0}], [{"result": False}]):
                    with self.subTest(version=version, phase=phase, payload=repr(payload)):
                        url = reverse(f"plugins:netbox_kea:server_shared_network{version}_add", args=[self.server.pk])
                        with _mutate_stub(f"network{version}-add", **{phase: payload}) as kea:
                            response = self.client.post(url, {"name": "clients"}, follow=True)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(f"network{version}-add", kea.commands())
                        messages = list(django_messages.get_messages(response.wsgi_request))
                        self.assertFalse(any(message.level == django_messages.SUCCESS for message in messages))
                        if phase == "config-test":
                            self.assertNotIn("config-write", kea.commands())
                            self.assertTrue(any(message.level == django_messages.ERROR for message in messages))
                        else:
                            self.assertTrue(any(message.level == django_messages.WARNING for message in messages))
                            self.assertTrue(any("created on the live server" in str(message) for message in messages))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkOptionDataPreservation(_ViewTestBase):
    """Verify non-DNS/NTP option-data entries are preserved on shared network save."""

    def _url(self, name="prod-net"):
        return reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, name])

    def test_post_preserves_non_dns_options(self):
        """config-set receives non-DNS/NTP option-data that was fetched from Kea."""
        custom_option = {"name": "vendor-specific", "data": "deadbeef"}
        config = _sn_config(
            4,
            "prod-net",
            option_data=[{"name": "domain-name-servers", "data": "8.8.8.8"}, custom_option],
        )
        with _edit_stub(config) as kea:
            self.client.post(
                self._url(),
                {
                    "name": "prod-net",
                    "description": "",
                    "interface": "",
                    "relay_addresses": "",
                    "dns_servers": "1.1.1.1",
                    "ntp_servers": "",
                },
            )
        options = _written_sn(kea, 4)["option-data"]
        option_names = [o["name"] for o in options]
        # The custom non-DNS option must be preserved.
        self.assertIn("vendor-specific", option_names)
        # The new DNS from the form must also be present.
        self.assertIn("domain-name-servers", option_names)

    def test_post_replaces_dns_servers_not_duplicates(self):
        """Old DNS option from Kea is dropped; only the form-supplied DNS value is written."""
        config = _sn_config(4, "prod-net", option_data=[{"name": "domain-name-servers", "data": "8.8.8.8"}])
        with _edit_stub(config) as kea:
            self.client.post(
                self._url(),
                {
                    "name": "prod-net",
                    "description": "",
                    "interface": "",
                    "relay_addresses": "",
                    "dns_servers": "1.1.1.1",
                    "ntp_servers": "",
                },
            )
        options = _written_sn(kea, 4)["option-data"]
        dns_opts = [o for o in options if o["name"] == "domain-name-servers"]
        # Only one DNS entry must be present (the new value, not the old one).
        self.assertEqual(len(dns_opts), 1)
        self.assertEqual(dns_opts[0]["data"], "1.1.1.1")


# ---------------------------------------------------------------------------
# SharedNetworkEdit fetch-failure paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkEditSnapshotFailures(_ViewTestBase):
    """Server Configuration failure paths for GET display and POST verification."""

    def _url(self, name="prod-net"):
        return reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, name])

    def _post_data(self, **overrides):
        data = {
            "name": "prod-net",
            "description": "x",
            "interface": "",
            "relay_addresses": "",
            "dns_servers": "",
            "ntp_servers": "",
        }
        data.update(overrides)
        return data

    def test_get_redirects_when_network_not_found(self):
        """GET must redirect when config-get returns a config with no matching network."""
        with stub_kea({"config-get": _EMPTY_SN_CONFIG_V4}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_redirects_when_fetch_raises_kea_exception(self):
        """GET must redirect when config-get returns a KeaException (result 1)."""
        with stub_kea({"config-get": {"result": 1, "text": "err"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    def test_get_redirects_when_shared_network_collection_is_incomplete(self):
        """GET must not offer a form that POST refuses."""
        responses = _catalogue_responses_for_subnets(
            4,
            [],
            shared_networks=[{"name": "prod-net", "subnet4": []}, None],
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 302)

    def test_post_aborts_when_reload_returns_empty(self):
        """POST aborts with an error when the verified network is absent."""
        with stub_kea({"config-get": _EMPTY_SN_CONFIG_V4}):
            response = self.client.post(self._url(), self._post_data())
        # Must re-render (not crash) with error message
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not reload")

    def test_post_aborts_when_family_configuration_is_not_an_object(self):
        with stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp4": []}}}) as kea:
            response = self.client.post(self._url(), self._post_data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not reload")
        self.assertNotIn("config-set", kea.commands())

    def test_post_aborts_when_shared_network_collection_is_incomplete(self):
        responses = _catalogue_responses_for_subnets(
            4,
            [],
            shared_networks=[{"name": "prod-net", "subnet4": []}, None],
        )

        with stub_kea(responses) as kea:
            response = self.client.post(self._url(), self._post_data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not reload")
        self.assertNotIn("config-set", kea.commands())

    def test_post_sets_ntp_servers_option(self):
        """POST with ntp_servers populates option-data with an ntp-servers entry."""
        with _edit_stub(_sn_config(4, "prod-net", option_data=[])) as kea:
            self.client.post(
                self._url(),
                {
                    "name": "prod-net",
                    "description": "",
                    "interface": "",
                    "relay_addresses": "",
                    "dns_servers": "",
                    "ntp_servers": "10.0.0.1",
                },
            )
        options = _written_sn(kea, 4)["option-data"]
        ntp_opts = [o for o in options if o.get("name") == "ntp-servers"]
        self.assertEqual(len(ntp_opts), 1)
        self.assertEqual(ntp_opts[0]["data"], "10.0.0.1")

    def test_post_generic_exception_rerenders(self):
        """A transport error during network_update must not crash (no 500)."""
        # Verification succeeds, then the write path raises a transport error.
        stub = {"config-get": queued(_sn_config(4, "prod-net"), requests.ConnectionError("boom"))}
        with stub_kea(stub):
            response = self.client.post(self._url(), self._post_data())
        self.assertIn(response.status_code, (200, 302))

    def test_post_invalid_form_rerenders(self):
        """POST with missing required field must re-render the form (200, no Kea)."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"description": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])


# ---------------------------------------------------------------------------
# Shared network list with DHCP disabled
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkListEdgeCases(_ViewTestBase):
    """Shared Network list when DHCP is disabled."""

    def test_dhcp4_disabled_redirects(self):
        """When dhcp4 disabled, the v4 list view redirects to the v6 route (no Kea)."""
        server_no4 = _make_db_server(name="no-dhcp4", ca_url="https://kea.example.com", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[server_no4.pk])
        with stub_kea({}) as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands(), [])


# ---------------------------------------------------------------------------
# SharedNetworkAdd/Delete — generic exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkCRUDGenericException(_ViewTestBase):
    """A transport error on add/delete shows a generic internal-error message."""

    def test_add_generic_exception_shows_error(self):
        """Transport exception on network4-add redirects with a generic error."""
        url = reverse("plugins:netbox_kea:server_shared_network4_add", args=[self.server.pk])
        stub = {
            "network4-add": requests.ConnectionError("connection reset"),
            "config-get": _EMPTY_SN_CONFIG_V4,  # follow=True lands on the list view
        }
        with stub_kea(stub):
            response = self.client.post(url, {"name": "new-net"}, follow=True)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("internal error" in m.lower() for m in msgs))

    def test_delete_generic_exception_shows_error(self):
        """Transport exception on network4-del redirects with a generic error."""
        url = reverse("plugins:netbox_kea:server_shared_network4_delete", args=[self.server.pk, "old-net"])
        stub = {
            "network4-del": requests.ConnectionError("timeout"),
            "config-get": _EMPTY_SN_CONFIG_V4,  # follow=True lands on the list view
        }
        with stub_kea(stub):
            response = self.client.post(url, {}, follow=True)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("internal error" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Shared network edit — option parsing in GET
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkEditOptionParsing(_ViewTestBase):
    """GET populates dns_servers/ntp_servers from the fetched option-data."""

    def test_get_populates_dns_and_ntp_from_option_data(self):
        config = _sn_config(
            4,
            "prod-net",
            option_data=[
                {"name": "domain-name-servers", "data": "8.8.8.8"},
                {"name": "ntp-servers", "data": "192.0.2.1"},
            ],
        )
        url = reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, "prod-net"])
        with stub_kea({"config-get": config}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "8.8.8.8")
        self.assertContains(response, "192.0.2.1")


# ---------------------------------------------------------------------------
# Shared networks tab disabled — defensive get_children guard
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworksTabDisabled(_ViewTestBase):
    """server.dhcp4=False → get_children returns [] immediately (before any client)."""

    def test_dhcp4_disabled_returns_empty_children(self):
        """dhcp4=False → get_children returns [] immediately (defensive guard, no Kea)."""
        from django.test import RequestFactory

        from netbox_kea.views.shared_networks import ServerSharedNetworks4View

        server = _make_db_server(name="no-dhcp4-children", dhcp4=False, dhcp6=True)
        view = ServerSharedNetworks4View()
        request = RequestFactory().get("/")
        request.user = self.user
        result = view.get_children(request, server)
        self.assertEqual(result, [])
