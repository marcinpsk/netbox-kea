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
* **add** (``config_write.add_shared_network``): ``network{v}-get`` (the read
  before the change), ``network{v}-add``, then the persist step (``config-get`` →
  ``config-test`` → ``config-write``; ``persist_config`` defaults True).
* **delete** (``config_write.delete_shared_network``): ``network{v}-get``,
  ``network{v}-del``, then the same persist step.
* **edit** (``config_write.edit_shared_network``): ``config-get``, the edit in
  place, ``config-test``, ``config-set``, then the persist step. The config-set
  body proves the version, network, and DHCP Options end to end.

Error paths are driven through the real client: a failure result is a payload
with a non-zero ``result``, and a transport error is a ``requests`` exception
instance raised at the HTTP boundary. ``test_config_write`` covers the outcomes
in depth; the tests here cover one message per outcome and per rejection.
"""

import copy

import requests
from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse

from .kea_stub import (
    _catalogue_responses_for_subnets,
    _network_absent,
    _network_present,
    _refused_connection,
    queued,
    stub_kea,
)
from .utils import _PLUGINS_CONFIG, _make_db_server, _page_data, _ReadModifyWriteMessages, _ViewTestBase

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
            "user-context": {"comment": "Alpha test network"},
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
    network: dict = {"name": name, subnet_key: list(subnets or [])}
    if description:
        network["user-context"] = {"comment": description}
    if option_data is not None:
        network["option-data"] = option_data
    return _catalogue_responses_for_subnets(version, [], shared_networks=[network])["config-get"]


def _change_stub(version, command, reply=_CONFIG_OK, *, before, after=None, **overrides):
    """Stub one Shared Network add or delete: the read before it, the change, a check read, and persist."""
    base = {
        f"network{version}-get": before if after is None else queued(before, after),
        f"network{version}-{command}": reply,
        "config-get": _catalogue_responses_for_subnets(version, [])["config-get"],
        "config-test": _CONFIG_OK,
        "config-write": _CONFIG_OK,
    }
    base.update(overrides)
    return stub_kea(base)


def _add_stub(name, reply=_CONFIG_OK, *, version=4, **overrides):
    """Stub an add of *name*, which the read before the change does not find."""
    return _change_stub(version, "add", reply, before=_network_absent(name), **overrides)


def _delete_stub(name, reply=_CONFIG_OK, *, version=4, **overrides):
    """Stub a delete of *name*, which the read before the change finds."""
    return _change_stub(version, "del", reply, before=_network_present(version, name), **overrides)


def _edit_stub(config_get, **overrides):
    """Stub the shared-network edit read-modify-write chain.

    *config_get* is deep-copied, so the edit in place never changes the module-level fixture.
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
    """Return the shared-network dict that the edit pushed back via config-set."""
    bodies = kea.bodies("config-set")
    assert bodies, "config-set was never issued (the edit did not complete)"
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
        self.assertContains(response, "Kea configuration facts are unavailable.")
        self.assertTrue(any(message.level == django_messages.ERROR for message in response.context["messages"]))

    def test_unreachable_server_shows_diagnostic_and_keeps_page_available(self):
        with stub_kea({"config-get": requests.ConnectionError("unreachable")}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea configuration facts are unavailable.")
        self.assertTrue(any(message.level == django_messages.ERROR for message in response.context["messages"]))

    def test_non_object_family_configuration_shows_diagnostic_instead_of_raising(self):
        with stub_kea({"config-get": {"result": 0, "arguments": {"Dhcp4": []}}}):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea did not return a Dhcp4 configuration object.")

    def test_empty_network_appears_in_both_lists_and_subnet_add_choices(self):
        network = {"name": "empty-clients", "user-context": {"comment": "No members yet"}, "subnet4": []}
        responses = _catalogue_responses_for_subnets(4, [], shared_networks=[network])

        with stub_kea(responses) as kea:
            server_list = self.client.get(self._url())
            combined_list = self.client.get(
                reverse("plugins:netbox_kea:combined_shared_networks4"), {"server": self.server.pk}
            )
            subnet_add = self.client.get(reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk]))

        self.assertContains(server_list, "empty-clients")
        self.assertContains(combined_list, "empty-clients")
        self.assertIn(
            ("empty-clients", "empty-clients"), subnet_add.context["form"].fields["shared_network"].widget.choices
        )
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

    def test_non_object_user_context_shows_the_network_without_a_description(self):
        responses = _catalogue_responses_for_subnets(
            4,
            [],
            shared_networks=[{"name": "clients", "user-context": "legacy note", "subnet4": []}],
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "clients")
        self.assertNotContains(response, "legacy note")
        self.assertContains(response, "Kea returned an invalid user-context setting.")

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
        with _add_stub("net-prod") as kea:
            response = self.client.post(self._url(), {"name": "net-prod"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", "config-get", "config-test", "config-write"])

    def test_post_calls_network_add_with_correct_version(self):
        """POST must send network4-add to the dhcp4 service with the network name."""
        with _add_stub("net-prod") as kea:
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
        with _add_stub("net6-prod", version=6) as kea:
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
        with _delete_stub("net-alpha") as kea:
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertEqual(kea.commands(), ["network4-get", "network4-del", "config-get", "config-test", "config-write"])

    def test_post_passes_correct_version_and_name(self):
        """POST must send network4-del to dhcp4 with the correct network name."""
        with _delete_stub("net-alpha") as kea:
            self.client.post(self._url(name="net-alpha"))
        body = kea.bodies("network4-del")[0]
        self.assertEqual(body["service"], ["dhcp4"])
        self.assertEqual(body["arguments"], {"name": "net-alpha"})

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
        with _delete_stub("net-beta", version=6) as kea:
            self.client.post(self._url(name="net-beta"))
        body = kea.bodies("network6-del")[0]
        self.assertEqual(body["service"], ["dhcp6"])


class _SharedNetworkChangeMessages:
    """One message per Configuration Change outcome and per rejection, for a Shared Network add or delete.

    Subclasses name the view, the change, and the read results that decide each case.
    """

    change: str
    confirmed: str
    not_sent: str

    def _url(self):
        raise NotImplementedError

    def _stub(self, reply=_CONFIG_OK, **overrides):
        raise NotImplementedError

    def _unchanged(self):
        """The check read that proves the failed change is not live."""
        raise NotImplementedError

    def _refusing(self):
        """The read before the change that makes the operation refuse to send it."""
        raise NotImplementedError

    def _post(self, stub, **server_fields):
        for field, value in server_fields.items():
            setattr(self.server, field, value)
        self.server.save()
        with stub as kea:
            response = self.client.post(self._url(), {"name": "net-prod"})
        self.assertEqual(response.status_code, 302)
        messages = [(m.level, str(m)) for m in django_messages.get_messages(response.wsgi_request)]
        return messages, kea.commands()

    def test_applied_and_persisted(self):
        messages, commands = self._post(self._stub())
        self.assertEqual(messages, [(django_messages.SUCCESS, self.confirmed)])
        self.assertEqual(commands, ["network4-get", self.change, "config-get", "config-test", "config-write"])

    def test_applied_and_persistence_not_requested(self):
        messages, commands = self._post(self._stub(), persist_config=False)
        self.assertEqual(messages, [(django_messages.SUCCESS, self.confirmed)])
        self.assertEqual(commands, ["network4-get", self.change])

    def test_applied_and_persistence_failed_shows_the_restart_warning(self):
        failure = {"result": 1, "text": "Unable to open file for writing"}
        messages, commands = self._post(self._stub(**{"config-write": failure}))
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        f"{self.confirmed} It is live, but it may not survive a Kea restart, because Kea did not save "
                        "it to disk. config-write failed: Unable to open file for writing"
                    ),
                )
            ],
        )
        self.assertEqual(commands, ["network4-get", self.change, "config-get", "config-test", "config-write"])

    def test_unknown_and_persisted(self):
        messages, commands = self._post(self._stub(requests.ReadTimeout("read timed out")))
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        "Kea did not confirm the change. Check the server configuration before retrying. "
                        "Kea's reply to the change was lost or unreadable."
                    ),
                )
            ],
        )
        self.assertEqual(commands, ["network4-get", self.change, "config-get", "config-test", "config-write"])

    def test_unknown_and_persistence_failed_never_claims_the_change_is_live(self):
        stub = self._stub(requests.ReadTimeout("read timed out"), **{"config-write": requests.ReadTimeout()})
        messages, _ = self._post(stub)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        "Kea did not confirm the change. Check the server configuration before retrying. "
                        "Kea also could not save its running configuration to disk. "
                        "Kea's reply to the change was lost or unreadable. "
                        "The reply to config-write was lost or unreadable."
                    ),
                )
            ],
        )

    def test_unknown_and_persistence_not_requested(self):
        messages, commands = self._post(self._stub(requests.ReadTimeout("read timed out")), persist_config=False)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        "Kea did not confirm the change. Check the server configuration before retrying. "
                        "Kea's reply to the change was lost or unreadable."
                    ),
                )
            ],
        )
        self.assertEqual(commands, ["network4-get", self.change])

    def test_kea_rejected(self):
        failure = {"result": 1, "text": "invalid shared network"}
        messages, commands = self._post(self._stub(failure, after=self._unchanged()))
        self.assertEqual(
            messages, [(django_messages.ERROR, "Kea rejected the change. Kea replied: invalid shared network")]
        )
        self.assertEqual(commands, ["network4-get", self.change, "network4-get"])

    def test_not_sent_after_the_read_before_the_change(self):
        messages, commands = self._post(self._stub(**{"network4-get": self._refusing()}))
        self.assertEqual(messages, [(django_messages.ERROR, f"The change was not sent to Kea. {self.not_sent}")])
        self.assertEqual(commands, ["network4-get"])

    def test_not_sent_after_a_refused_connection(self):
        messages, commands = self._post(self._stub(_refused_connection()))
        self.assertEqual(
            messages, [(django_messages.ERROR, "The change was not sent to Kea. Kea could not be reached.")]
        )
        self.assertEqual(commands, ["network4-get", self.change])

    def test_invalid_client_configuration(self):
        messages, commands = self._post(self._stub(), client_cert_path="/etc/kea/client.pem")
        self.assertEqual(
            messages,
            [
                (
                    django_messages.ERROR,
                    (
                        "The change was not sent to Kea, because the Server settings are not valid. "
                        "NetBox could not build a Kea client from the Server connection settings."
                    ),
                )
            ],
        )
        self.assertEqual(commands, [])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkAddMessages(_SharedNetworkChangeMessages, _ViewTestBase):
    change = "network4-add"
    confirmed = "Shared network 'net-prod' created."
    not_sent = "Shared Network 'net-prod' already exists."

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_network4_add", args=[self.server.pk])

    def _stub(self, reply=_CONFIG_OK, **overrides):
        return _add_stub("net-prod", reply, **overrides)

    def _unchanged(self):
        return _network_absent("net-prod")

    def _refusing(self):
        return _network_present(4, "net-prod")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkDeleteMessages(_SharedNetworkChangeMessages, _ViewTestBase):
    change = "network4-del"
    confirmed = "Shared network 'net-prod' deleted."
    not_sent = "Shared Network 'net-prod' not found."

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_network4_delete", args=[self.server.pk, "net-prod"])

    def _stub(self, reply=_CONFIG_OK, **overrides):
        return _delete_stub("net-prod", reply, **overrides)

    def _unchanged(self):
        return _network_present(4, "net-prod")

    def _refusing(self):
        return _network_absent("net-prod")


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

    def test_get_help_names_each_command_of_the_change(self):
        with stub_kea({"config-get": _sn_config(4, "prod-net", option_data=[])}):
            response = self.client.get(self._url())
        self._assert_help_names_the_read_modify_write(response)

    def test_get_reads_the_live_configuration_even_when_the_display_cache_is_warm(self):
        """The edit form is a read-modify-write prefill, so it must not serve the display cache."""
        responses = _catalogue_responses_for_subnets(
            4,
            [],
            shared_networks=[{"name": "prod-net", "user-context": {"comment": "Old"}, "subnet4": []}],
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
            self.assertEqual(response.context["form"].initial["dns_servers"], "198.18.0.53")
            self.client.post(self._url(), {**_page_data(response), "description": "Renamed"})

        self.assertEqual(
            _written_sn(kea)["option-data"],
            [
                {"name": "domain-name-servers", "space": "vendor-4491", "data": "198.18.0.99"},
                {"code": 6, "data": "198.18.0.53"},
            ],
        )

    def test_other_family_option_names_stay_out_of_the_form_and_survive_a_save(self):
        options = [{"name": "dns-servers", "data": "192.0.2.53"}, {"name": "sntp-servers", "data": "192.0.2.123"}]
        with _edit_stub(_sn_config(4, "prod-net", option_data=options)) as kea:
            initial = self.client.get(self._url()).context["form"].initial
            self.assertEqual((initial["dns_servers"], initial["ntp_servers"]), ("", ""))
            post = self.client.post(self._url(), self._post_data(**initial))
        self.assertEqual(post.status_code, 302)
        self.assertEqual(_written_sn(kea)["option-data"], options)

    def test_unchanged_csv_preserves_raw_data_and_flags(self):
        options = [
            {"code": 6, "data": "198.18.0.53, 198.18.0.54", "csv-format": True},
            {"code": 42, "data": "198.18.0.123, 198.18.0.124", "csv-format": True},
        ]
        with _edit_stub(_sn_config(4, "prod-net", option_data=options)) as kea:
            post = self.client.post(self._url(), {**_page_data(self.client.get(self._url())), "description": "Renamed"})
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
            self.assertEqual(response.context["form"].initial["dns_servers"], "198.18.0.53")
            self.client.post(self._url(), {**_page_data(response), "description": "Renamed"})

        self.assertEqual(_written_sn(kea)["option-data"], [option])

    def test_displayed_suppressed_addresses_can_be_cleared(self):
        options = [
            {"code": 6, "data": "198.18.0.53", "never-send": True, "csv-format": True},
            {"code": 42, "data": "198.18.0.123", "never-send": True, "always-send": False},
        ]
        with _edit_stub(_sn_config(4, "prod-net", option_data=options)) as kea:
            get = self.client.get(self._url())
            self.assertEqual(get.context["form"].initial["dns_servers"], "198.18.0.53")
            self.assertEqual(get.context["form"].initial["ntp_servers"], "198.18.0.123")
            post = self.client.post(self._url(), {**_page_data(get), "dns_servers": "", "ntp_servers": ""})
        self.assertEqual(post.status_code, 302)
        self.assertEqual(_written_sn(kea)["option-data"], [])

    def test_empty_suppression_data_survives_an_unchanged_blank_save(self):
        option = {"code": 6, "data": "", "never-send": True}
        with _edit_stub(_sn_config(4, "prod-net", option_data=[option])) as kea:
            get = self.client.get(self._url())
            self.assertEqual(get.context["form"].initial["dns_servers"], "")
            self.client.post(self._url(), self._post_data())
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
        self.assertEqual(get.status_code, 302)
        self._fresh_client()
        with _edit_stub(config) as kea:
            post = self.client.post(self._url(), self._post_data(description="Renamed"))
        self.assertEqual(post.status_code, 302)
        self.assertEqual(
            [str(m) for m in django_messages.get_messages(post.wsgi_request)],
            ["The change was not sent to Kea. Kea returned a configuration that NetBox cannot edit safely."],
        )
        self.assertEqual(kea.commands(), ["config-get"])

    def test_a_dns_value_that_is_not_an_address_list_refuses_the_edit_form(self):
        config = _sn_config(4, "prod-net", option_data=[{"code": 6, "data": "ns.example.org"}])
        with _edit_stub(config):
            get = self.client.get(self._url())
        self.assertEqual(get.status_code, 302)
        self.assertEqual(
            [str(m) for m in django_messages.get_messages(get.wsgi_request)],
            ["Shared network 'prod-net' not found or could not be retrieved."],
        )

    def test_post_valid_runs_the_read_modify_write_and_redirects(self):
        """POST with valid data reads the configuration once, under the lock, and redirects."""
        with _edit_stub(_sn_config(4, "prod-net")) as kea:
            response = self.client.post(self._url(), self._post_data(description="Updated description"))
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertEqual(
            kea.commands(), ["config-get", "config-test", "config-set", "config-get", "config-test", "config-write"]
        )
        written = _written_sn(kea, 4)
        self.assertEqual(written["user-context"], {"comment": "Updated description"})
        self.assertNotIn("description", written)

    def test_the_description_is_the_kea_comment_and_other_user_context_survives(self):
        config = _sn_config(4, "prod-net")
        config["arguments"]["Dhcp4"]["shared-networks"][0]["user-context"] = {"comment": "Old", "owner": "noc"}
        for description, expected in (("New", {"comment": "New", "owner": "noc"}), ("", {"owner": "noc"})):
            with self.subTest(description=description), _edit_stub(config) as kea:
                page = self.client.get(self._url())
                self.assertEqual(page.context["form"].initial["description"], "Old")
                self.client.post(self._url(), {**_page_data(page), "description": description})
            self.assertEqual(_written_sn(kea, 4)["user-context"], expected)

    def test_a_structured_comment_stays_out_of_the_form_until_a_description_replaces_it(self):
        config = _sn_config(4, "prod-net")
        config["arguments"]["Dhcp4"]["shared-networks"][0]["user-context"] = {"comment": ["one", "two"]}
        for description, expected in (("", {"comment": ["one", "two"]}), ("New", {"comment": "New"})):
            with self.subTest(description=description), _edit_stub(config) as kea:
                response = self.client.get(self._url())
                self.assertEqual(response.context["form"].initial["description"], "")
                self.client.post(self._url(), self._post_data(description=description))
            self.assertEqual(_written_sn(kea, 4)["user-context"], expected)

    def test_a_long_comment_does_not_block_an_unrelated_edit(self):
        comment = "x" * 300
        with _edit_stub(_sn_config(4, "prod-net", description=comment)) as kea:
            post = self.client.post(self._url(), {**_page_data(self.client.get(self._url())), "interface": "eth1"})
        self.assertEqual(post.status_code, 302)
        self.assertEqual(_written_sn(kea, 4)["user-context"], {"comment": comment})

    def test_an_unrelated_edit_keeps_a_multiline_comment_the_browser_flattened(self):
        comment = " First line\nSecond line "
        with _edit_stub(_sn_config(4, "prod-net", description=comment)) as kea:
            page = _page_data(self.client.get(self._url()))
            # A text input drops line breaks; Django strips the rest.
            post = self.client.post(self._url(), {**page, "description": "First lineSecond line", "interface": "eth1"})
        self.assertEqual(post.status_code, 302)
        self.assertEqual(_written_sn(kea, 4)["user-context"], {"comment": comment})

    def test_clearing_the_only_comment_removes_the_user_context(self):
        with _edit_stub(_sn_config(4, "prod-net", description="Old")) as kea:
            self.client.post(self._url(), {**_page_data(self.client.get(self._url())), "description": ""})
        self.assertNotIn("user-context", _written_sn(kea, 4))

    def test_post_sends_the_config_set_to_dhcp4(self):
        """POST must issue the config-set to the dhcp4 service."""
        with _edit_stub(_sn_config(4, "prod-net")) as kea:
            self.client.post(self._url(), self._post_data())
        self.assertEqual(kea.bodies("config-set")[0]["service"], ["dhcp4"])

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

    def test_post_sends_the_config_set_to_dhcp6(self):
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

    def test_other_family_option_names_stay_out_of_the_form_and_survive_a_save(self):
        options = [
            {"name": "domain-name-servers", "data": "2001:db8::53"},
            {"name": "ntp-servers", "data": "2001:db8::123"},
        ]
        with _edit_stub(_sn_config(6, "prod-net6", option_data=options)) as kea:
            initial = self.client.get(self._url()).context["form"].initial
            self.assertEqual((initial["dns_servers"], initial["ntp_servers"]), ("", ""))
            post = self.client.post(self._url(), {"interface": "", "relay_addresses": "", **initial})
        self.assertEqual(post.status_code, 302)
        self.assertEqual(_written_sn(kea, 6)["option-data"], options)


class _RunningConfiguration:
    """A daemon whose config-get returns the running configuration and whose config-set replaces it."""

    def __init__(self, config_get: dict) -> None:
        self.config_get = copy.deepcopy(config_get)

    def responses(self) -> dict:
        return {
            "config-get": lambda _body: copy.deepcopy(self.config_get),
            "config-test": _CONFIG_OK,
            "config-set": self._set,
            "config-write": _CONFIG_OK,
        }

    def _set(self, body: dict) -> dict:
        self.config_get = {"result": 0, "arguments": {**copy.deepcopy(body["arguments"]), "hash": "set"}}
        return _CONFIG_OK

    def network(self, version: int) -> dict:
        return self.config_get["arguments"][f"Dhcp{version}"]["shared-networks"][0]


_DNS_OPTION = {
    4: ("domain-name-servers", "192.0.2.53", "192.0.2.54"),
    6: ("dns-servers", "2001:db8::53", "2001:db8::54"),
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkEditStaleValues(_ViewTestBase):
    """Two operators load the same edit page, and each one changes a different field."""

    def test_a_save_from_a_page_that_shows_a_changed_value_is_not_sent(self):
        for version in (4, 6):
            name, old, new = _DNS_OPTION[version]
            daemon = _RunningConfiguration(
                _sn_config(version, "prod-net", description="Old", option_data=[{"name": name, "data": old}])
            )
            url = reverse(f"plugins:netbox_kea:server_shared_network{version}_edit", args=[self.server.pk, "prod-net"])
            with self.subTest(version=version), stub_kea(daemon.responses()) as kea:
                self._fresh_client()
                first = _page_data(self.client.get(url))
                second = _page_data(self.client.get(url))
                self.client.post(url, {**first, "description": "New"})
                response = self.client.post(url, {**second, "dns_servers": new})
                # The second page still shows the old description, so its save must not write it back.
                self.assertEqual(daemon.network(version)["user-context"], {"comment": "New"})
                self.assertEqual(daemon.network(version)["option-data"], [{"name": name, "data": old}])
                self.assertEqual(len(kea.bodies("config-set")), 1)
                self.assertEqual(
                    [(m.level, str(m)) for m in django_messages.get_messages(response.wsgi_request)][-1],
                    (
                        django_messages.ERROR,
                        (
                            "The change was not sent to Kea. Shared Network 'prod-net' changed in Kea. "
                            "Reload the page and try again."
                        ),
                    ),
                )


# ---------------------------------------------------------------------------
# Tests for shared network POST — option-data preservation
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkEditMessages(_ReadModifyWriteMessages):
    """One POST per Configuration Change outcome and per rejection, for the Shared Network edit."""

    def test_edit(self):
        self._assert_one_message_per_case(
            reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, "prod-net"]),
            {"name": "prod-net", "description": "x"},
            {"config-get": _sn_config(4, "prod-net")},
            "Shared network 'prod-net' updated.",
        )


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
                                (
                                    "Kea did not confirm the change. Check the server configuration before retrying. "
                                    "Kea's reply to the change was lost or unreadable."
                                ),
                            )
                        ],
                    )
                    # config-write saves what Kea runs, whether the change applied or not.
                    self.assertEqual(
                        kea.commands()[:6],
                        ["config-get", "config-test", "config-set", "config-get", "config-test", "config-write"],
                    )

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
                                        (
                                            "Shared network 'clients' updated. It is live, but it may not survive "
                                            "a Kea restart, because Kea did not save it to disk. "
                                            "The reply to config-write was lost or unreadable."
                                        ),
                                    )
                                ],
                            )

    def test_hook_mutation_rejects_malformed_persistence_phase_replies(self):
        for version in (4, 6):
            for phase in ("config-test", "config-write"):
                for payload in ([], [{}], [None], [{"result": 2}, {"result": 0}], [{"result": False}]):
                    with self.subTest(version=version, phase=phase, payload=repr(payload)):
                        url = reverse(f"plugins:netbox_kea:server_shared_network{version}_add", args=[self.server.pk])
                        with _add_stub("clients", version=version, **{phase: payload}) as kea:
                            response = self.client.post(url, {"name": "clients"}, follow=True)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(f"network{version}-add", kea.commands())
                        messages = list(django_messages.get_messages(response.wsgi_request))
                        self.assertFalse(any(message.level == django_messages.SUCCESS for message in messages))
                        # The add is applied in both phases, so the view warns and never reports a failure.
                        if phase == "config-test":
                            self.assertNotIn("config-write", kea.commands())
                        self.assertFalse(any(message.level == django_messages.ERROR for message in messages))
                        self.assertTrue(any(message.level == django_messages.WARNING for message in messages))
                        self.assertTrue(any("may not survive a Kea restart" in str(message) for message in messages))


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
            self.client.post(self._url(), {**_page_data(self.client.get(self._url())), "dns_servers": "1.1.1.1"})
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
            self.client.post(self._url(), {**_page_data(self.client.get(self._url())), "dns_servers": "1.1.1.1"})
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

    def test_post_is_not_sent_when_the_configuration_cannot_be_edited(self):
        """The edit reads the network under the lock; a missing network or a malformed configuration sends nothing."""
        incomplete = _catalogue_responses_for_subnets(
            4, [], shared_networks=[{"name": "prod-net", "subnet4": []}, None]
        )
        cases = (
            (_EMPTY_SN_CONFIG_V4, "Shared Network 'prod-net' not found."),
            (
                {"result": 0, "arguments": {"Dhcp4": []}},
                "Kea did not return a usable reply to the read before the change.",
            ),
            (incomplete["config-get"], "Kea returned a configuration that NetBox cannot edit safely."),
            (requests.ConnectionError("boom"), "Kea did not return a usable reply to the read before the change."),
        )
        for config_get, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic, config_get=repr(config_get)):
                self._fresh_client()
                with stub_kea({"config-get": config_get}) as kea:
                    response = self.client.post(self._url(), self._post_data())
                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    [(m.level, str(m)) for m in django_messages.get_messages(response.wsgi_request)],
                    [(django_messages.ERROR, f"The change was not sent to Kea. {diagnostic}")],
                )
                self.assertEqual(kea.commands(), ["config-get"])

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
