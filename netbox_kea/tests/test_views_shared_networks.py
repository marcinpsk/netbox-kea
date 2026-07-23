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
* **edit** (``network_update``): the POST first reloads the network via
  ``config-get`` (``_fetch_network``), then ``network_update`` runs a
  read-modify-write cycle ``config-get`` → ``config-test`` → ``config-set`` →
  ``config-write``. There is no free ``network{v}-update`` hook, so the option
  payload is asserted on the **config-set body** (``_written_sn``), which proves
  the version, network, and option-data end to end.

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

from .kea_stub import queued, stub_kea
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase

_CONFIG_OK = {"result": 0}

# ---------------------------------------------------------------------------
# config-get fixtures for the shared-networks LIST views
# ---------------------------------------------------------------------------

_SHARED_NETWORKS_CONFIG_V4 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [{"id": 1, "subnet": "192.168.0.0/24"}],
                "shared-networks": [
                    {
                        "name": "net-alpha",
                        "description": "Alpha test network",
                        "subnet4": [
                            {"id": 10, "subnet": "10.0.0.0/24"},
                            {"id": 11, "subnet": "10.0.1.0/24"},
                        ],
                    }
                ],
            }
        },
    }
]

_SHARED_NETWORKS_CONFIG_V6 = [
    {
        "result": 0,
        "arguments": {
            "Dhcp6": {
                "subnet6": [],
                "shared-networks": [
                    {
                        "name": "net-beta",
                        "description": "",
                        "subnet6": [
                            {"id": 20, "subnet": "2001:db8::/48"},
                        ],
                    }
                ],
            }
        },
    }
]

# A config with an empty shared-networks list (used by not-found / abort paths).
_EMPTY_SN_CONFIG_V4 = [{"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}]


# ---------------------------------------------------------------------------
# Stub builders (real KeaClient + HTTP-boundary stub)
# ---------------------------------------------------------------------------


def _sn_config(version=4, name="prod-net", description="", option_data=None, subnets=None):
    """config-get payload exposing a single shared network under ``Dhcp{version}``."""
    subnet_key = f"subnet{version}"
    network: dict = {"name": name, "description": description, subnet_key: list(subnets or [])}
    if option_data is not None:
        network["option-data"] = option_data
    return [{"result": 0, "arguments": {f"Dhcp{version}": {"shared-networks": [network], subnet_key: []}}}]


def _mutate_stub(command, response=_CONFIG_OK, **overrides):
    """Stub a ``network{v}-add``/``-del`` mutation plus its ``_persist_config`` chain.

    ``network_add``/``network_del`` issue the mutation command then
    ``_persist_config`` (``config-get`` → ``config-test`` → ``config-write``,
    because ``persist_config`` defaults True). When *response* carries an error
    (or is an exception), the mutation raises before persistence, so the persist
    registrations simply go unused.
    """
    base = {
        command: response,
        "config-get": [{"result": 0, "arguments": {}}],
        "config-test": _CONFIG_OK,
        "config-write": _CONFIG_OK,
    }
    base.update(overrides)
    return stub_kea(base)


def _edit_stub(config_get, **overrides):
    """Stub the shared-network edit read-modify-write chain.

    The edit POST reloads the network via ``_fetch_network`` (``config-get``),
    then ``network_update`` runs ``config-get`` → ``config-test`` → ``config-set``
    → ``config-write``. *config_get* is deep-copied because ``network_update``
    mutates the fetched config in place; assert on the resulting config-set body
    via :func:`_written_sn`.
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
class TestSharedNetworkEditFetchFailures(_ViewTestBase):
    """_fetch_network failure paths: GET redirect + POST abort."""

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

    def test_post_aborts_when_reload_returns_empty(self):
        """POST aborts with error when _fetch_network (reload before write) returns empty."""
        # POST path reloads via _fetch_network; an empty shared-networks list (prod-net
        # absent) triggers the abort path before any mutation.
        with stub_kea({"config-get": _EMPTY_SN_CONFIG_V4}):
            response = self.client.post(self._url(), self._post_data())
        # Must re-render (not crash) with error message
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not reload")

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
        # _fetch_network's config-get succeeds; network_update's config-get then raises a
        # transport error, exercising the view's (RequestException, ValueError) branch.
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
# Shared network list — dhcp disabled + null config
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkListEdgeCases(_ViewTestBase):
    """Shared network list when dhcp disabled or config-get returns null arguments."""

    def test_dhcp4_disabled_redirects(self):
        """When dhcp4 disabled, the v4 list view redirects to the v6 route (no Kea)."""
        server_no4 = _make_db_server(name="no-dhcp4", ca_url="https://kea.example.com", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[server_no4.pk])
        with stub_kea({}) as kea:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands(), [])

    def test_null_config_returns_empty(self):
        """get_children returns [] when config-get returns null arguments."""
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[self.server.pk])
        with stub_kea({"config-get": [{"result": 0, "arguments": None}]}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


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

        from netbox_kea.views import ServerSharedNetworks4View

        server = _make_db_server(name="no-dhcp4-children", dhcp4=False, dhcp6=True)
        view = ServerSharedNetworks4View()
        request = RequestFactory().get("/")
        request.user = self.user
        result = view.get_children(request, server)
        self.assertEqual(result, [])
