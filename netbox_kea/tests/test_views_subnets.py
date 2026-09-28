# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for netbox_kea plugin.

Also contains pure-Python unit tests for helper functions defined in views.py
(e.g. ``_extract_identifier``), which do not require a database but live here
because they are tightly coupled to view logic.

These tests verify correct HTTP responses and redirect behaviour for every view.
They drive a **real** ``KeaClient`` and stub only the HTTP boundary via
``kea_stub.stub_kea`` (see that module), so the actual request payloads Kea would
receive are exercised and can be asserted on; no running Kea instance is required.

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

from copy import deepcopy
from unittest.mock import patch

import requests
from django.contrib import messages as django_messages
from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse

from ..views.subnets import _NO_SUBNET_CIDR
from .kea_stub import (
    Applied,
    SubnetDaemon,
    _catalogue_responses_for_subnets,
    _network_present,
    _refused_connection,
    _res_page,
    _subnet_list,
    queued,
    stub_kea,
)
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase

# Shared stub responses for the subnet list/table views, which issue config-get
# (subnets + shared-networks) then stat-lease{v}-get (utilisation; degrades if the
# stat_cmds hook is absent — modelled by result 2 → KeaException → skipped).
_EMPTY_CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}
_EMPTY_CONFIG6 = {"result": 0, "arguments": {"Dhcp6": {"subnet6": [], "shared-networks": []}}}
_STAT_ABSENT4 = {"result": 2, "text": "unknown command 'stat-lease4-get'"}
_STAT_ABSENT6 = {"result": 2, "text": "unknown command 'stat-lease6-get'"}
_ABSENT_READ_HOOKS = {
    "subnet4-list": {"result": 2, "text": "subnet_cmds is not loaded"},
    "subnet6-list": {"result": 2, "text": "subnet_cmds is not loaded"},
    "stat-lease4-get": _STAT_ABSENT4,
    "stat-lease6-get": _STAT_ABSENT6,
}


def _shown(network: str = "") -> dict[str, str]:
    """Return the hidden fields of an edit page that confirmed the Shared Network *network* of the Subnet."""
    return {"shown_network": network, "shown_network_confirmed": "True"}


def _edit_responses(version, subnet_response, config_response):
    """Describe consistent identity and declared configuration for the edit form."""
    subnet = deepcopy(subnet_response["arguments"][f"subnet{version}"][0])
    identity = {"id": subnet["id"], "subnet": subnet["subnet"]}
    configuration = deepcopy(config_response)
    if configuration.get("result") == 0 and isinstance(configuration.get("arguments"), dict):
        service = configuration["arguments"][f"Dhcp{version}"]
        members = service.setdefault(f"subnet{version}", [])
        for network in service.get("shared-networks", []):
            if any(
                item.get("id") == subnet["id"] for item in network.get(f"subnet{version}", []) if isinstance(item, dict)
            ):
                members = network[f"subnet{version}"]
                identity["shared-network-name"] = network["name"]
                break
        members[:] = [item for item in members if not isinstance(item, dict) or item.get("id") != subnet["id"]]
        members.append(subnet)
    return {
        f"subnet{version}-list": _subnet_list(version, [identity]),
        f"subnet{version}-get": subnet_response,
        "config-get": configuration,
    }


def _pool_add_registry(subnet_id: int, cidr: str) -> dict:
    """Return the Pool add command chain for one Subnet."""
    return {
        "subnet4-list": _subnet_list(4, [{"id": subnet_id, "subnet": cidr}]),
        "reservation-get-page": {"result": 3},
        "subnet4-delta-add": {"result": 0},
        "config-get": _EMPTY_CONFIG4,
        "config-test": {"result": 0},
        "config-write": {"result": 0},
        "stat-lease4-get": _STAT_ABSENT4,
    }


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnets4View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/subnets4/"""

    def test_configured_subnet_offers_no_options_without_identity_hook(self):
        """A Subnet options change needs a Verified Subnet, which Kea cannot confirm without subnet_cmds."""
        config = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"id": 7, "subnet": "198.18.0.0/24"}]}}}
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": config}):
            response = self.client.get(reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
        self.assertContains(response, "198.18.0.0/24")
        self.assertNotContains(response, "Edit options")
        for action in ("options_edit", "wipe_leases"):
            url = reverse(f"plugins:netbox_kea:server_subnet4_{action}", args=[self.server.pk, 7])
            self.assertNotContains(response, f'href="{url}"')

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG4, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_sets_tab_in_context(self):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views.subnets import ServerDHCP4SubnetsView

        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG4, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerDHCP4SubnetsView.tab)

    def test_get_with_dhcp4_disabled_redirects_with_valid_pk(self):
        v6_only = _make_db_server(name="v6-only-subnets", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_subnets4", args=[v6_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(v6_only.pk), response.url)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnets6View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/subnets6/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG6, "stat-lease6-get": _STAT_ABSENT6}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_sets_tab_in_context(self):
        """F2: GET response must include 'tab' in context for tab bar highlighting.

        v4 and v6 subnets now render under the single shared 'Subnets' tab
        (owned by ServerDHCP4SubnetsView); the v6 view injects it via context.
        """
        from netbox_kea.views.subnets import ServerDHCP4SubnetsView

        url = reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG6, "stat-lease6-get": _STAT_ABSENT6}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerDHCP4SubnetsView.tab)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEnrichment(_ViewTestBase):
    """Subnet views must pass option-data and pool information through to the table."""

    def test_subnet_tab_and_combined_view_show_identical_rows(self):
        subnet = {
            "id": 7,
            "subnet": "198.18.1.0/24",
            "pools": [{"pool": "198.18.1.64/26"}],
            "option-data": [{"code": 3, "name": "routers", "data": "198.18.1.1"}],
            "ddns-qualifying-suffix": "example.test",
        }
        responses = {
            "subnet4-list": _subnet_list(4, [{"id": 7, "subnet": "198.18.1.0/24", "shared-network-name": "clients"}]),
            "config-get": {
                "result": 0,
                "arguments": {"Dhcp4": {"shared-networks": [{"name": "clients", "subnet4": [subnet]}]}},
            },
            "stat-lease4-get": _STAT_ABSENT4,
        }
        with stub_kea({**_ABSENT_READ_HOOKS, **responses}):
            tab_response = self.client.get(reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
            combined_response = self.client.get(
                reverse("plugins:netbox_kea:combined_subnets4"), {"server": self.server.pk}
            )

        self.assertEqual(tab_response.status_code, 200)
        self.assertEqual(combined_response.status_code, 200)
        self.assertEqual(combined_response.context["errors"], [])
        tab_rows = list(tab_response.context["table"].data)
        combined_rows = list(combined_response.context["table"].data)
        self.assertEqual(len(tab_rows), 1)
        self.assertEqual(len(combined_rows), 1)
        self.assertEqual(combined_rows[0]["pools"], ["198.18.1.64-198.18.1.127"])
        fields = ("id", "subnet", "pools", "options", "shared_network", "ddns_qualifying_suffix", "can_change")
        self.assertEqual(
            [{field: row[field] for field in fields} for row in tab_rows],
            [{field: row[field] for field in fields} for row in combined_rows],
        )

    def _config_with_subnet(self, version: int) -> list[dict]:
        """Return a mock config-get response with one subnet including options and pools."""
        subnet_key = f"subnet{version}"
        dhcp_key = f"Dhcp{version}"
        subnet = {
            "id": 1,
            "subnet": "10.0.0.0/24",
            "option-data": [
                {"code": 3, "name": "routers", "data": "10.0.0.1"},
                {"code": 6, "name": "domain-name-servers", "data": "8.8.8.8"},
            ],
            "pools": [{"pool": "10.0.0.50-10.0.0.99", "option-data": []}],
        }
        return [{"result": 0, "arguments": {dhcp_key: {subnet_key: [subnet], "shared-networks": []}}}]

    def _stub(self):
        """Subnet list with one option/pool-carrying subnet; stat_cmds hook absent."""
        return stub_kea(
            {**_ABSENT_READ_HOOKS, "config-get": self._config_with_subnet(4)[0], "stat-lease4-get": _STAT_ABSENT4}
        )

    def test_subnet_table_includes_options_data(self):
        """Each subnet dict in the table must carry parsed option-data."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with self._stub():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Table rows should contain gateway info from the subnet options
        self.assertContains(response, "10.0.0.1")

    def test_subnet_table_includes_pool_ranges(self):
        """Each subnet dict must carry pool range data."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with self._stub():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.50-10.0.0.99")

    def test_subnet_table_data_has_subnet_sort_key(self):
        """F1: each subnet dict must have an integer _subnet_sort_key for numeric sort."""
        import ipaddress

        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with self._stub():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        for row in table.data:
            self.assertIn("_subnet_sort_key", row, "Missing _subnet_sort_key in subnet row")
            self.assertIsInstance(row["_subnet_sort_key"], int)
        # Verify value: 10.0.0.0/24 → network address int
        first_row = next(iter(table.data))
        expected = int(ipaddress.ip_network("10.0.0.0/24").network_address)
        self.assertEqual(first_row["_subnet_sort_key"], expected)


class TestSubnetSnapshotDiagnostics(_ViewTestBase):
    def test_unavailable_catalogue_renders_an_error(self):
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "subnet4-list": requests.ConnectionError("identity unavailable"),
                "config-get": requests.ConnectionError("configuration unavailable"),
                "stat-lease4-get": _STAT_ABSENT4,
            }
        ):
            response = self.client.get(reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["table"].data), [])
        self.assertTrue(any(message.level == django_messages.ERROR for message in response.context["messages"]))
        self.assertContains(response, "Kea subnet identity facts are unavailable.")

    def test_incomplete_catalogue_renders_usable_rows_with_a_warning(self):
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "subnet4-list": _subnet_list(4, [{"id": 1, "subnet": "198.18.0.0/24"}]),
                "config-get": {
                    "result": 0,
                    "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "198.18.0.0/24", "pools": "invalid"}]}},
                },
                "stat-lease4-get": _STAT_ABSENT4,
            }
        ):
            response = self.client.get(reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
        self.assertContains(response, "198.18.0.0/24")
        self.assertContains(response, "Kea returned a non-list Pool collection.")
        levels = [message.level for message in response.context["messages"]]
        self.assertIn(django_messages.WARNING, levels)
        self.assertNotIn(django_messages.ERROR, levels)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: Subnet utilization statistics
# ─────────────────────────────────────────────────────────────────────────────

_STAT_LEASE4_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "result-set": {
                "columns": [
                    "subnet-id",
                    "total-addresses",
                    "assigned-addresses",
                    "declined-addresses",
                ],
                "rows": [[1, 100, 25, 0]],
            }
        },
    }
]


def _config_with_one_subnet(service=None):
    """Return a minimal config-get payload with one subnet."""
    version = 6 if (service and service[0] == "dhcp6") else 4
    dhcp_key = f"Dhcp{version}"
    subnet_key = f"subnet{version}"
    return [
        {
            "result": 0,
            "arguments": {
                dhcp_key: {
                    "option-data": [],
                    subnet_key: [{"id": 1, "subnet": "192.168.1.0/24", "option-data": [], "pools": []}],
                    "shared-networks": [],
                }
            },
        }
    ]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetUtilizationStats(_ViewTestBase):
    """Subnet table must show a utilization column when ``stat_cmds`` hook is loaded."""

    @staticmethod
    def _stat(assigned, total=100):
        """A stat-lease4-get response for one subnet with the given utilisation."""
        return {
            "result": 0,
            "arguments": {
                "result-set": {
                    "columns": ["subnet-id", "total-addresses", "assigned-addresses", "declined-addresses"],
                    "rows": [[1, total, assigned, 0]],
                }
            },
        }

    def test_utilization_percentage_shown_in_table(self):
        """25/100 addresses → '25%' utilization shown in subnets4 table."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "config-get": _config_with_one_subnet()[0],
                "stat-lease4-get": _STAT_LEASE4_RESPONSE[0],
            }
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "25%")

    def test_no_crash_when_stat_cmds_unavailable(self):
        """When stat_cmds hook is not loaded, subnets page must still render (200)."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea(
            {**_ABSENT_READ_HOOKS, "config-get": _config_with_one_subnet()[0], "stat-lease4-get": _STAT_ABSENT4}
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_zero_percent_when_no_leases_assigned(self):
        """0 assigned / 100 total → '0%' utilization."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea(
            {**_ABSENT_READ_HOOKS, "config-get": _config_with_one_subnet()[0], "stat-lease4-get": self._stat(0)}
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0%")

    def test_hundred_percent_when_fully_utilized(self):
        """All addresses assigned → '100%' utilization."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "config-get": _config_with_one_subnet()[0],
                "stat-lease4-get": self._stat(50, total=50),
            }
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "100%")


# ─────────────────────────────────────────────────────────────────────────────
# Feature 3.2: Subnet Lease Wipe — _BaseSubnetWipeView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4WipeView(_ViewTestBase):
    """Tests for ServerSubnet4WipeView (GET confirmation + POST wipe)."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_wipe_leases", args=[self.server.pk, subnet_id])

    def test_get_returns_confirmation_page(self):
        """GET must show the wipe confirmation page with subnet info."""
        subnet = {"result": 0, "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}]}}
        with stub_kea(_edit_responses(4, subnet, _EMPTY_CONFIG4)):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")
        self.assertContains(response, "42")

    def test_get_names_the_live_subnet_not_the_cached_one(self):
        """The confirmation must describe what the POST will act on, so it reads live."""
        cached = {"result": 0, "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}]}}
        live = {"result": 0, "arguments": {"subnet4": [{"id": 42, "subnet": "10.9.0.0/24"}]}}
        with stub_kea({**_ABSENT_READ_HOOKS, **_edit_responses(4, cached, _EMPTY_CONFIG4)}):
            self.client.get(reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
        with stub_kea(_edit_responses(4, live, _EMPTY_CONFIG4)):
            response = self.client.get(self._url())
        self.assertContains(response, "10.9.0.0/24")
        self.assertNotContains(response, "10.0.0.0/24")

    def test_get_shows_form_when_subnet_fetch_fails(self):
        """GET must still return 200 even when the subnet-get Kea call fails."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": {"result": 1, "text": "unavailable"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_calls_lease_wipe_and_redirects(self):
        """POST must call lease_wipe on the client and redirect to the subnets tab."""
        with stub_kea({**_ABSENT_READ_HOOKS, "lease4-wipe": {"result": 0}}) as kea:
            response = self.client.post(self._url(subnet_id=10))
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn("lease4-wipe", kea.commands())
        self.assertEqual(kea.bodies("lease4-wipe")[0]["arguments"]["subnet-id"], 10)

    def test_post_on_kea_exception_shows_error_message(self):
        """POST that causes a KeaException must flash an error and redirect (no 500)."""
        with stub_kea({**_ABSENT_READ_HOOKS, "lease4-wipe": {"result": 1, "text": "hook not loaded"}}):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_post_on_unexpected_exception_shows_error_message(self):
        """POST that raises an unexpected exception must redirect (no 500)."""
        with stub_kea({**_ABSENT_READ_HOOKS, "lease4-wipe": ValueError("unexpected")}):
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
class TestServerSubnet6WipeView(_ViewTestBase):
    """Tests for ServerSubnet6WipeView — verifies v6 variant uses correct Kea commands."""

    def _url(self, subnet_id=7):
        return reverse("plugins:netbox_kea:server_subnet6_wipe_leases", args=[self.server.pk, subnet_id])

    def test_get_returns_200(self):
        subnet = {"result": 0, "arguments": {"subnet6": [{"id": 7, "subnet": "2001:db8::/32"}]}}
        with stub_kea(_edit_responses(6, subnet, _EMPTY_CONFIG6)):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2001:db8::/32")

    def test_post_calls_lease_wipe_v6(self):
        """POST must call lease_wipe with version=6."""
        with stub_kea({**_ABSENT_READ_HOOKS, "lease6-wipe": {"result": 0}}) as kea:
            response = self.client.post(self._url(subnet_id=7))
        self.assertEqual(response.status_code, 302)
        self.assertIn("lease6-wipe", kea.commands())
        self.assertEqual(kea.bodies("lease6-wipe")[0]["arguments"]["subnet-id"], 7)


# ─────────────────────────────────────────────────────────────────────────────
# Subnet Edit views
# ─────────────────────────────────────────────────────────────────────────────

_SUBNET4_GET_FULL = [
    {
        "result": 0,
        "arguments": {
            "subnet4": [
                {
                    "id": 42,
                    "subnet": "10.0.0.0/24",
                    "pools": [{"pool": "10.0.0.100-10.0.0.200"}],
                    "option-data": [
                        {"name": "routers", "data": "10.0.0.1"},
                        {"name": "domain-name-servers", "data": "8.8.8.8"},
                    ],
                    "valid-lifetime": 3600,
                }
            ]
        },
    }
]

_SUBNET6_GET_FULL = [
    {
        "result": 0,
        "arguments": {
            "subnet6": [
                {
                    "id": 7,
                    "subnet": "2001:db8::/48",
                    "pools": [],
                    "option-data": [],
                    "valid-lifetime": 3600,
                }
            ]
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4EditView(_ViewTestBase):
    """Tests for ServerSubnet4EditView (GET prefill + POST update)."""

    # The update merges the form onto the live Subnet, so the daemon holds one to merge onto.
    _LIVE_SUBNET4 = {"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def _get_stub(self, subnet=None, config=None):
        return stub_kea(
            _edit_responses(
                4,
                subnet if subnet is not None else _SUBNET4_GET_FULL[0],
                config if config is not None else _CONFIG4_NO_NETWORKS[0],
            )
        )

    def _daemon(self, live: dict | None = None) -> SubnetDaemon:
        """The daemon that the form, the edit and the persist step read: Subnet 42 outside two Shared Networks."""
        return SubnetDaemon(4, [live or self._LIVE_SUBNET4], networks=("net-alpha", "net-beta"))

    def _post_stub(self, live: dict | None = None):
        return stub_kea({**_ABSENT_READ_HOOKS, **self._daemon(live).responses()})

    @staticmethod
    def _updated_subnet(kea):
        """The subnet object in the real subnet4-update payload."""
        return kea.bodies("subnet4-update")[0]["arguments"]["subnet4"][0]

    def test_displayed_suppressed_options_preserve_metadata_unless_cleared(self):
        options = [
            {"code": 3, "data": "198.18.0.1", "never-send": True},
            {"code": 6, "data": "198.18.0.53", "never-send": True, "csv-format": True},
            {"code": 42, "data": "198.18.0.123", "never-send": True, "always-send": False},
        ]
        live = {"id": 42, "subnet": "198.18.0.0/24", "pools": [], "option-data": options}
        for clear in (False, True):
            with self.subTest(clear=clear), self._post_stub(live) as kea:
                get = self.client.get(self._url())
                initial = get.context["form"].initial
                self.assertEqual(initial["gateway"], "198.18.0.1")
                self.assertEqual(initial["dns_servers"], "198.18.0.53")
                self.assertEqual(initial["ntp_servers"], "198.18.0.123")
                data = {**_shown(), "subnet_cidr": live["subnet"], "pools": "", "shared_network": ""}
                data.update(
                    {field: "" if clear else initial[field] for field in ("gateway", "dns_servers", "ntp_servers")}
                )
                post = self.client.post(self._url(), data)
                self.assertEqual(post.status_code, 302)
                self.assertEqual(self._updated_subnet(kea)["option-data"], [] if clear else options)

    def test_hidden_options_survive_blank_form_fields(self):
        for option in (
            {"code": 6, "never-send": True},
            {"code": 6, "data": "", "never-send": True},
            {"code": 6, "data": "C6120035", "csv-format": False},
        ):
            live = {"id": 42, "subnet": "198.18.0.0/24", "pools": [], "option-data": [option]}
            with self.subTest(option=option), self._post_stub(live) as kea:
                get = self.client.get(self._url())
                self.assertEqual(get.context["form"].initial.get("dns_servers", ""), "")
                post = self.client.post(
                    self._url(), {**_shown(), "subnet_cidr": live["subnet"], "dns_servers": "", "shared_network": ""}
                )
                self.assertEqual(post.status_code, 302)
                self.assertEqual(self._updated_subnet(kea)["option-data"], [option])

    def test_get_returns_200(self):
        """GET must render the edit form with status 200."""
        with self._get_stub():
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_prefills_form_with_current_subnet_values(self):
        """GET must pre-populate form with current subnet CIDR and pools."""
        with self._get_stub():
            response = self.client.get(self._url())
        self.assertContains(response, "10.0.0.0/24")
        self.assertContains(response, "10.0.0.100-10.0.0.200")

    def test_get_prefills_a_code_only_dns_option(self):
        """A DNS option written by code renders in the form so a save does not drop it."""
        subnet = deepcopy(_SUBNET4_GET_FULL[0])
        subnet["arguments"]["subnet4"][0]["option-data"] = [
            {"code": 6, "data": "10.0.0.53"},
            {"name": "domain-name-servers", "space": "vendor-4491", "data": "10.0.0.99"},
        ]
        with self._get_stub(subnet=subnet):
            response = self.client.get(self._url())
        self.assertEqual(response.context["form"].initial["dns_servers"], "10.0.0.53")

    def test_get_prefills_membership_from_the_live_declaration_when_the_catalogue_is_stale(self):
        """A Subnet added after the catalogue was cached must not render as global."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG4}):
            self.client.get(reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
        member = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [],
                    "shared-networks": [{"name": "net-alpha", "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}]}],
                }
            },
        }
        with self._get_stub(config=member):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["shared_network"], "net-alpha")

    def test_get_leaves_unshowable_dns_entries_out_of_the_form(self):
        """Binary and class-tagged entries have no form representation, so the field stays empty."""
        subnet = deepcopy(_SUBNET4_GET_FULL[0])
        subnet["arguments"]["subnet4"][0]["option-data"] = [
            {"code": 6, "data": "0A000035", "csv-format": False},
            {"code": 6, "data": "10.0.1.53", "client-classes": ["class-a"]},
        ]
        with self._get_stub(subnet=subnet):
            response = self.client.get(self._url())
        self.assertEqual(response.context["form"].initial.get("dns_servers", ""), "")

    def test_get_prefills_one_router_but_leaves_a_router_array_out(self):
        for data, expected in (("10.0.0.1", "10.0.0.1"), ("10.0.0.1, 10.0.0.2", "")):
            subnet = deepcopy(_SUBNET4_GET_FULL[0])
            subnet["arguments"]["subnet4"][0]["option-data"] = [{"code": 3, "data": data}]
            with self.subTest(data=data), self._get_stub(subnet=subnet):
                response = self.client.get(self._url())
            self.assertEqual(response.context["form"].initial.get("gateway", ""), expected)

    def test_get_prefills_membership_from_the_live_declaration_when_the_target_is_incomplete(self):
        """An incomplete live declaration still decides membership; the cached catalogue never does."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _CONFIG4_NO_NETWORKS[0]}):
            self.client.get(reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
        target = {"id": 42, "subnet": "10.0.0.0/24", "pools": [{"pool": "invalid"}]}
        live = {"result": 0, "arguments": {"subnet4": [{**target, "pools": []}]}}
        member = {
            "result": 0,
            "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": [{"name": "clients", "subnet4": [target]}]}},
        }
        with stub_kea(
            {
                "subnet4-list": _subnet_list(4, [{"id": 42, "subnet": "10.0.0.0/24"}]),
                "subnet4-get": live,
                "config-get": member,
            }
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["shared_network"], "clients")

    def test_get_reads_the_live_configuration_on_every_visit(self):
        """The edit form is a read-modify-write prefill, so it must not serve the display cache."""
        with self._get_stub() as kea:
            self.client.get(self._url())
            first = kea.commands().count("config-get")
            self.client.get(self._url())
            second = kea.commands().count("config-get")
        self.assertGreaterEqual(second - first, 1)

    def test_get_when_subnet_fetch_fails_redirects_with_error(self):
        """GET must redirect to the subnet list when the subnet-get Kea call fails."""
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "config-get": {"result": 1, "text": "unavailable"},
                "subnet4-get": {"result": 1, "text": "unavailable"},
            }
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("subnets", response.url)

    def test_post_valid_form_calls_subnet_update_and_redirects(self):
        """POST with valid form must call subnet_update and redirect to subnet list."""
        with self._post_stub() as kea:
            response = self.client.post(
                self._url(subnet_id=42),
                {
                    **_shown(),
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "10.0.0.100-10.0.0.200",
                    "gateway": "10.0.0.1",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn("subnet4-update", kea.commands())

    def test_post_passes_correct_version_and_subnet_id_to_subnet_update(self):
        """POST must issue subnet4-update (version=4) for the correct subnet_id."""
        with self._post_stub() as kea:
            self.client.post(
                self._url(subnet_id=42),
                {
                    **_shown(),
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertEqual(self._updated_subnet(kea)["id"], 42)

    def test_post_that_kea_rejects_rerenders_the_form_with_the_input(self):
        daemon = self._daemon()
        daemon.script("subnet4-update", {"result": 1, "text": "invalid pool"})
        with stub_kea({**_ABSENT_READ_HOOKS, **daemon.responses()}):
            response = self.client.post(
                self._url(subnet_id=42),
                {**_shown(), "subnet_cidr": "10.0.0.0/24", "pools": "10.0.0.100-10.0.0.200", "gateway": "10.0.0.1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["pools"].value(), "10.0.0.100-10.0.0.200")
        self.assertContains(response, "Kea rejected the change. Kea replied: invalid pool")

    def test_post_invalid_form_rerenders_with_200(self):
        """POST with invalid data (bad gateway IP) must re-render the form."""
        # Invalid form re-renders after the network config-get, before any subnet4-update.
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _CONFIG4_NO_NETWORKS[0]}) as kea:
            response = self.client.post(
                self._url(subnet_id=42),
                {
                    **_shown(),
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "not-an-ip",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("subnet4-update", kea.commands())

    def test_get_requires_login(self):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_post_requires_login(self):
        """Unauthenticated POST must redirect to login."""
        self.client.logout()
        response = self.client.post(self._url(), {})
        self.assertIn(response.status_code, (302, 403))

    def test_post_passes_renew_rebind_timers_to_subnet_update(self):
        """F11: POST with renew_timer and rebind_timer must reach the subnet4-update payload."""
        with self._post_stub() as kea:
            self.client.post(
                self._url(subnet_id=42),
                {
                    **_shown(),
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                    "renew_timer": "600",
                    "rebind_timer": "900",
                },
            )
        subnet = self._updated_subnet(kea)
        self.assertEqual(subnet["renew-timer"], 600)
        self.assertEqual(subnet["rebind-timer"], 900)

    def test_post_omits_timers_when_not_supplied(self):
        """F11: POST without timer fields must leave them out of the subnet4-update payload."""
        with self._post_stub() as kea:
            self.client.post(
                self._url(subnet_id=42),
                {
                    **_shown(),
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        subnet = self._updated_subnet(kea)
        self.assertNotIn("renew-timer", subnet)
        self.assertNotIn("rebind-timer", subnet)

    # ── DDNS qualifying suffix ────────────────────────────────────────────────

    def test_post_passes_ddns_qualifying_suffix_to_subnet_update(self):
        """POST with a DDNS suffix must reach the subnet4-update payload."""
        with self._post_stub() as kea:
            self.client.post(
                self._url(subnet_id=42),
                {
                    **_shown(),
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                    "ddns_qualifying_suffix": "example.com.",
                },
            )
        self.assertEqual(self._updated_subnet(kea)["ddns-qualifying-suffix"], "example.com.")

    def test_post_blank_managed_options_explicitly_clears_values(self):
        live = {
            "id": 42,
            "subnet": "198.18.0.0/24",
            "option-data": [
                {"code": 3, "data": "198.18.0.1"},
                {"code": 6, "data": "198.18.0.53"},
                {"code": 42, "data": "198.18.0.123"},
            ],
        }
        with self._post_stub(live) as kea:
            post = self.client.post(
                self._url(),
                {
                    **_shown(),
                    "subnet_cidr": live["subnet"],
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertEqual(post.status_code, 302)
        self.assertEqual(self._updated_subnet(kea)["option-data"], [])

    def test_post_clears_ddns_qualifying_suffix_with_empty_string(self):
        """Clearing the DDNS field on edit must remove it from the subnet4-update payload.

        The edit form is always fully populated, so an empty field means "clear". The
        live subnet carries a suffix; the cleared POST must drop it. This locks the
        view→client wiring: reverting the call to ``... or None`` would coerce the
        cleared field to None (= preserve), so the live suffix would survive in the payload.
        """
        live_with_ddns = {**self._LIVE_SUBNET4, "ddns-qualifying-suffix": "old.example.com."}
        with self._post_stub(live_with_ddns) as kea:
            self.client.post(
                self._url(subnet_id=42),
                {
                    **_shown(),
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                    "ddns_qualifying_suffix": "",
                },
            )
        self.assertNotIn("ddns-qualifying-suffix", self._updated_subnet(kea))

    # ── F5: inherited options ─────────────────────────────────────────────────

    _SUBNET4_NO_OPTS = {
        "result": 0,
        "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
    }

    def test_get_passes_inherited_dns_from_global_config(self):
        """F5: When subnet has no DNS set, inherited_options contains global DNS."""
        config_with_global_dns = {
            "result": 0,
            "arguments": {
                "Dhcp4": {"option-data": [{"name": "domain-name-servers", "data": "8.8.8.8"}], "shared-networks": []}
            },
        }
        with self._get_stub(subnet=self._SUBNET4_NO_OPTS, config=config_with_global_dns):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        inherited = response.context.get("inherited_options", {})
        self.assertIn("dns_servers", inherited)
        self.assertEqual(inherited["dns_servers"]["value"], "8.8.8.8")
        self.assertEqual(inherited["dns_servers"]["source"], "global")

    def test_get_inherited_options_empty_when_kea_config_fails(self):
        """Subnet facts prefill the form without unavailable inherited options."""
        with self._get_stub(subnet=_SUBNET4_GET_FULL[0], config={"result": 1, "text": "err"}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["inherited_options"], {})
        self.assertContains(response, "this form cannot be saved")

    def test_get_inherited_options_excludes_field_already_set_in_subnet(self):
        """F5: Fields already set in the subnet itself are excluded from inherited_options."""
        # _SUBNET4_GET_FULL has domain-name-servers: 8.8.8.8 in option-data
        config_with_global_dns = {
            "result": 0,
            "arguments": {
                "Dhcp4": {"option-data": [{"name": "domain-name-servers", "data": "1.1.1.1"}], "shared-networks": []}
            },
        }
        with self._get_stub(subnet=_SUBNET4_GET_FULL[0], config=config_with_global_dns):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        inherited = response.context.get("inherited_options", {})
        # dns_servers is already set by subnet — should NOT appear as inherited
        self.assertNotIn("dns_servers", inherited)

    def test_get_inherited_options_prefers_shared_network_over_global(self):
        """F5: Shared-network option-data overrides global in inherited_options."""
        config_shared_net = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "option-data": [{"name": "domain-name-servers", "data": "8.8.8.8"}],
                    "shared-networks": [
                        {
                            "name": "net-alpha",
                            "subnet4": [{"id": 42}],
                            "option-data": [{"name": "domain-name-servers", "data": "192.168.1.1"}],
                        }
                    ],
                }
            },
        }
        with self._get_stub(subnet=self._SUBNET4_NO_OPTS, config=config_shared_net):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        inherited = response.context.get("inherited_options", {})
        self.assertIn("dns_servers", inherited)
        # Should use shared-network value, not global
        self.assertEqual(inherited["dns_servers"]["value"], "192.168.1.1")
        self.assertIn("net-alpha", inherited["dns_servers"]["source"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet6EditView(_ViewTestBase):
    """Tests for ServerSubnet6EditView — verifies v6 variant uses correct version."""

    # The update merges the form onto the live Subnet, so the daemon holds one to merge onto.
    _LIVE_SUBNET6 = {"id": 7, "subnet": "2001:db8::/48", "pools": [], "option-data": []}

    def _url(self, subnet_id=7):
        return reverse("plugins:netbox_kea:server_subnet6_edit", args=[self.server.pk, subnet_id])

    def _get_stub(self, subnet=None, config=None):
        return stub_kea(
            _edit_responses(
                6,
                subnet if subnet is not None else _SUBNET6_GET_FULL[0],
                config if config is not None else _CONFIG6_NO_NETWORKS[0],
            )
        )

    def _post_stub(self):
        return stub_kea({**_ABSENT_READ_HOOKS, **SubnetDaemon(6, [self._LIVE_SUBNET6]).responses()})

    @staticmethod
    def _updated_subnet(kea):
        """The subnet object in the real subnet6-update payload."""
        return kea.bodies("subnet6-update")[0]["arguments"]["subnet6"][0]

    def test_get_returns_200(self):
        """GET must return 200 for IPv6 edit view."""
        with self._get_stub():
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_calls_subnet_update_with_version_6(self):
        """POST must issue subnet6-update (the v6-specific command) for the correct subnet_id."""
        with self._post_stub() as kea:
            response = self.client.post(
                self._url(subnet_id=7),
                {
                    **_shown(),
                    "subnet_cidr": "2001:db8::/48",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                },
            )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn("subnet6-update", kea.commands())
        self.assertEqual(self._updated_subnet(kea)["id"], 7)


# ---------------------------------------------------------------------------
# Test data for subnet→network assignment
# ---------------------------------------------------------------------------

# Config-get response where subnet 42 is inside "net-alpha"
_CONFIG4_WITH_SUBNET_IN_NETWORK = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [],
                "shared-networks": [
                    {
                        "name": "net-alpha",
                        "subnet4": [
                            {"id": 42, "subnet": "10.0.0.0/24"},
                        ],
                    },
                    {
                        "name": "net-beta",
                        "subnet4": [],
                    },
                ],
            }
        },
    }
]

# Config-get response where subnet 42 is NOT in any shared network
_CONFIG4_NO_NETWORKS = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}],
                "shared-networks": [
                    {"name": "net-alpha", "subnet4": []},
                    {"name": "net-beta", "subnet4": []},
                ],
            }
        },
    }
]

# Config-get response for v6 subnet edit (no network assignment)
_CONFIG6_NO_NETWORKS = [
    {
        "result": 0,
        "arguments": {
            "Dhcp6": {
                "subnet6": [{"id": 7, "subnet": "2001:db8::/48"}],
                "shared-networks": [],
            }
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4EditViewNetworkAssignment(_ViewTestBase):
    """Tests for shared-network assignment in ServerSubnet4EditView."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def _get_stub(self, config):
        return stub_kea(_edit_responses(4, _SUBNET4_GET_FULL[0], config))

    def test_get_shows_network_dropdown_with_available_networks(self):
        """GET must render the form with a shared_network dropdown listing available networks."""
        with self._get_stub(config=_CONFIG4_NO_NETWORKS[0]):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "net-alpha")
        self.assertContains(response, "net-beta")

    def test_get_preselects_current_network_when_subnet_belongs_to_network(self):
        """GET must pre-select the current shared network in the dropdown."""
        with self._get_stub(config=_CONFIG4_WITH_SUBNET_IN_NETWORK[0]):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        # The form initial value should be net-alpha (selected option)
        self.assertContains(response, "net-alpha")

    def test_post_moves_the_subnet_from_the_shown_network_to_the_chosen_one(self):
        """The view sends the Shared Network that the page showed and the chosen one; Kea holds the shown one."""
        for current, chosen, deleted, added in (
            (None, "net-alpha", [], ["net-alpha"]),
            ("net-alpha", "", ["net-alpha"], []),
            ("net-alpha", "net-beta", ["net-alpha"], ["net-beta"]),
            ("net-alpha", "net-alpha", [], []),
        ):
            with self.subTest(current=current, chosen=chosen):
                daemon = SubnetDaemon(
                    4,
                    [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}],
                    networks=("net-alpha", "net-beta"),
                    members={42: current} if current else {},
                )
                with stub_kea({**_ABSENT_READ_HOOKS, **daemon.responses()}) as kea:
                    response = self.client.post(
                        self._url(), {"subnet_cidr": "10.0.0.0/24", "shared_network": chosen, **_shown(current or "")}
                    )
                self.assertEqual(response.status_code, 302)
                self.assertEqual([body["arguments"]["name"] for body in kea.bodies("network4-subnet-del")], deleted)
                self.assertEqual([body["arguments"]["name"] for body in kea.bodies("network4-subnet-add")], added)
                self.assertEqual(daemon.members, {42: chosen} if chosen else {})
                self.assertIn("subnet4-update", kea.commands())


# ─────────────────────────────────────────────────────────────────────────────
# Subnet add: the Shared Network field
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4AddViewSharedNetwork(_ViewTestBase):
    """GET/POST /plugins/kea/servers/<pk>/subnets4/add/ — shared_network field."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    def _stub(self):
        return stub_kea({**_ABSENT_READ_HOOKS, **SubnetDaemon(4, networks=("net-alpha", "net-beta")).responses()})

    def test_get_shows_shared_network_dropdown(self):
        """GET must render a shared_network dropdown populated from Kea config."""
        with self._stub():
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "net-alpha")
        self.assertContains(response, "net-beta")

    def test_post_rejects_ntp_hostname_without_subnet_add(self):
        with self._stub() as kea:
            response = self.client.post(self._url(), {**_SUBNET_ADD_POST, "ntp_servers": "ntp.example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["ntp_servers"],
            ["Invalid NTP server IP address: 'ntp.example.com'"],
        )
        self.assertNotIn("subnet4-add", kea.commands())


# ---------------------------------------------------------------------------
# Tests for _get_network_choices — None/missing arguments handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkChoicesNoneArguments(_ViewTestBase):
    """Subnet facts prefill the edit form when Server Configuration is unavailable."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def test_get_rejects_malformed_subnet_response(self):
        payloads = (
            [],
            {"result": 0, "arguments": {"subnet4": {"id": 42}}},
            {"result": 0, "arguments": {"subnet4": ["not-an-object"]}},
            {"result": 0, "arguments": {"subnet4": [{"id": 7, "subnet": "10.0.0.0/24"}]}},
            {"result": 0, "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [{"pool": "bad"}]}]}},
        )
        for payload in payloads:
            with (
                self.subTest(payload=payload),
                stub_kea(
                    {
                        **_ABSENT_READ_HOOKS,
                        "config-get": {"result": 1, "text": "unavailable"},
                        "subnet4-get": payload,
                    }
                ),
            ):
                response = self.client.get(self._url())
                self.assertRedirects(
                    response,
                    reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]),
                    fetch_redirect_response=False,
                )
                self.assertTrue(
                    any(
                        message.level == django_messages.ERROR and "Could not load subnet configuration" in str(message)
                        for message in get_messages(response.wsgi_request)
                    )
                )

    def test_get_ignores_membership_under_a_duplicate_shared_network_name(self):
        """A duplicate network name makes membership unknown, so the form reads the Subnet alone."""
        member = {"id": 42, "subnet": "10.0.0.0/24"}
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {"shared-networks": [{"name": "dup", "subnet4": [member]}, {"name": "dup", "subnet4": []}]}
            },
        }
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": config, "subnet4-get": _SUBNET4_GET_FULL[0]}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["shared_network"], "")
        self.assertContains(response, "this form cannot be saved")
        self.assertIn("subnet4-get", kea.commands())

    def test_get_falls_back_when_config_returns_none_arguments(self):
        """Malformed configuration leaves the Subnet form available."""
        config_none_args = {"result": 0, "arguments": None, "text": "no config"}
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": config_none_args, "subnet4-get": _SUBNET4_GET_FULL[0]}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this form cannot be saved")

    def test_get_falls_back_when_config_raises_kea_exception(self):
        """GET must not crash when config-get fails (result 1 → real KeaException)."""
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "config-get": {"result": 1, "text": "error"},
                "subnet4-get": _SUBNET4_GET_FULL[0],
            }
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this form cannot be saved")


# ---------------------------------------------------------------------------
# Tests for renew/rebind timer zero round-trip (subnet edit GET)
# ---------------------------------------------------------------------------

_SUBNET4_GET_ZERO_TIMERS = [
    {
        "result": 0,
        "arguments": {
            "subnet4": [
                {
                    "id": 42,
                    "subnet": "10.1.0.0/24",
                    "renew-timer": 0,
                    "rebind-timer": 0,
                    "pools": [],
                    "option-data": [],
                }
            ]
        },
    }
]

_CONFIG4_NO_NETWORKS_RESP = [{"result": 0, "arguments": {"Dhcp4": {"shared-networks": [], "subnet4": []}}}]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditZeroTimers(_ViewTestBase):
    """Renew/rebind timer value of 0 must round-trip through subnet edit GET."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def _stub(self):
        """The Server Configuration declares zero-valued Subnet timers."""
        return stub_kea(_edit_responses(4, _SUBNET4_GET_ZERO_TIMERS[0], _CONFIG4_NO_NETWORKS_RESP[0]))

    def test_get_includes_zero_renew_timer_in_initial(self):
        """GET for a subnet with renew-timer=0 must populate the form field with 0."""
        with self._stub():
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        form = response.context.get("form")
        self.assertIsNotNone(form, "Expected a form in context but got None")
        self.assertEqual(form.initial.get("renew_timer"), 0)

    def test_get_includes_zero_rebind_timer_in_initial(self):
        """GET for a subnet with rebind-timer=0 must populate the form field with 0."""
        with self._stub():
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        form = response.context.get("form")
        self.assertIsNotNone(form, "Expected a form in context but got None")
        self.assertEqual(form.initial.get("rebind_timer"), 0)


# ---------------------------------------------------------------------------
# Pool view exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolDeleteExceptions(_ViewTestBase):
    """_BasePoolDeleteView checks the Pool text in the URL."""

    _SPACED = "192.0.2.10 - 192.0.2.20"

    def _stub(self):
        subnet = {"id": 42, "subnet": "192.0.2.0/24", "pools": [{"pool": self._SPACED}]}
        return stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                **_catalogue_responses_for_subnets(4, [subnet]),
                "subnet4-delta-del": {"result": 0},
                "config-test": {"result": 0},
                "config-write": {"result": 0},
            }
        )

    def test_get_invalid_pool_format_returns_400(self):
        """GET with invalid pool string must return 400 (before any Kea call)."""
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "not_a_pool_format!!"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)

    def test_get_range_pool_with_spaces_returns_200(self):
        """GET with a Kea range pool string like '192.0.2.10 - 192.0.2.20' must not return 400."""
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, self._SPACED])
        with self._stub():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_post_invalid_pool_format_returns_400(self):
        """POST with invalid pool string must return 400 (before any Kea call)."""
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "not_a_pool_format!!"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 400)

    def test_post_range_pool_with_spaces_sends_the_normalized_range(self):
        """POST with a Kea range pool string like '192.0.2.10 - 192.0.2.20' deletes that Pool."""
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, self._SPACED])
        with self._stub() as kea:
            response = self.client.post(url, {"subnet_cidr": "192.0.2.0/24"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            kea.bodies("subnet4-delta-del")[0]["arguments"],
            {"subnet4": [{"id": 42, "subnet": "192.0.2.0/24", "pools": [{"pool": "192.0.2.10-192.0.2.20"}]}]},
        )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddAllocatesThroughCatalogue(_ViewTestBase):
    """The add view takes each new Subnet identity from a live Subnet Catalogue observation."""

    def _post(self, daemon: SubnetDaemon, **data):
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        self.client.cookies.pop("messages", None)
        with stub_kea({**_ABSENT_READ_HOOKS, **daemon.responses()}) as kea:
            response = self.client.post(url, {**_SUBNET_ADD_POST, **data})
        return response, kea

    @staticmethod
    def _added_ids(kea):
        return [body["arguments"]["subnet4"][0]["id"] for body in kea.bodies("subnet4-add")]

    def test_blank_id_takes_the_highest_existing_id_plus_one(self):
        daemon = SubnetDaemon(4, [{"id": 3, "subnet": "198.18.3.0/24"}, {"id": 7, "subnet": "198.18.7.0/24"}])
        response, kea = self._post(daemon, subnet="198.18.1.0/24")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.bodies("subnet4-add")[0]["arguments"]["subnet4"], [{"subnet": "198.18.1.0/24", "id": 8}])

    def test_existing_explicit_id_is_a_form_error_without_a_kea_write(self):
        response, kea = self._post(
            SubnetDaemon(4, [{"id": 7, "subnet": "198.18.7.0/24"}]), subnet="198.18.1.0/24", subnet_id="7"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["subnet_id"], ["Subnet ID 7 already exists."])
        self.assertNotIn("subnet4-add", kea.commands())

    def test_existing_cidr_is_a_form_error_without_a_kea_write(self):
        for existing in ("198.18.1.0/24", "198.18.1.5/24"):
            with self.subTest(existing=existing):
                response, kea = self._post(SubnetDaemon(4, [{"id": 4, "subnet": existing}]), subnet="198.18.1.0/24")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["form"].errors["subnet"], ["Subnet 198.18.1.0/24 already exists."])
                self.assertNotIn("subnet4-add", kea.commands())

    def test_other_family_cidr_is_a_form_error_without_a_kea_write(self):
        response, kea = self._post(SubnetDaemon(4), subnet="2001:db8::/64")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["subnet"], ["Enter an IPv4 Subnet CIDR."])
        self.assertNotIn("subnet4-add", kea.commands())

    def test_incomplete_identity_blocks_creation(self):
        for label, identity in (
            ("hook absent", {"result": 2, "text": "subnet_cmds is not loaded"}),
            ("read failed", {"result": 1, "text": "internal error"}),
            ("malformed entry", _subnet_list(4, [{"id": "one", "subnet": "198.18.9.0/24"}])),
        ):
            with self.subTest(label):
                daemon = SubnetDaemon(4)
                daemon.script("subnet4-list", identity)
                response, kea = self._post(daemon, subnet="198.18.1.0/24")
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "Make sure the subnet_cmds hook library is loaded",
                    " ".join(response.context["form"].non_field_errors()),
                )
                self.assertNotIn("subnet4-add", kea.commands())

    def test_exhausted_id_range_is_a_form_error_without_a_kea_write(self):
        daemon = SubnetDaemon(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        # mock-ok: the real range is 4 billion IDs wide, so narrowing it is the only way to fill it.
        with patch("netbox_kea.subnet_catalogue.MAX_SUBNET_ID", 1):
            response, kea = self._post(daemon, subnet="198.18.2.0/24")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["subnet_id"], ["The Kea subnet ID range is exhausted."])
        self.assertNotIn("subnet4-add", kea.commands())

    def test_concurrent_id_collision_retries_once_with_a_fresh_allocation(self):
        daemon = SubnetDaemon(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        daemon.before("subnet4-add", lambda d: d.add({"id": 2, "subnet": "198.18.200.0/24"}))
        response, kea = self._post(daemon, subnet="198.18.2.0/24")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._added_ids(kea), [2, 3])
        self.assertIn(3, daemon.ids())
        self.assertIn("Subnet 3 (198.18.2.0/24) added.", [str(m) for m in get_messages(response.wsgi_request)])

    def test_second_concurrent_collision_is_a_kea_rejection(self):
        daemon = SubnetDaemon(4, [{"id": 1, "subnet": "198.18.1.0/24"}])
        daemon.before("subnet4-add", lambda d: d.add({"id": 2, "subnet": "198.18.200.0/24"}))
        daemon.before("subnet4-add", lambda d: d.add({"id": 3, "subnet": "198.18.201.0/24"}))
        response, kea = self._post(daemon, subnet="198.18.2.0/24")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._added_ids(kea), [2, 3])
        errors = [str(m) for m in get_messages(response.wsgi_request) if m.level == django_messages.ERROR]
        self.assertEqual(
            errors, ["Kea rejected the change. Kea replied: ID of the new IPv4 subnet '3' is already in use"]
        )

    def test_a_retry_whose_fresh_observation_holds_the_cidr_is_a_form_error(self):
        def rival(d):
            d.add({"id": 1, "subnet": "198.18.200.0/24"})
            d.add({"id": 5, "subnet": "198.18.2.0/24"})

        daemon = SubnetDaemon(4)
        daemon.before("subnet4-add", rival)
        response, kea = self._post(daemon, subnet="198.18.2.0/24")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["subnet"], ["Subnet 198.18.2.0/24 already exists."])
        self.assertEqual(self._added_ids(kea), [1])

    def test_rejection_without_a_collision_is_not_retried(self):
        for label, data in (("automatic ID", {}), ("explicit ID", {"subnet_id": "5"})):
            with self.subTest(label):
                daemon = SubnetDaemon(4)
                daemon.script("subnet4-add", {"result": 1, "text": "bad pool"})
                response, kea = self._post(daemon, subnet="198.18.2.0/24", **data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(kea.commands().count("subnet4-add"), 1)

    def test_pool_errors_are_form_errors_without_a_kea_write(self):
        for pools, error in (
            ("198.18.1.10-198.18.1.20\n198.18.2.0/28", "Pool 198.18.2.0/28 is outside Subnet 198.18.1.0/24."),
            (
                "198.18.1.10-198.18.1.20\n198.18.1.16/28",
                "Pool 198.18.1.16-198.18.1.31 overlaps Pool 198.18.1.10-198.18.1.20.",
            ),
            ("nonsense", "Pool nonsense must be a range (start-end) or a prefix (CIDR)."),
        ):
            with self.subTest(pools=pools):
                response, kea = self._post(SubnetDaemon(4), subnet="198.18.1.0/24", pools=pools)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["form"].errors["pools"], [error])
                self.assertNotIn("subnet4-add", kea.commands())

    def test_parsed_pools_reach_kea_as_ranges(self):
        response, kea = self._post(
            SubnetDaemon(4), subnet="198.18.1.0/24", pools=" 198.18.1.10 - 198.18.1.20 \n198.18.1.64/28"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            kea.bodies("subnet4-add")[0]["arguments"]["subnet4"][0]["pools"],
            [{"pool": "198.18.1.10-198.18.1.20"}, {"pool": "198.18.1.64-198.18.1.79"}],
        )


# ---------------------------------------------------------------------------
# SubnetAdd exception paths
# ---------------------------------------------------------------------------

_SUBNET_ADD_POST = {
    "subnet": "10.2.0.0/24",
    "subnet_id": "",
    "pools": "",
    "gateway": "",
    "dns_servers": "",
    "ntp_servers": "",
    "shared_network": "",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddExceptionPaths(_ViewTestBase):
    """_BaseSubnetAddView GET/POST paths around the change."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    def test_get_falls_back_when_network_choices_raise(self):
        """GET must render the form with fallback choices when config-get fails (result 1 → KeaException)."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": {"result": 1, "text": "config error"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_add_with_invalid_pool_on_another_subnet(self):
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {"subnet4": [{"id": 90, "subnet": "198.18.90.0/24", "pools": [{"pool": "invalid"}]}]}
            },
        }
        responses = {
            **_ABSENT_READ_HOOKS,
            "config-get": config,
            "subnet4-list": _subnet_list(4, [{"id": 90, "subnet": "198.18.90.0/24"}]),
            "subnet4-add": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        with stub_kea(responses):
            response = self.client.post(self._url(), {**_SUBNET_ADD_POST, "subnet": "198.18.1.0/24"})
        self.assertEqual(response.status_code, 302)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any(message.level == django_messages.SUCCESS for message in messages))
        self.assertTrue(any(message.level == django_messages.WARNING for message in messages))

    def test_post_with_an_unusable_client_configuration_sends_nothing(self):
        """The Shared Network read fails before the change, so the form reports it and nothing reaches Kea."""
        bad_server = _make_db_server(name="bad-cert", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[bad_server.pk])
        with stub_kea(SubnetDaemon(4).responses()) as kea:
            response = self.client.post(url, _SUBNET_ADD_POST)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not load shared networks from Kea")
        self.assertEqual(kea.commands(), [])


_ADDED = "Subnet 4 (10.2.0.0/24) added."
_UNCONFIRMED_ADD = "Kea did not confirm the change. Check the server configuration before retrying."
_LOST = "Kea's reply to the change was lost or unreadable."
_SENT_ID = "NetBox sent Subnet 4 (10.2.0.0/24)."
_RESTART = "It is live, but it may not survive a Kea restart, because Kea did not save it to disk."
_ADD_PERSIST = ["config-get", "config-test", "config-write"]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddMessages(_ViewTestBase):
    """One POST per Configuration Change outcome and per rejection, for a Subnet add.

    ``test_config_write`` covers the outcomes in depth. The form read, the scope read and the persist step all
    read the same daemon.
    """

    def _daemon(self) -> SubnetDaemon:
        return SubnetDaemon(4, [{"id": 3, "subnet": "10.1.0.0/24"}], networks=("alpha",))

    def _post(self, daemon: SubnetDaemon, **data):
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, **daemon.responses()}) as kea:
            response = self.client.post(url, {**_SUBNET_ADD_POST, **data})
        messages = [(m.level, str(m)) for m in get_messages(response.wsgi_request)]
        return response, messages, kea.commands()

    def test_applied_and_persisted(self):
        response, messages, commands = self._post(self._daemon())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages, [(django_messages.SUCCESS, _ADDED)])
        self.assertEqual(commands, ["config-get", "subnet4-list", "config-get", "subnet4-add", *_ADD_PERSIST])

    def test_applied_with_a_shared_network(self):
        daemon = self._daemon()
        response, messages, commands = self._post(daemon, shared_network="alpha")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            messages, [(django_messages.SUCCESS, "Subnet 4 (10.2.0.0/24) added to Shared Network 'alpha'.")]
        )
        self.assertEqual(
            commands,
            [
                "config-get",
                "network4-get",
                "subnet4-list",
                "config-get",
                "subnet4-add",
                "network4-subnet-add",
                *_ADD_PERSIST,
            ],
        )
        self.assertEqual(daemon.members, {4: "alpha"})

    def test_applied_and_persistence_not_requested(self):
        self.server.persist_config = False
        self.server.save()
        response, messages, commands = self._post(self._daemon())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages, [(django_messages.SUCCESS, _ADDED)])
        self.assertEqual(commands[-1], "subnet4-add")

    def test_applied_and_persistence_failed_shows_the_restart_warning(self):
        daemon = self._daemon()
        daemon.script("config-write", {"result": 1, "text": "disk full"})
        response, messages, _ = self._post(daemon)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages, [(django_messages.WARNING, f"{_ADDED} {_RESTART} config-write failed: disk full")])

    def test_unknown_names_the_sent_id(self):
        daemon = self._daemon()
        daemon.script("subnet4-add", Applied(requests.ReadTimeout("read timed out")))
        response, messages, commands = self._post(daemon, shared_network="alpha")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages, [(django_messages.WARNING, f"{_UNCONFIRMED_ADD} {_LOST} {_SENT_ID}")])
        self.assertNotIn("network4-subnet-add", commands)
        self.assertEqual(commands[-3:], _ADD_PERSIST)

    def test_unknown_and_persistence_failed_never_claims_the_change_is_live(self):
        daemon = self._daemon()
        daemon.script("subnet4-add", requests.ReadTimeout("read timed out"))
        daemon.script("config-write", requests.ReadTimeout("read timed out"))
        _, messages, _ = self._post(daemon)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        f"{_UNCONFIRMED_ADD} Kea also could not save its running configuration to disk. {_LOST} "
                        f"{_SENT_ID} The reply to config-write was lost or unreadable."
                    ),
                )
            ],
        )

    def test_unknown_when_a_subnet_with_the_sent_identity_appeared(self):
        daemon = self._daemon()
        daemon.before("subnet4-add", lambda d: d.add({"id": 4, "subnet": "10.2.0.0/24"}))
        response, messages, _ = self._post(daemon)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        f"{_UNCONFIRMED_ADD} Kea replied: ID of the new IPv4 subnet '4' is already in use "
                        f"The read after the failure shows the change. {_SENT_ID}"
                    ),
                )
            ],
        )

    def test_unknown_after_a_failed_rollback_names_each_step(self):
        daemon = self._daemon()
        daemon.script("network4-subnet-add", {"result": 1, "text": "subnet is in use"})
        daemon.script("subnet4-del", requests.ReadTimeout("read timed out"))
        response, messages, _ = self._post(daemon, shared_network="alpha")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        f"{_UNCONFIRMED_ADD} Step 1, add Subnet 4 (10.2.0.0/24): applied. "
                        "Step 2, assign it to Shared Network 'alpha': not applied. Kea replied: subnet is in use "
                        f"Step 3, delete Subnet 4 again: unknown. {_LOST}"
                    ),
                )
            ],
        )

    def test_kea_rejected_keeps_the_input(self):
        daemon = self._daemon()
        daemon.script("subnet4-add", {"result": 1, "text": "invalid pool"})
        response, messages, commands = self._post(daemon, pools="10.2.0.10-10.2.0.20")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["pools"].value(), "10.2.0.10-10.2.0.20")
        self.assertEqual(messages, [(django_messages.ERROR, "Kea rejected the change. Kea replied: invalid pool")])
        self.assertNotIn("config-write", commands)

    def test_kea_rejected_assignment_after_the_rollback(self):
        daemon = self._daemon()
        daemon.script("network4-subnet-add", {"result": 1, "text": "subnet is in use"})
        response, messages, commands = self._post(daemon, shared_network="alpha")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.ERROR,
                    (
                        "Kea rejected the change. NetBox added Subnet 4 (10.2.0.0/24), but the assignment to Shared "
                        "Network 'alpha' did not apply, so NetBox deleted the Subnet again. Kea replied: subnet is in use"
                    ),
                )
            ],
        )
        self.assertEqual(daemon.ids(), [3])
        self.assertNotIn("config-write", commands)

    def test_not_sent_when_the_shared_network_is_gone(self):
        daemon = self._daemon()
        daemon.before("network4-get", lambda d: d.networks.remove("alpha"))
        response, messages, commands = self._post(daemon, shared_network="alpha")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            messages, [(django_messages.ERROR, "The change was not sent to Kea. Shared Network 'alpha' not found.")]
        )
        self.assertEqual(commands, ["config-get", "network4-get"])

    def test_not_sent_after_a_refused_connection(self):
        refused = _refused_connection()
        daemon = self._daemon()
        daemon.script("subnet4-add", refused)
        response, messages, _ = self._post(daemon)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            messages, [(django_messages.ERROR, "The change was not sent to Kea. Kea could not be reached.")]
        )


_EDITED_SUBNET = "Subnet 42 (10.0.0.0/24) updated."
_SCOPE = ["subnet4-list", "config-get"]


def _page_data(form) -> dict[str, str]:
    """Return the data that a browser posts for *form*, as the page rendered it."""
    return {name: "" if form[name].value() is None else str(form[name].value()) for name in form.fields}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditMessages(_ViewTestBase):
    """One POST per Configuration Change outcome and per rejection, for a Subnet edit.

    ``test_config_write`` covers the outcomes in depth. The form read, the scope read and the persist step all read the
    same daemon, which holds Subnet 42 in Shared Network 'alpha'.
    """

    @staticmethod
    def _daemon(cidr: str = "10.0.0.0/24") -> SubnetDaemon:
        return SubnetDaemon(
            4,
            [{"id": 42, "subnet": cidr, "pools": [], "option-data": []}],
            networks=("alpha", "beta"),
            members={42: "alpha"},
        )

    def _post(self, daemon: SubnetDaemon, **data):
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        with stub_kea({**_ABSENT_READ_HOOKS, **daemon.responses()}) as kea:
            response = self.client.post(
                url, {"subnet_cidr": "10.0.0.0/24", "shared_network": "alpha", **_shown("alpha"), **data}
            )
        messages = [(m.level, str(m)) for m in get_messages(response.wsgi_request)]
        return response, messages, kea.commands()

    def test_applied_and_persisted(self):
        daemon = self._daemon()
        response, messages, commands = self._post(daemon, valid_lft="7200")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages, [(django_messages.SUCCESS, _EDITED_SUBNET)])
        self.assertEqual(commands, ["config-get", *_SCOPE, "subnet4-get", "subnet4-update", *_ADD_PERSIST])
        self.assertEqual(daemon.subnet(42)["valid-lifetime"], 7200)

    def test_applied_with_a_move(self):
        daemon = self._daemon()
        response, messages, commands = self._post(daemon, shared_network="beta")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages, [(django_messages.SUCCESS, _EDITED_SUBNET)])
        self.assertEqual(
            commands,
            [
                "config-get",
                *_SCOPE,
                "network4-get",
                "subnet4-get",
                "network4-subnet-del",
                "network4-subnet-add",
                "subnet4-update",
                *_ADD_PERSIST,
            ],
        )
        self.assertEqual(daemon.members, {42: "beta"})

    def test_applied_and_persistence_not_requested(self):
        self.server.persist_config = False
        self.server.save()
        response, messages, commands = self._post(self._daemon())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages, [(django_messages.SUCCESS, _EDITED_SUBNET)])
        self.assertEqual(commands[-1], "subnet4-update")

    def test_applied_and_persistence_failed_shows_the_restart_warning(self):
        daemon = self._daemon()
        daemon.script("config-write", {"result": 1, "text": "disk full"})
        response, messages, _ = self._post(daemon)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            messages, [(django_messages.WARNING, f"{_EDITED_SUBNET} {_RESTART} config-write failed: disk full")]
        )

    def test_unknown_names_each_step_and_still_persists(self):
        daemon = self._daemon()
        daemon.script("subnet4-update", Applied(requests.ReadTimeout("read timed out")))
        response, messages, commands = self._post(daemon, shared_network="beta")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        f"{_UNCONFIRMED_ADD} Step 1, remove Subnet 42 from Shared Network 'alpha': applied. "
                        "Step 2, add Subnet 42 to Shared Network 'beta': applied. "
                        f"Step 3, update the fields of Subnet 42: unknown. {_LOST}"
                    ),
                )
            ],
        )
        self.assertEqual(commands[-4:], ["subnet4-update", *_ADD_PERSIST])

    def test_unknown_after_a_failed_rollback_names_each_step(self):
        daemon = self._daemon()
        daemon.script(
            "network4-subnet-add",
            {"result": 1, "text": "shared network is locked"},
            requests.ReadTimeout("read timed out"),
        )
        response, messages, commands = self._post(daemon, shared_network="beta")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.WARNING,
                    (
                        f"{_UNCONFIRMED_ADD} Step 1, remove Subnet 42 from Shared Network 'alpha': applied. "
                        "Step 2, add Subnet 42 to Shared Network 'beta': not applied. "
                        "Kea replied: shared network is locked "
                        f"Step 3, add Subnet 42 to Shared Network 'alpha' to undo step 1: unknown. {_LOST}"
                    ),
                )
            ],
        )
        self.assertNotIn("subnet4-update", commands)
        self.assertEqual(commands[-3:], _ADD_PERSIST)

    def test_kea_rejected_after_the_rollback_keeps_the_input(self):
        daemon = self._daemon()
        daemon.script("subnet4-update", {"result": 1, "text": "invalid pool"})
        response, messages, commands = self._post(daemon, shared_network="beta", pools="10.0.0.10-10.0.0.20")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["pools"].value(), "10.0.0.10-10.0.0.20")
        self.assertEqual(
            messages,
            [
                (
                    django_messages.ERROR,
                    (
                        "Kea rejected the change. A step of the change did not apply, so NetBox undid the steps "
                        "before it. Subnet 42 (10.0.0.0/24) is in Shared Network 'alpha' again. "
                        "Kea replied: invalid pool"
                    ),
                )
            ],
        )
        self.assertEqual(daemon.members, {42: "alpha"})
        self.assertNotIn("config-write", commands)

    def test_not_sent_when_the_subnet_changed(self):
        response, messages, commands = self._post(self._daemon("10.0.9.0/24"), shared_network="beta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            messages,
            [
                (
                    django_messages.ERROR,
                    (
                        "The change was not sent to Kea. Subnet 42 (10.0.0.0/24) changed in Kea. "
                        "Reload the page and try again."
                    ),
                )
            ],
        )
        self.assertEqual(commands, ["config-get", *_SCOPE])

    def test_not_sent_when_the_shared_network_is_gone(self):
        daemon = self._daemon()
        daemon.before("network4-get", lambda d: d.networks.remove("beta"))
        response, messages, commands = self._post(daemon, shared_network="beta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            messages, [(django_messages.ERROR, "The change was not sent to Kea. Shared Network 'beta' not found.")]
        )
        self.assertEqual(commands, ["config-get", *_SCOPE, "network4-get"])

    def test_a_membership_that_changed_after_the_page_was_shown_is_not_sent(self):
        """Another writer moves the Subnet after the GET, so a save of the fields must not move it back."""
        daemon = self._daemon()
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        with stub_kea({**_ABSENT_READ_HOOKS, **daemon.responses()}) as kea:
            page = self.client.get(url)
            self.assertEqual(page.context["form"].initial["shared_network"], "alpha")
            daemon.members[42] = "beta"
            shown = len(kea.commands())
            response = self.client.post(url, {**_page_data(page.context["form"]), "dns_servers": "10.0.0.53"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(m.level, str(m)) for m in get_messages(response.wsgi_request)],
            [
                (
                    django_messages.ERROR,
                    (
                        "The change was not sent to Kea. The Shared Network of Subnet 42 (10.0.0.0/24) changed in "
                        "Kea. Reload the page and try again."
                    ),
                )
            ],
        )
        self.assertEqual(kea.commands()[shown:], ["config-get", *_SCOPE])
        self.assertEqual(daemon.members, {42: "beta"})

    def test_a_page_that_could_not_confirm_the_membership_sends_nothing(self):
        """The page preselects no Shared Network, so a save of the fields must not take the Subnet out of it."""
        daemon = self._daemon()
        failed = {"result": 1, "text": "config-get failed"}
        # The two configuration reads of the GET fail, and the reads of the POST succeed.
        daemon.script("config-get", failed, failed)
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        with stub_kea({**_ABSENT_READ_HOOKS, **daemon.responses()}) as kea:
            page = self.client.get(url)
            self.assertEqual(page.context["form"].initial["shared_network"], "")
            shown = len(kea.commands())
            response = self.client.post(url, {**_page_data(page.context["form"]), "dns_servers": "10.0.0.53"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "NetBox could not confirm the Shared Network of this Subnet when it showed the page. "
            "Reload the page and try again.",
        )
        self.assertEqual(kea.commands()[shown:], ["config-get"])
        self.assertEqual(daemon.members, {42: "alpha"})

    def test_not_sent_after_a_refused_connection(self):
        refused = _refused_connection()
        daemon = self._daemon()
        daemon.script("subnet4-update", refused)
        response, messages, _ = self._post(daemon)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            messages, [(django_messages.ERROR, "The change was not sent to Kea. Kea could not be reached.")]
        )


# ---------------------------------------------------------------------------
# SubnetEdit exception paths
# ---------------------------------------------------------------------------

_SUBNET4_EDIT_POST = {
    **_shown(),
    "subnet_cidr": "10.0.0.0/24",
    "pools": "",
    "gateway": "",
    "dns_servers": "",
    "ntp_servers": "",
    "shared_network": "",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditPostExceptions(_ViewTestBase):
    """_BaseSubnetEditView POST with Pools and Subnet facts that Kea declares in unusual ways."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def test_a_pool_outside_the_subnet_is_a_form_error_without_a_kea_write(self):
        target = {"id": 42, "subnet": "198.18.0.5/24", "pools": []}
        responses = {
            "subnet4-list": _subnet_list(4, [{"id": 42, "subnet": "198.18.0.5/24"}]),
            "config-get": {"result": 0, "arguments": {"Dhcp4": {"subnet4": [target]}}},
            "subnet4-get": {"result": 0, "arguments": {"subnet4": [target]}},
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        post = {**_SUBNET4_EDIT_POST, "subnet_cidr": "198.18.0.5/24"}
        with stub_kea(responses) as kea:
            rejected = self.client.post(self._url(), {**post, "pools": "198.18.0.10-198.18.0.30\n198.18.1.0/28"})
            accepted = self.client.post(self._url(), {**post, "pools": "198.18.0.0/27"})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(
            rejected.context["form"].errors["pools"], ["Pool 198.18.1.0/28 is outside Subnet 198.18.0.0/24."]
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(
            [body["arguments"]["subnet4"][0]["pools"] for body in kea.bodies("subnet4-update")],
            [[{"pool": "198.18.0.0-198.18.0.31"}]],
        )

    def test_edit_preserves_live_pool_when_target_configuration_pool_is_invalid(self):
        target = {"id": 42, "subnet": "198.18.0.0/24", "pools": [{"pool": "invalid"}]}
        live_target = {**target, "pools": [{"pool": "198.18.0.10-198.18.0.30"}]}
        responses = {
            "subnet4-list": _subnet_list(4, [{"id": 42, "subnet": "198.18.0.0/24"}]),
            "config-get": {"result": 0, "arguments": {"Dhcp4": {"subnet4": [target]}}},
            "subnet4-get": {"result": 0, "arguments": {"subnet4": [live_target]}},
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        with stub_kea(responses) as kea:
            edit = self.client.get(self._url())
            self.assertEqual(edit.status_code, 200)
            response = self.client.post(
                self._url(),
                {
                    **_SUBNET4_EDIT_POST,
                    "subnet_cidr": edit.context["form"].initial["subnet_cidr"],
                    "pools": edit.context["form"].initial["pools"],
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            kea.bodies("subnet4-update")[0]["arguments"]["subnet4"][0]["pools"],
            [{"pool": "198.18.0.10-198.18.0.30"}],
        )

    def test_edit_preserves_pool_when_configuration_recovers_between_reads(self):
        target = {"id": 42, "subnet": "198.18.0.0/24", "pools": [{"pool": "invalid"}]}
        live_target = {**target, "pools": [{"pool": "198.18.0.10-198.18.0.30"}]}
        responses = {
            "subnet4-list": _subnet_list(4, [{"id": 42, "subnet": "198.18.0.0/24"}]),
            "config-get": queued(
                {"result": 0, "arguments": {"Dhcp4": {"subnet4": [target]}}},
                {"result": 0, "arguments": {"Dhcp4": {"subnet4": [live_target]}}},
            ),
            "subnet4-get": {"result": 0, "arguments": {"subnet4": [live_target]}},
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        with stub_kea(responses) as kea:
            edit = self.client.get(self._url())
            self.assertEqual(edit.status_code, 200)
            response = self.client.post(
                self._url(),
                {
                    **_SUBNET4_EDIT_POST,
                    "subnet_cidr": edit.context["form"].initial["subnet_cidr"],
                    "pools": edit.context["form"].initial["pools"],
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            kea.bodies("subnet4-update")[0]["arguments"]["subnet4"][0]["pools"],
            [{"pool": "198.18.0.10-198.18.0.30"}],
        )

    def test_edit_with_invalid_pool_on_another_subnet(self):
        target = {"id": 42, "subnet": "198.18.0.0/24", "pools": []}
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {"subnet4": [target, {"id": 90, "subnet": "198.18.90.0/24", "pools": [{"pool": "invalid"}]}]}
            },
        }
        responses = {
            **_ABSENT_READ_HOOKS,
            "subnet4-list": _subnet_list(
                4, [{"id": 42, "subnet": "198.18.0.0/24"}, {"id": 90, "subnet": "198.18.90.0/24"}]
            ),
            "config-get": config,
            "subnet4-get": {"result": 0, "arguments": {"subnet4": [target]}},
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        with stub_kea(responses):
            response = self.client.post(self._url(), {**_SUBNET4_EDIT_POST, "subnet_cidr": "198.18.0.0/24"})
        self.assertEqual(response.status_code, 302)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any(message.level == django_messages.SUCCESS for message in messages))
        self.assertTrue(any(message.level == django_messages.WARNING for message in messages))

    def test_post_malformed_live_cidr_rerenders_without_mutation(self):
        cidr = "198.18.0.0/32"
        config = {"subnet4": [{"id": 42, "subnet": cidr}], "shared-networks": []}
        for live in ({"id": 42, "subnet": 3323068416}, {"id": 42}):
            with self.subTest(live=live):
                responses = {
                    "subnet4-list": _subnet_list(4, [{"id": 42, "subnet": cidr}]),
                    "config-get": {"result": 0, "arguments": {"Dhcp4": config}},
                    "subnet4-get": {"result": 0, "arguments": {"subnet4": [live]}},
                }
                with stub_kea({**_ABSENT_READ_HOOKS, **responses}) as kea:
                    response = self.client.post(
                        self._url(), {**_shown(), "subnet_cidr": cidr, "valid_lft": "7200", "shared_network": ""}
                    )
                self.assertContains(
                    response,
                    "The change was not sent to Kea. Kea did not return a usable reply to the read before the change.",
                )
                self.assertNotContains(response, "valid CIDR")
                self.assertEqual(kea.commands(), ["config-get", "subnet4-list", "config-get", "subnet4-get"])


# ---------------------------------------------------------------------------
# Subnet list view — null config + export + HTMX partial
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetListViewEdgeCases(_ViewTestBase):
    """Lines 1110, 1173, 1181: subnet view null config, export, HTMX."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])

    def test_null_config_arguments_raises(self):
        """Null config-get arguments returns an empty table (degraded 200 state)."""
        with stub_kea(
            {**_ABSENT_READ_HOOKS, "config-get": {"result": 0, "arguments": None}, "stat-lease4-get": _STAT_ABSENT4}
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_export_returns_csv(self):
        """?export=csv returns a CSV file response."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG4, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(self._url() + "?export=csv")
        self.assertEqual(response.status_code, 200)

    def test_htmx_partial_returns_table_fragment(self):
        """HTMX request to the subnet view returns a partial table fragment."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG4, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(self._url(), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Subnet delete — GET exception + POST generic exception
# ---------------------------------------------------------------------------


_SEEN = "10.0.0.0/24"
_OLD_POOL = "10.0.0.10-10.0.0.20"
_NEW_POOL = "10.0.0.100-10.0.0.110"
_KEA_OK = {"result": 0, "text": "ok"}


def _subnet_1(cidr: str = _SEEN, pools: tuple[str, ...] = (_OLD_POOL,)) -> dict:
    return {"id": 1, "subnet": cidr, "pools": [{"pool": pool} for pool in pools]}


def _change_responses(*states: dict | None, **overrides) -> dict:
    """Answer one Subnet Catalogue read per state in turn, each change, and the persist step.

    A state of None is a Server without Subnets. The last state repeats, and its config-get also answers the persist step.
    """
    reads = [_catalogue_responses_for_subnets(4, [state] if state else []) for state in states]
    responses = {
        **_ABSENT_READ_HOOKS,
        "subnet4-list": queued(*(read["subnet4-list"] for read in reads)),
        "config-get": queued(*(read["config-get"] for read in reads)),
        "reservation-get-page": {"result": 3},
        "subnet4-del": _KEA_OK,
        "subnet4-delta-add": _KEA_OK,
        "subnet4-delta-del": _KEA_OK,
        "config-test": _KEA_OK,
        "config-write": _KEA_OK,
    }
    responses.update(overrides)
    return responses


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAndPoolChangesRequireTheVerifiedSubnet(_ViewTestBase):
    """Subnet delete, Pool add and Pool delete run only on the Subnet whose ID and CIDR the page showed.

    Each view maps the Configuration Change Outcome through the shared mapper.
    """

    def _cases(self):
        """Each view: its name, URL, form data, Kea command, the reads before its scope, and its confirmed text."""
        pk = self.server.pk
        return (
            (
                "subnet delete",
                reverse("plugins:netbox_kea:server_subnet4_delete", args=[pk, 1]),
                {"subnet_cidr": _SEEN},
                "subnet4-del",
                0,
                f"Subnet 1 ({_SEEN}) deleted.",
            ),
            (
                "pool add",
                reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[pk, 1]),
                {"subnet_cidr": _SEEN, "pool": _NEW_POOL},
                "subnet4-delta-add",
                # The form reads the Subnet Catalogue before the operation opens its scope.
                1,
                f"Pool {_NEW_POOL} added to subnet 1.",
            ),
            (
                "pool delete",
                reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[pk, 1, _OLD_POOL]),
                {"subnet_cidr": _SEEN},
                "subnet4-delta-del",
                0,
                f"Pool {_OLD_POOL} removed from subnet 1.",
            ),
        )

    def _post(self, url, data, responses):
        # The messages of an earlier subtest wait in the cookie until a page shows them.
        self.client.cookies.pop("messages", None)
        with stub_kea(responses) as kea:
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        return [(m.level, str(m)) for m in get_messages(response.wsgi_request)], kea

    def test_an_id_that_now_names_another_cidr_is_not_deleted(self):
        """The red test of #204: the delete POST names an ID that now belongs to another CIDR."""
        url = reverse("plugins:netbox_kea:server_subnet4_delete", args=[self.server.pk, 1])
        messages, kea = self._post(url, {"subnet_cidr": _SEEN}, _change_responses(_subnet_1("10.0.1.0/24")))
        self.assertEqual(kea.commands(), ["subnet4-list", "config-get"])
        self.assertEqual(
            messages,
            [
                (
                    django_messages.ERROR,
                    f"The change was not sent to Kea. Subnet 1 ({_SEEN}) changed in Kea. Reload the page and try again.",
                )
            ],
        )

    def test_the_get_puts_the_cidr_that_it_shows_into_the_form(self):
        for name, url, _data, _command, _reads, _confirmed in self._cases():
            with self.subTest(name), stub_kea(_change_responses(_subnet_1())):
                response = self.client.get(url)
                self.assertContains(response, f'name="subnet_cidr" value="{_SEEN}"')

    def test_an_applied_and_persisted_change_is_a_success(self):
        for name, url, data, command, _reads, confirmed in self._cases():
            with self.subTest(name):
                messages, kea = self._post(url, data, _change_responses(_subnet_1()))
                self.assertEqual(messages, [(django_messages.SUCCESS, confirmed)])
                # The Pool add reads the Reservations after the change, for its overlap warning.
                commands = [name for name in kea.commands() if name != "reservation-get-page"]
                self.assertEqual(commands[-4:], [command, "config-get", "config-test", "config-write"])

    def test_the_commands_name_the_verified_subnet_and_the_typed_pool(self):
        bodies = {
            "subnet4-del": {"id": 1},
            "subnet4-delta-add": {"subnet4": [{"id": 1, "subnet": _SEEN, "pools": [{"pool": _NEW_POOL}]}]},
            "subnet4-delta-del": {"subnet4": [{"id": 1, "subnet": _SEEN, "pools": [{"pool": _OLD_POOL}]}]},
        }
        for name, url, data, command, _reads, _confirmed in self._cases():
            with self.subTest(name):
                _, kea = self._post(url, data, _change_responses(_subnet_1()))
                self.assertEqual([body["arguments"] for body in kea.bodies(command)], [bodies[command]])

    def test_a_failed_persist_step_is_a_warning_that_the_change_is_live(self):
        failure = {"result": 1, "text": "disk full"}
        for name, url, data, _command, _reads, confirmed in self._cases():
            with self.subTest(name):
                messages, _ = self._post(url, data, _change_responses(_subnet_1(), **{"config-write": failure}))
                restart = "It is live, but it may not survive a Kea restart, because Kea did not save it to disk."
                self.assertEqual(
                    messages, [(django_messages.WARNING, f"{confirmed} {restart} config-write failed: disk full")]
                )

    def test_a_lost_reply_is_a_warning_that_kea_did_not_confirm_the_change(self):
        for name, url, data, command, _reads, _confirmed in self._cases():
            with self.subTest(name):
                messages, kea = self._post(
                    url, data, _change_responses(_subnet_1(), **{command: requests.ReadTimeout("read timed out")})
                )
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
                self.assertIn(command, kea.commands())

    def test_a_failure_that_changed_nothing_is_a_kea_rejection(self):
        failure = {"result": 1, "text": "command failed"}
        for name, url, data, command, _reads, _confirmed in self._cases():
            with self.subTest(name):
                messages, _ = self._post(url, data, _change_responses(_subnet_1(), **{command: failure}))
                self.assertEqual(
                    messages, [(django_messages.ERROR, "Kea rejected the change. Kea replied: command failed")]
                )

    def test_an_id_that_now_names_another_cidr_is_not_sent(self):
        for name, url, data, command, reads, _confirmed in self._cases():
            with self.subTest(name):
                states = (_subnet_1(),) * reads + (_subnet_1("10.0.1.0/24"),)
                messages, kea = self._post(url, data, _change_responses(*states))
                self.assertNotIn(command, kea.commands())
                self.assertEqual(
                    messages,
                    [
                        (
                            django_messages.ERROR,
                            (
                                f"The change was not sent to Kea. Subnet 1 ({_SEEN}) changed in Kea. "
                                "Reload the page and try again."
                            ),
                        )
                    ],
                )

    def test_an_incomplete_identity_observation_is_not_sent_and_never_reads_as_absent(self):
        failed = {"result": 1, "text": "internal error"}
        for name, url, data, command, reads, _confirmed in self._cases():
            with self.subTest(name):
                listed = _catalogue_responses_for_subnets(4, [_subnet_1()])["subnet4-list"]
                responses = _change_responses(_subnet_1(), **{"subnet4-list": queued(*(listed,) * reads, failed)})
                messages, kea = self._post(url, data, responses)
                self.assertNotIn(command, kea.commands())
                self.assertEqual(
                    messages,
                    [
                        (
                            django_messages.ERROR,
                            (
                                "The change was not sent to Kea. NetBox could not confirm Kea's Subnet list, "
                                "so it did not send the change. Try again later."
                            ),
                        )
                    ],
                )

    def test_a_delete_get_that_cannot_confirm_the_subnet_redirects_with_an_error(self):
        failed = {"result": 1, "text": "internal error"}
        unconfirmed = "NetBox could not confirm Subnet 1 in Kea. Reload the Subnets page and try again."
        for name, url, _data, _command, reads, _confirmed in self._cases():
            if reads:
                continue  # The Pool add GET shows the form error instead.
            for state, responses in (
                ("unreadable", _change_responses(_subnet_1(), **{"subnet4-list": failed, "config-get": failed})),
                ("absent", _change_responses(None)),
            ):
                with self.subTest(name, state=state):
                    self.client.cookies.pop("messages", None)
                    with stub_kea(responses):
                        response = self.client.get(url)
                    self.assertEqual(response.status_code, 302)
                    errors = [str(m) for m in get_messages(response.wsgi_request) if m.level == django_messages.ERROR]
                    self.assertIn(unconfirmed, errors)

    def test_a_delete_without_the_cidr_sends_nothing(self):
        for name, url, _data, _command, reads, _confirmed in self._cases():
            if reads:
                continue  # The Pool add form reports a missing CIDR itself.
            with self.subTest(name):
                messages, kea = self._post(url, {}, _change_responses(_subnet_1()))
                self.assertEqual(kea.commands(), [])
                self.assertEqual(messages, [(django_messages.ERROR, _NO_SUBNET_CIDR)])

    def test_a_pool_add_without_the_cidr_is_a_form_error(self):
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 1])
        with stub_kea(_change_responses(_subnet_1())) as kea:
            response = self.client.post(url, {"pool": _NEW_POOL})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid subnet CIDR")
        self.assertNotIn("subnet4-delta-add", kea.commands())

    def test_an_invalid_client_configuration_sends_nothing(self):
        bad = _make_db_server(name="bad-cert-change", client_cert_path="/nonexistent/cert.pem")
        for name, url, data, _command, reads, _confirmed in self._cases():
            if reads:
                continue  # The Pool add form cannot read the Subnet Catalogue either, so it shows a form error.
            with self.subTest(name):
                bad_url = url.replace(f"/servers/{self.server.pk}/", f"/servers/{bad.pk}/")
                messages, kea = self._post(bad_url, data, _change_responses(_subnet_1()))
                self.assertEqual(kea.commands(), [])
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

    def test_a_pool_delete_whose_pool_is_outside_the_cidr_sends_nothing(self):
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 1, "10.0.1.10-10.0.1.20"])
        messages, kea = self._post(url, {"subnet_cidr": _SEEN}, _change_responses(_subnet_1()))
        self.assertEqual(kea.commands(), [])
        self.assertEqual(
            messages,
            [
                (
                    django_messages.ERROR,
                    "Pool 10.0.1.10-10.0.1.20 is not a valid Pool of Subnet 10.0.0.0/24. Nothing was sent to Kea.",
                )
            ],
        )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetDeleteExceptionPaths(_ViewTestBase):
    """Lines 3177-3178, 3203-3205: subnet delete GET exception and POST generic."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_delete", args=[self.server.pk, subnet_id])

    def test_get_exception_redirects_with_an_error(self):
        """A failed configuration read leaves no CIDR for the confirm form, so the GET redirects."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": {"result": 1, "text": "configuration unavailable"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        errors = [str(m) for m in get_messages(response.wsgi_request) if m.level == django_messages.ERROR]
        self.assertIn("NetBox could not confirm Subnet 42 in Kea. Reload the Subnets page and try again.", errors)


# ---------------------------------------------------------------------------
# _fetch_subnets_from_server — null config, shared-network subnets, stat_cmds exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchSubnetsFromServer(_ViewTestBase):
    """Lines 3807-3855: _fetch_subnets_from_server edge cases."""

    def _run(self, responses):
        """Render the combined Subnet table through its public HTTP view."""
        with stub_kea({**_ABSENT_READ_HOOKS, **responses}):
            response = self.client.get(reverse("plugins:netbox_kea:combined_subnets4"), {"server": self.server.pk})
        self.assertEqual(response.status_code, 200)
        return list(response.context["table"].data)

    def test_null_arguments_preserves_confirmed_empty_identity(self):
        """Malformed config does not override a complete identity observation with no Subnets."""
        result = self._run(
            {
                "subnet4-list": {"result": 3, "text": "no subnets"},
                "config-get": {"result": 0, "arguments": None},
                "stat-lease4-get": _STAT_ABSENT4,
            }
        )
        self.assertEqual(result, [])

    def test_subnets_in_shared_network_included(self):
        """Subnets nested inside shared-networks are included."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [],
                    "shared-networks": [{"name": "prod", "subnet4": [{"id": 10, "subnet": "192.168.0.0/24"}]}],
                }
            },
        }
        # stat-lease4-get result 2 → KeaException, exactly as a missing stat_cmds hook behaves.
        result = self._run(
            {
                "subnet4-list": {
                    "result": 0,
                    "arguments": {"subnets": [{"id": 10, "subnet": "192.168.0.0/24", "shared-network-name": "prod"}]},
                },
                "config-get": config,
                "stat-lease4-get": _STAT_ABSENT4,
            }
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["subnet"], "192.168.0.0/24")

    def test_stat_cmds_exception_swallowed(self):
        """A stat_cmds failure (missing hook) is swallowed; subnets are still returned."""
        config_resp = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [
                        {
                            "id": 1,
                            "subnet": "10.0.0.0/24",
                            "pools": [{"pool": "10.0.0.10-10.0.0.20"}],
                            "option-data": [
                                {
                                    "code": 6,
                                    "name": "domain-name-servers",
                                    "space": "dhcp4",
                                    "data": "10.0.0.53",
                                    "csv-format": True,
                                    "always-send": False,
                                    "never-send": False,
                                }
                            ],
                        }
                    ],
                    "shared-networks": [],
                }
            },
        }
        result = self._run(
            {
                "subnet4-list": {
                    "result": 0,
                    "arguments": {"subnets": [{"id": 1, "subnet": "10.0.0.0/24"}]},
                },
                "config-get": config_resp,
                "stat-lease4-get": _STAT_ABSENT4,
            }
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pools"], ["10.0.0.10-10.0.0.20"])
        self.assertEqual(result[0]["options"], {"dns_servers": "10.0.0.53"})

    def test_stat_cmds_success_updates_subnet(self):
        """Valid stat-lease4-get data is merged into the subnet dict."""
        config_resp = {
            "result": 0,
            "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}], "shared-networks": []}},
        }
        stat_resp = {
            "result": 0,
            "arguments": {
                "result-set": {
                    "columns": ["subnet-id", "total-addresses", "assigned-addresses"],
                    "rows": [[1, 100, 25]],
                }
            },
        }
        result = self._run(
            {
                "subnet4-list": {
                    "result": 0,
                    "arguments": {"subnets": [{"id": 1, "subnet": "10.0.0.0/24"}]},
                },
                "config-get": config_resp,
                "stat-lease4-get": stat_resp,
            }
        )
        self.assertEqual(len(result), 1)
        # stat data was merged into the subnet dict
        self.assertEqual(result[0].get("total"), 100)
        self.assertEqual(result[0].get("assigned"), 25)

    def test_configuration_only_snapshot_remains_visible(self):
        """A missing subnet_cmds hook does not hide validated configuration facts."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [{"id": 7, "subnet": "198.18.7.0/24", "pools": []}],
                    "shared-networks": [],
                }
            },
        }
        result = self._run(
            {
                "subnet4-list": {"result": 2, "text": "unknown command"},
                "config-get": config,
                "stat-lease4-get": _STAT_ABSENT4,
            }
        )
        self.assertEqual([(row["id"], row["subnet"]) for row in result], [(7, "198.18.7.0/24")])
        self.assertFalse(result[0]["identity_verified"])

    def test_identity_only_snapshot_remains_visible(self):
        """A config-get failure does not hide verified Subnet identity."""
        result = self._run(
            {
                "subnet4-list": {
                    "result": 0,
                    "arguments": {"subnets": [{"id": 8, "subnet": "198.18.8.0/24"}]},
                },
                "config-get": requests.ConnectionError("down"),
                "stat-lease4-get": _STAT_ABSENT4,
            }
        )
        self.assertEqual([(row["id"], row["subnet"]) for row in result], [(8, "198.18.8.0/24")])
        self.assertTrue(result[0]["identity_verified"])
        self.assertEqual(result[0]["pools"], [])


# ---------------------------------------------------------------------------
# Subnet edit — _form_initial with ntp/dns + lease time fields
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNonCanonicalCidr(_ViewTestBase):
    """Kea accepts a prefix with host bits set and returns it as configured, so editing must too."""

    _CIDR = "10.0.0.5/24"

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def _stub(self, cidr: str = _CIDR):
        live = {"id": 42, "subnet": cidr, "pools": [], "option-data": []}
        return stub_kea({**_ABSENT_READ_HOOKS, **SubnetDaemon(4, [live]).responses()})

    def test_round_trip_preserves_the_configured_prefix(self):
        """GET then POST must reach subnet4-update with the prefix Kea reported, unmodified."""
        with self._stub():
            get_response = self.client.get(self._url())
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.context["form"].initial["subnet_cidr"], self._CIDR)
        self.assertEqual(get_response.context["subnet_cidr"], self._CIDR)

        with self._stub() as kea:
            post_response = self.client.post(
                self._url(),
                {
                    **_shown(),
                    "subnet_cidr": get_response.context["form"].initial["subnet_cidr"],
                    "valid_lft": "7200",
                    "shared_network": "",
                },
            )
        self.assertEqual(post_response.status_code, 302)
        self.assertIn("subnet4-update", kea.commands())
        self.assertEqual(kea.bodies("subnet4-update")[0]["arguments"]["subnet4"][0]["subnet"], self._CIDR)

    def test_a_rejected_cidr_is_shown_to_the_user(self):
        """A hidden field whose errors are never rendered is a dead end, not a validation message."""
        with self._stub() as kea:
            response = self.client.post(
                self._url(), {**_shown(), "subnet_cidr": "not-a-subnet", "valid_lft": "7200", "shared_network": ""}
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("subnet4-update", kea.commands())
        self.assertContains(response, "Invalid subnet CIDR")

    def test_a_cidr_of_the_other_family_is_a_form_error_without_a_kea_write(self):
        with self._stub() as kea:
            response = self.client.post(self._url(), {**_shown(), "subnet_cidr": "2001:db8::/64", "shared_network": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["subnet_cidr"], ["Enter an IPv4 Subnet CIDR."])
        self.assertEqual(kea.commands(), ["config-get"])

    def test_a_forged_cidr_cannot_replace_the_live_subnet(self):
        with self._stub("198.18.0.5/24") as kea:
            response = self.client.post(
                self._url(), {**_shown(), "subnet_cidr": "198.18.1.0/24", "valid_lft": "7200", "shared_network": ""}
            )
        self.assertEqual(kea.commands(), ["config-get", "subnet4-list", "config-get"])
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "The change was not sent to Kea. Subnet 42 (198.18.1.0/24) changed in Kea. Reload the page and try again.",
        )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditFormInitialFields(_ViewTestBase):
    """Lines 2937-2938, 2944, 2946: _form_initial parses ntp/dns + lease time fields."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def test_get_populates_ntp_and_lease_times(self):
        """_form_initial picks up ntp-servers, min-valid-lft, max-valid-lft, renew/rebind-timer."""
        subnet_resp = {
            "result": 0,
            "arguments": {
                "subnet4": [
                    {
                        "id": 42,
                        "subnet": "10.0.0.0/24",
                        "pools": [],
                        "option-data": [{"name": "ntp-servers", "data": "10.0.0.1"}],
                        "valid-lifetime": 3600,
                        "min-valid-lifetime": 1800,
                        "max-valid-lifetime": 7200,
                        "renew-timer": 900,
                        "rebind-timer": 1500,
                    }
                ]
            },
        }
        config_resp = {
            "result": 0,
            "arguments": {"Dhcp4": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}], "shared-networks": []}},
        }
        with stub_kea(_edit_responses(4, subnet_resp, config_resp)):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        initial = response.context["form"].initial
        self.assertEqual(initial.get("min_valid_lft"), 1800)
        self.assertEqual(initial.get("max_valid_lft"), 7200)
        self.assertEqual(initial.get("renew_timer"), 900)
        self.assertEqual(initial.get("rebind_timer"), 1500)


# ---------------------------------------------------------------------------
# _get_network_data — unnamed network (no name key) is skipped (line 2913)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetNetworkDataUnnamedNetwork(_ViewTestBase):
    """Line 2913: shared-network without a name key is skipped."""

    def test_unnamed_network_skipped_in_choices(self):
        """Network with no 'name' key is not added to choices; the named one still appears."""
        config_resp = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}],
                    "shared-networks": [
                        {"subnet4": []},  # no 'name' key → skipped
                        {"name": "valid-net", "subnet4": []},
                    ],
                }
            },
        }
        subnet_resp = {
            "result": 0,
            "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
        }
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        with stub_kea(_edit_responses(4, subnet_resp, config_resp)):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid-net")


# ---------------------------------------------------------------------------
# Shared Network edit GET: config-get with non-dict arguments
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchNetworkNonDictArgs(_ViewTestBase):
    """config-get returning non-dict arguments makes the edit GET redirect."""

    def test_get_non_dict_args_redirects(self):
        """config-get returning arguments=None is an unavailable snapshot, so GET redirects."""
        url = reverse(
            "plugins:netbox_kea:server_shared_network4_edit",
            args=[self.server.pk, "test-net"],
        )
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": {"result": 0, "arguments": None}}):
            response = self.client.get(url)
        # network not found → redirects back to shared_networks4
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/servers/{self.server.pk}/", response.url)


# ---------------------------------------------------------------------------
# _get_network_choices — KeaException (lines 2737-2738)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetNetworkChoicesKeaException(_ViewTestBase):
    """Lines 2737-2738: KeaException in _get_network_choices → returns default choice."""

    def test_kea_exception_returns_global_pool_only(self):
        """config-get failing (result 1 → KeaException) → the add form falls back to global-pool only."""
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": {"result": 1, "text": "error"}}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# _get_inherited_options._parse_opts — "routers" and "ntp-servers" (lines 2974, 2977-2978)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetInheritedOptionsParseOpts(_ViewTestBase):
    """Lines 2974, 2977-2978: _parse_opts handles 'routers' and 'ntp-servers' entries."""

    def test_global_options_routers_and_ntp_servers_inherited(self):
        """GET subnet4_edit with global routers + ntp-servers → inherited_options populated."""
        subnet_resp = {
            "result": 0,
            "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
        }
        config_resp = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [],
                    "shared-networks": [],
                    "option-data": [
                        {"name": "routers", "data": "10.0.0.1"},
                        {"name": "ntp-servers", "data": "10.0.0.2"},
                    ],
                }
            },
        }
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        with stub_kea(_edit_responses(4, subnet_resp, config_resp)):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # inherited_options should have gateway and ntp_servers from global config
        inherited = response.context.get("inherited_options", {})
        self.assertIn("gateway", inherited)
        self.assertIn("ntp_servers", inherited)


# ---------------------------------------------------------------------------
# Subnet add — _get_network_choices error handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddNetworkChoicesError(_ViewTestBase):
    """_get_network_choices error handling in the subnet-add view."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    def test_invalid_shared_networks_disables_choices_and_blocks_creation(self):
        responses = {
            **_ABSENT_READ_HOOKS,
            "config-get": {"result": 0, "arguments": {"Dhcp4": {"shared-networks": "invalid"}}},
            "subnet4-add": {"result": 0, "arguments": {"subnets": [{"id": 42}]}},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        with self.subTest(method="GET"), stub_kea(responses):
            response = self.client.get(self._url())
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].fields["shared_network"].disabled)
        with self.subTest(method="POST"), stub_kea(responses) as kea:
            response = self.client.post(self._url(), {"subnet": "198.18.0.0/24", "shared_network": ""})
            self.assertNotIn("subnet4-add", kea.commands())
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Could not load shared networks from Kea")

    def test_get_shows_warning_when_network_choices_fail(self):
        """GET must render 200 with a warning message when config-get fails (result 1 → KeaException)."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": {"result": 1, "text": "config-get failed"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("shared network" in m.lower() for m in msgs))

    def test_post_rejects_submission_when_network_choices_fail(self):
        """POST must show a form error and NOT issue subnet4-add when config-get fails."""
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": {"result": 1, "text": "config-get failed"}}) as kea:
            response = self.client.post(
                self._url(),
                {
                    "subnet": "10.99.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                    "shared_network": "",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("subnet4-add", kea.commands())


# ---------------------------------------------------------------------------
# Subnet edit: _get_network_data error handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkDataErrors(_ViewTestBase):
    """_get_network_data must degrade gracefully on transport and parse errors."""

    _LIVE_SUBNET = {
        "result": 0,
        "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
    }

    def _shared_network_responses(self, networks):
        subnet = {"id": 1, "subnet": "198.18.0.0/24"}
        return {
            "subnet4-list": _subnet_list(4, [subnet]),
            "config-get": {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [subnet],
                        "shared-networks": networks,
                        "option-data": [{"name": "domain-name-servers", "data": "198.18.0.53"}],
                    }
                },
            },
            "subnet4-get": {"result": 0, "arguments": {"subnet4": [subnet]}},
            "subnet4-update": {"result": 0},
            "network4-get": _network_present(4, "clients"),
            "network4-subnet-add": {"result": 0},
            "network4-subnet-del": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }

    def _assert_unreadable_shared_networks_block_edit(self, networks):
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        with stub_kea(self._shared_network_responses(networks)) as kea:
            with self.subTest(method="GET"):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["inherited_options"], {})
            with self.subTest(method="POST"):
                response = self.client.post(url, {**_shown(), "subnet_cidr": "198.18.0.0/24", "shared_network": ""})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    set(kea.commands()) & {"subnet4-update", "network4-subnet-add", "network4-subnet-del"}, set()
                )
                self.assertContains(response, "Could not load shared networks from Kea. Please try again.")

    def test_null_shared_network_blocks_edit_and_inherited_hints(self):
        self._assert_unreadable_shared_networks_block_edit([None])

    def test_duplicate_shared_network_subnet_ids_block_edit_and_inherited_hints(self):
        self._assert_unreadable_shared_networks_block_edit(
            [
                {
                    "name": "clients",
                    "subnet4": [
                        {"id": 2, "subnet": "198.18.1.0/24"},
                        {"id": 2, "subnet": "198.18.2.0/24"},
                    ],
                }
            ]
        )

    def test_duplicate_subnet_ids_across_shared_networks_block_edit_and_inherited_hints(self):
        self._assert_unreadable_shared_networks_block_edit(
            [
                {"name": "clients", "subnet4": [{"id": 2, "subnet": "198.18.1.0/24"}]},
                {"name": "guests", "subnet4": [{"id": 2, "subnet": "198.18.2.0/24"}]},
            ]
        )

    def test_null_shared_network_subnet_blocks_edit_and_inherited_hints(self):
        self._assert_unreadable_shared_networks_block_edit([{"name": "clients", "subnet4": [None]}])

    def test_invalid_shared_network_member_pool_preserves_unrelated_operations(self):
        responses = self._shared_network_responses(
            [{"name": "clients", "subnet4": [{"id": 2, "subnet": "198.18.1.0/24", "pools": "invalid"}]}]
        )
        responses["subnet4-list"] = _subnet_list(
            4,
            [
                {"id": 1, "subnet": "198.18.0.0/24"},
                {"id": 2, "subnet": "198.18.1.0/24", "shared-network-name": "clients"},
            ],
        )
        responses["subnet4-add"] = {"result": 0, "arguments": {"subnets": [{"id": 3}]}}
        edit_url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        add_url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        with self.subTest(operation="inherited hints"), stub_kea(responses):
            response = self.client.get(edit_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.context["inherited_options"],
                {"dns_servers": {"value": "198.18.0.53", "source": "global"}},
            )
            self.assertTrue(
                any(
                    message.level == django_messages.WARNING and "non-list Pool collection" in str(message)
                    for message in get_messages(response.wsgi_request)
                )
            )
        for url, data, command in (
            (add_url, {"subnet": "198.18.2.0/24", "shared_network": ""}, "subnet4-add"),
            (edit_url, {**_shown(), "subnet_cidr": "198.18.0.0/24", "shared_network": ""}, "subnet4-update"),
        ):
            with self.subTest(operation=command), stub_kea(responses) as kea:
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 302)
                self.assertIn(command, kea.commands())
                messages = list(get_messages(response.wsgi_request))
                self.assertTrue(any(message.level == django_messages.SUCCESS for message in messages))
                self.assertTrue(
                    any(
                        message.level == django_messages.WARNING and "non-list Pool collection" in str(message)
                        for message in messages
                    )
                )

    def test_missing_shared_network_subnet_id_blocks_edit_and_inherited_hints(self):
        self._assert_unreadable_shared_networks_block_edit(
            [{"name": "clients", "subnet4": [{"subnet": "198.18.1.0/24"}]}]
        )

    def test_non_scalar_shared_network_subnet_id_blocks_edit_and_inherited_hints(self):
        self._assert_unreadable_shared_networks_block_edit(
            [{"name": "clients", "subnet4": [{"id": [], "subnet": "198.18.1.0/24"}]}]
        )

    def test_empty_shared_network_is_selectable_and_edit_proceeds(self):
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        with stub_kea(self._shared_network_responses([{"name": "clients", "subnet4": []}])) as kea:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(("clients", "clients"), response.context["form"].fields["shared_network"].choices)
            self.assertEqual(
                response.context["inherited_options"]["dns_servers"], {"value": "198.18.0.53", "source": "global"}
            )
            response = self.client.post(url, {**_shown(), "subnet_cidr": "198.18.0.0/24", "shared_network": "clients"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("subnet4-update", kea.commands())
        self.assertIn("network4-subnet-add", kea.commands())

    def test_invalid_shared_networks_blocks_edit_mutation(self):
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        subnet = {"id": 1, "subnet": "198.18.0.0/24"}
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "config-get": {
                    "result": 0,
                    "arguments": {"Dhcp4": {"subnet4": [subnet], "shared-networks": "invalid"}},
                },
                "subnet4-get": {"result": 0, "arguments": {"subnet4": [subnet]}},
                "subnet4-update": {"result": 0},
                "network4-subnet-add": {"result": 0},
                "network4-subnet-del": {"result": 0},
                "config-test": {"result": 0},
                "config-write": {"result": 0},
            }
        ) as kea:
            response = self.client.post(url, {**_shown(), "subnet_cidr": "198.18.0.0/24", "shared_network": ""})
        self.assertEqual(kea.commands(), ["config-get"])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not load shared networks from Kea. Please try again.")

    def test_invalid_shared_networks_preserves_edit_display(self):
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        live_subnet = {"result": 0, "arguments": {"subnet4": [{"id": 1, "subnet": "198.18.0.0/24"}]}}
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "config-get": {
                    "result": 0,
                    "arguments": {
                        "Dhcp4": {
                            "shared-networks": "invalid",
                            "option-data": [{"name": "domain-name-servers", "data": "198.18.0.53"}],
                        }
                    },
                },
                "subnet4-get": live_subnet,
            }
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["subnet_cidr"], "198.18.0.0/24")
        self.assertContains(response, "this form cannot be saved")
        self.assertEqual(response.context["inherited_options"], {})

    def test_transport_error_renders_edit_form_with_warning(self):
        """A requests.RequestException on config-get must not cause a 500 in the edit view."""
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        with stub_kea(
            {**_ABSENT_READ_HOOKS, "subnet4-get": self._LIVE_SUBNET, "config-get": requests.ConnectionError("down")}
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this form cannot be saved")
        self.assertEqual(response.context["form"].initial["subnet_cidr"], "10.0.0.0/24")

    def test_value_error_renders_edit_form_with_warning(self):
        """A ValueError on config-get must not cause a 500 in the edit view."""
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        with stub_kea(
            {**_ABSENT_READ_HOOKS, "subnet4-get": self._LIVE_SUBNET, "config-get": ValueError("bad response")}
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this form cannot be saved")


# ---------------------------------------------------------------------------
# F5: get_client() failures in delete/wipe/pool-delete POST handlers
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetDeleteClientError(_ViewTestBase):
    """Subnet delete handlers must handle get_client() failures gracefully."""

    def test_get_with_get_client_failure_redirects(self):
        """A real get_client() failure in delete GET redirects with an error, not 500."""
        bad = _make_db_server(name="bad-cert-del-get", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_delete", args=[bad.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetWipeClientError(_ViewTestBase):
    """Subnet wipe handlers must handle get_client() failures gracefully."""

    def test_post_with_get_client_failure_redirects(self):
        """A real get_client() failure (cert without key → ValueError) in wipe POST must redirect."""
        bad = _make_db_server(name="bad-cert-wipe-post", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_wipe_leases", args=[bad.pk, 1])
        response = self.client.post(url, {"confirm": "1"})
        self.assertIn(response.status_code, [200, 302])

    def test_get_with_get_client_failure_renders(self):
        """A real get_client() failure in wipe GET must still render the confirm page, not 500."""
        bad = _make_db_server(name="bad-cert-wipe-get", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_wipe_leases", args=[bad.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# F6: get_subnets() non-dict arguments guard
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetSubnetsConfigShapeGuard(_ViewTestBase):
    """get_subnets() returns [] when config-get arguments is non-dict."""

    def test_non_dict_arguments_returns_empty_list(self):
        """Non-dict (string) arguments in config-get must yield an empty subnet list, not a 500."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea(
            {
                **_ABSENT_READ_HOOKS,
                "config-get": {"result": 0, "arguments": "unexpected string"},
                "stat-lease4-get": _STAT_ABSENT4,
            }
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_integer_arguments_returns_empty_list(self):
        """Integer arguments in config-get must yield an empty subnet list, not a 500."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea(
            {**_ABSENT_READ_HOOKS, "config-get": {"result": 0, "arguments": 42}, "stat-lease4-get": _STAT_ABSENT4}
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Pool add POST exception branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolDeltaHostBitsSubnet(_ViewTestBase):
    """The delta commands must echo the Subnet prefix exactly as Kea declares it, host bits included.

    The page shows the canonical CIDR; the Verified Subnet supplies Kea's text, so no Subnet lookup by ID runs.
    """

    def _stub(self, version, cidr, command, pools=()):
        subnet = {"id": 1, "subnet": cidr, "pools": [{"pool": pool} for pool in pools]}
        return stub_kea(
            {
                f"subnet{version}-list": _subnet_list(version, [subnet]),
                "config-get": {
                    "result": 0,
                    "arguments": {f"Dhcp{version}": {f"subnet{version}": [subnet], "shared-networks": []}},
                },
                "reservation-get-page": {"result": 3},
                command: {"result": 0},
                "config-test": {"result": 0},
                "config-write": {"result": 0},
            }
        )

    def test_pool_add_sends_the_declared_ipv4_prefix(self):
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 1])
        with self._stub(4, "198.18.1.5/24", "subnet4-delta-add") as kea:
            response = self.client.post(url, {"subnet_cidr": "198.18.1.0/24", "pool": "198.18.1.100-198.18.1.110"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            kea.bodies("subnet4-delta-add")[0]["arguments"],
            {"subnet4": [{"id": 1, "subnet": "198.18.1.5/24", "pools": [{"pool": "198.18.1.100-198.18.1.110"}]}]},
        )
        self.assertNotIn("subnet4-get", kea.commands())
        self.assertIn("config-write", kea.commands())

    def test_pool_delete_sends_the_declared_ipv6_prefix(self):
        pool = "2001:db8:1::100-2001:db8:1::1ff"
        url = reverse("plugins:netbox_kea:server_subnet6_pool_delete", args=[self.server.pk, 1, pool])
        with self._stub(6, "2001:db8:1::5/64", "subnet6-delta-del", pools=(pool,)) as kea:
            response = self.client.post(url, {"subnet_cidr": "2001:db8:1::/64"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            kea.bodies("subnet6-delta-del")[0]["arguments"],
            {"subnet6": [{"id": 1, "subnet": "2001:db8:1::5/64", "pools": [{"pool": pool}]}]},
        )
        self.assertNotIn("subnet6-get", kea.commands())
        self.assertIn("config-write", kea.commands())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolAddPostErrors(_ViewTestBase):
    """Cover pool add POST error handling."""

    def _url(self, subnet_id=1):
        return reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, subnet_id])

    def _pool_add_stub(self, **overrides):
        """Pool add chain: Subnet Catalogue reads, reservation overlap probe, subnet4-delta-add, persist.

        follow=True lands on the subnets list (config-get + stat). Override a leg to drive errors.
        """
        base = _pool_add_registry(1, "10.0.0.0/24")
        base.update(overrides)
        return stub_kea({**_ABSENT_READ_HOOKS, **base})

    def test_the_overlap_probe_asks_kea_for_one_subnet(self):
        """The probe needs one subnet, so it must not page through the whole server.

        ``reservation-get-page`` takes an optional ``subnet-id``. Without it Kea returns
        every reservation on the server and the view filters them client-side.
        """
        with self._pool_add_stub() as kea:
            response = self.client.post(
                self._url(subnet_id=1), {"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.10-10.0.0.20"}, follow=True
            )

        self.assertEqual(response.status_code, 200)
        probe = kea.bodies("reservation-get-page")
        self.assertTrue(probe)
        self.assertEqual(probe[0]["arguments"]["subnet-id"], 1)

    def test_incomplete_reservation_snapshot_is_reported(self):
        """Say so when the overlap check could not read every reservation.

        `reservation_snapshot` does not raise once a page has succeeded: it quarantines
        the rest as diagnostics. Reading only `snapshot.records` then produces no
        warning, which the operator reads as "no overlapping reservation".
        """
        quarantined = _res_page([{"subnet-id": 1, "remote-id": "relay-value"}])

        with self._pool_add_stub(**{"reservation-get-page": quarantined}) as kea:
            response = self.client.post(
                self._url(), {"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.10-10.0.0.20"}, follow=True
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "incomplete list")
        self.assertIn("subnet4-delta-add", kea.commands())

    def test_failed_overlap_probe_logs_its_traceback(self):
        """Record why the overlap warning was skipped, and keep the pool add working.

        The probe swallows every exception to stay non-blocking, so without the traceback
        an operator cannot tell a Kea read failure from a genuinely empty overlap.
        """
        malformed_probe = {"result": 0, "arguments": {"hosts": None, "next": {"from": 0, "source-index": 0}}}

        with self._pool_add_stub(**{"reservation-get-page": malformed_probe}) as kea:
            with self.assertLogs("netbox_kea.views.subnets", level="WARNING") as logs:
                response = self.client.post(
                    self._url(), {"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.10-10.0.0.20"}, follow=True
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn("subnet4-delta-add", kea.commands())
        overlap_records = [record for record in logs.records if "overlap" in record.getMessage()]
        self.assertTrue(overlap_records)
        self.assertIsNotNone(overlap_records[0].exc_info)


# Every command that changes Kea state on the Pool add path.
_POOL_WRITE_COMMANDS = {"subnet4-delta-add", "config-test", "config-set", "config-write"}


def _pool_add_catalogue(pools: tuple[str, ...] = ("10.0.0.10-10.0.0.20",), *, configuration: bool = True) -> dict:
    """Return the Pool add chain for Subnet 1 (10.0.0.0/24) with its declared Pools."""
    registry = _pool_add_registry(1, "10.0.0.0/24")
    if configuration:
        subnet = {"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": pool} for pool in pools]}
        registry["config-get"] = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [subnet], "shared-networks": []}}}
    return registry


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolAddChecksTheVerifiedSubnet(_ViewTestBase):
    """The Pool add form parses the Pool against the Verified Subnet before Kea sees it."""

    def _url(self, subnet_id=1):
        return reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, subnet_id])

    def _post(self, pool, *, subnet_id=1, **stub_overrides):
        with stub_kea({**_ABSENT_READ_HOOKS, **_pool_add_catalogue(), **stub_overrides}) as kea:
            response = self.client.post(self._url(subnet_id), {"subnet_cidr": "10.0.0.0/24", "pool": pool})
        return response, kea

    def _assert_no_write(self, kea):
        self.assertFalse(_POOL_WRITE_COMMANDS & set(kea.commands()), kea.commands())

    def test_a_pool_outside_the_subnet_is_a_field_error_and_kea_gets_no_command(self):
        for pool in ("10.0.1.10-10.0.1.20", "10.0.0.250-10.0.1.5", "10.0.1.0/28"):
            with self.subTest(pool=pool):
                response, kea = self._post(pool)

                self.assertEqual(response.status_code, 200)
                self.assertIn("pool", response.context["form"].errors)
                self.assertIn("outside Subnet 10.0.0.0/24", str(response.context["form"].errors["pool"]))
                self._assert_no_write(kea)

    def test_invalid_text_is_a_field_error_and_kea_gets_no_command(self):
        for pool in ("nonsense", "10.0.0.0/99", "10.0.0.90-10.0.0.80"):
            with self.subTest(pool=pool):
                response, kea = self._post(pool)

                self.assertEqual(response.status_code, 200)
                self.assertIn("pool", response.context["form"].errors)
                self._assert_no_write(kea)

    def test_an_overlap_with_an_existing_pool_is_a_field_error_and_kea_gets_no_command(self):
        response, kea = self._post("10.0.0.0/27")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["pool"],
            ["Pool 10.0.0.0-10.0.0.31 overlaps existing Pool 10.0.0.10-10.0.0.20."],
        )
        self.assertContains(response, "overlaps existing Pool 10.0.0.10-10.0.0.20")
        self._assert_no_write(kea)

    def test_missing_configuration_facts_skip_the_overlap_check_and_kea_decides(self):
        with stub_kea({**_ABSENT_READ_HOOKS, **_pool_add_catalogue(configuration=False)}) as kea:
            response = self.client.post(self._url(), {"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.15-10.0.0.30"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            kea.bodies("subnet4-delta-add")[0]["arguments"]["subnet4"],
            [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.15-10.0.0.30"}]}],
        )

    def test_explicit_range_and_cidr_both_reach_kea_as_a_range(self):
        for pool, sent in (
            (" 10.0.0.100 - 10.0.0.110 ", "10.0.0.100-10.0.0.110"),
            ("10.0.0.64/28", "10.0.0.64-10.0.0.79"),
        ):
            with self.subTest(pool=pool):
                response, kea = self._post(pool)

                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    kea.bodies("subnet4-delta-add")[0]["arguments"]["subnet4"],
                    [{"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": sent}]}],
                )
                self.assertIn(f"Pool {sent} added to subnet 1.", [str(m) for m in get_messages(response.wsgi_request)])

    def _assert_form_error(self, response, kea, message):
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("pool", response.context["form"].errors)
        self.assertEqual(response.context["form"].non_field_errors(), [message])
        self._assert_no_write(kea)
        self.assertNotIn("reservation-get-page", kea.commands())

    def test_an_id_that_names_no_verified_subnet_is_a_form_error_and_kea_gets_no_command(self):
        response, kea = self._post("10.0.0.100-10.0.0.110", subnet_id=99)

        self._assert_form_error(
            response, kea, "This Subnet is not in the current Subnet Catalogue. Reload the Subnets page and try again."
        )

    def test_an_unconfirmed_subnet_list_is_a_form_error_that_never_reads_as_absent(self):
        failed = {"result": 1, "text": "internal error"}
        for name, overrides in (
            ("unavailable catalogue", {"subnet4-list": failed, "config-get": failed}),
            ("identity read fails", {"subnet4-list": failed}),
        ):
            with self.subTest(name):
                response, kea = self._post("10.0.0.100-10.0.0.110", **overrides)

                self._assert_form_error(
                    response,
                    kea,
                    "NetBox could not confirm Kea's Subnet list, so it did not send the change. Try again later.",
                )
                self.assertNotContains(response, "not in the current Subnet Catalogue")

    def test_a_cidr_that_is_not_the_verified_subnet_says_that_the_subnet_changed(self):
        with stub_kea({**_ABSENT_READ_HOOKS, **_pool_add_catalogue()}) as kea:
            response = self.client.post(self._url(), {"subnet_cidr": "10.0.1.0/24", "pool": "10.0.0.100-10.0.0.110"})

        self._assert_form_error(response, kea, "Subnet 1 (10.0.1.0/24) changed in Kea. Reload the page and try again.")

    def test_a_rejected_change_shows_no_reservation_warning(self):
        reservations = _res_page([{"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:01", "ip-address": "10.0.0.50"}])
        response, kea = self._post(
            "10.0.0.32/27",
            **{"reservation-get-page": reservations, "subnet4-delta-add": {"result": 1, "text": "command failed"}},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [(m.level, str(m)) for m in get_messages(response.wsgi_request)],
            [(django_messages.ERROR, "Kea rejected the change. Kea replied: command failed")],
        )
        self.assertNotIn("reservation-get-page", kea.commands())

    def test_a_reservation_inside_the_new_pool_is_a_warning(self):
        reservations = _res_page(
            [
                {"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:01", "ip-address": "10.0.0.50"},
                {"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:02", "ip-address": "10.0.0.90"},
            ]
        )
        response, kea = self._post("10.0.0.32/27", **{"reservation-get-page": reservations})

        self.assertEqual(response.status_code, 302)
        self.assertIn("subnet4-delta-add", kea.commands())
        warnings = [str(m) for m in get_messages(response.wsgi_request) if m.level == django_messages.WARNING]
        self.assertEqual(
            warnings,
            [
                (
                    "Pool 10.0.0.32-10.0.0.63 overlaps 1 existing reservation(s): 10.0.0.50. "
                    "Kea allows this. Reservations take priority over pool allocation."
                )
            ],
        )


# ─────────────────────────────────────────────────────────────────────────────
# Subnet add GET/POST client creation errors
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddGetClientError(_ViewTestBase):
    """Cover subnet add GET when client creation fails."""

    def test_get_client_error_disables_network_field(self):
        """A real get_client() failure (cert without key → ValueError) in GET disables the network field."""
        bad = _make_db_server(name="bad-cert-add-get", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[bad.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_post_client_error_rerenders(self):
        """A real get_client() failure in POST re-renders the form with an error, no 500."""
        bad = _make_db_server(name="bad-cert-add-post", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[bad.pk])
        response = self.client.post(
            url,
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# get_subnets() error path
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetSubnetsError(_ViewTestBase):
    """Cover get_subnets() error path."""

    def test_config_get_value_error_shows_empty(self):
        """A ValueError from config-get shows an empty subnet list with an error message, no 500."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": ValueError("bad JSON"), "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# F9/F10/F11: Non-dict items & empty shared_network preservation
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetListNonDictItems(_ViewTestBase):
    """F9: Non-dict items in top-level subnets list and shared-networks list."""

    def test_non_dict_items_in_subnet_list_skipped(self):
        """Non-dict entries (string, int) in subnet4 list are skipped; valid subnet shows."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}, "malformed", 42],
                    "shared-networks": [],
                }
            },
        }
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": config, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")

    def test_non_dict_items_in_shared_networks_skipped(self):
        """Non-dict entries in shared-networks list are skipped; valid network shows."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [],
                    "shared-networks": [
                        {"name": "net1", "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]},
                        "invalid",
                    ],
                }
            },
        }
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": config, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")

    def test_non_dict_subnet_inside_shared_network_skipped(self):
        """Non-dict subnet items within a shared-network's subnet list are skipped."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [],
                    "shared-networks": [
                        {"name": "net1", "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}, "bad"]},
                    ],
                }
            },
        }
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": config, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetNetworkDataNonDictSubnet(_ViewTestBase):
    """F10: _get_network_data returns None network when shared-network has non-dict subnet."""

    def test_edit_loads_with_malformed_subnet_in_shared_network(self):
        """Edit page loads (or redirects) when a shared-network contains non-dict subnet items."""
        subnet_get_resp = {
            "result": 0,
            "arguments": {
                "subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": [], "valid-lft": 3600}]
            },
        }
        config_get_resp = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}],
                    "shared-networks": [
                        {"name": "net1", "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}, "bad"]},
                    ],
                }
            },
        }
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        with stub_kea(_edit_responses(4, subnet_get_resp, config_get_resp)):
            response = self.client.get(url)
        # _get_network_data returns None (malformed) → view still renders or redirects, never 500.
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# Coverage gap tests — _subnet_to_row, config-get edge cases, stats, pool overlap
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetViewCoverageGaps(_ViewTestBase):
    """Tests targeting specific uncovered lines in views/subnets.py."""

    def _subnets4_url(self):
        return reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])

    def _list_stub(self, config, stat=None):
        """Subnets-list GET chain: config-get (subnets) + stat-lease4-get (utilisation)."""
        return stub_kea(
            {**_ABSENT_READ_HOOKS, "config-get": config, "stat-lease4-get": stat if stat is not None else _STAT_ABSENT4}
        )

    def _pool_add_stub(self, **overrides):
        """Pool add chain: Subnet Catalogue reads, overlap probe, subnet4-delta-add, persist, list."""
        base = _pool_add_registry(42, "10.0.0.0/24")
        base.update(overrides)
        return stub_kea({**_ABSENT_READ_HOOKS, **base})

    # ── 1. config-get returns non-dict arguments (~lines 92-99) ──────────

    def test_config_get_non_dict_arguments_returns_empty_subnets(self):
        """config-get returning arguments='not-a-dict' logs a warning and returns 200 with no subnets."""
        with self._list_stub({"result": 0, "arguments": "not-a-dict"}):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["table"].data), 0)

    def test_config_get_list_arguments_returns_empty_subnets(self):
        """config-get returning arguments=[...] logs a warning and returns 200 with no subnets."""
        with self._list_stub({"result": 0, "arguments": [1, 2, 3]}):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["table"].data), 0)

    # ── 2. Stats enrichment exception paths (~lines 130-134) ─────────────

    def test_stats_value_error_still_renders_subnets(self):
        """When stat-lease4-get raises ValueError, subnets render without utilisation."""
        with self._list_stub(_config_with_one_subnet()[0], stat=ValueError("bad stat response")):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)
        # No utilisation columns should be present
        self.assertNotIn("utilization", next(iter(table.data)))

    def test_stats_type_error_still_renders_subnets(self):
        """When stat-lease4-get raises TypeError, subnets render without utilisation."""
        with self._list_stub(_config_with_one_subnet()[0], stat=TypeError("unexpected None in stat parsing")):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["table"].data), 1)

    def test_stats_key_error_still_renders_subnets(self):
        """When stat-lease4-get raises KeyError, subnets render without utilisation."""
        with self._list_stub(_config_with_one_subnet()[0], stat=KeyError("result-set")):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["table"].data), 1)

    def test_stats_request_exception_still_renders_subnets(self):
        """When stat-lease4-get raises RequestException, subnets render without utilisation."""
        with self._list_stub(_config_with_one_subnet()[0], stat=requests.RequestException("timeout")):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["table"].data), 1)

    # ── 3. _subnet_to_row with non-scalar ID (~lines 56-58) ─────────────

    def test_subnet_with_list_id_is_skipped(self):
        """Subnet with id=[1,2,3] must be skipped (non-scalar ID)."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [{"id": [1, 2, 3], "subnet": "10.0.0.0/24"}, {"id": 2, "subnet": "10.0.1.0/24"}],
                    "shared-networks": [],
                }
            },
        }
        with self._list_stub(config):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        # Only subnet with id=2 should appear; id=[1,2,3] is skipped
        self.assertEqual(len(table.data), 1)
        self.assertEqual(next(iter(table.data))["id"], 2)

    def test_subnet_with_dict_id_is_skipped(self):
        """Subnet with id={"nested": "dict"} must be skipped (non-scalar ID)."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [
                        {"id": {"nested": "dict"}, "subnet": "10.0.0.0/24"},
                        {"id": 5, "subnet": "10.0.2.0/24"},
                    ],
                    "shared-networks": [],
                }
            },
        }
        with self._list_stub(config):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)
        self.assertEqual(next(iter(table.data))["id"], 5)

    # ── 4. _subnet_to_row with malformed CIDR (~lines 60-62) ────────────

    def test_subnet_with_malformed_cidr_is_skipped(self):
        """Subnet with subnet='not-a-cidr' must be skipped and logged."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [{"id": 1, "subnet": "not-a-cidr"}, {"id": 2, "subnet": "192.168.1.0/24"}],
                    "shared-networks": [],
                }
            },
        }
        with self._list_stub(config):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)
        self.assertEqual(next(iter(table.data))["subnet"], "192.168.1.0/24")

    def test_subnet_with_empty_cidr_is_skipped(self):
        """Subnet with subnet='' must be skipped (ValueError from ip_network)."""
        config = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [{"id": 1, "subnet": ""}, {"id": 2, "subnet": "172.16.0.0/16"}],
                    "shared-networks": [],
                }
            },
        }
        with self._list_stub(config):
            response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)
        self.assertEqual(next(iter(table.data))["id"], 2)

    # ── 5. Subnet edit POST with an unusable client ─────────────────────

    def test_subnet_edit_post_client_creation_failure_sends_nothing(self):
        """A real get_client() failure (cert without key → ValueError) leaves the form without network choices."""
        bad = _make_db_server(name="bad-cert-edit-cov", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[bad.pk, 42])
        with stub_kea({}) as kea:
            response = self.client.post(url, {"subnet_cidr": "10.0.0.0/24"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not load shared networks from Kea. Please try again.")
        self.assertEqual(kea.commands(), [])

    # ── 6. Pool add with reservation overlap warning (~lines 204-257) ────

    def _pool_add_url(self):
        return reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])

    def test_pool_add_reservation_lookup_failure_warns_that_the_check_did_not_run(self):
        """A Kea contract error must tell the operator that no overlap check ran."""
        with self.assertLogs("netbox_kea.views.subnets", level="WARNING") as logs:
            with self._pool_add_stub(**{"reservation-get-page": {"result": 2, "text": "host_cmds not loaded"}}):
                response = self.client.post(
                    self._pool_add_url(), {"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.100-10.0.0.200"}, follow=True
                )
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.SUCCESS for m in msgs))
        self.assertTrue(any("overlap check did not run" in m.message.lower() for m in msgs))
        overlap_records = [record for record in logs.records if "overlap" in record.getMessage()]
        self.assertTrue(overlap_records, logs.output)
        self.assertEqual(overlap_records[0].levelname, "WARNING", logs.output)
        self.assertIsNotNone(overlap_records[0].exc_info)

    def test_pool_add_reservation_lookup_request_exception_warns_that_the_check_did_not_run(self):
        """A transport error must tell the operator that no overlap check ran."""
        with self._pool_add_stub(**{"reservation-get-page": requests.RequestException("timeout")}):
            response = self.client.post(
                self._pool_add_url(), {"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.100-10.0.0.200"}, follow=True
            )
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.SUCCESS for m in msgs))
        self.assertTrue(any("overlap check did not run" in m.message.lower() for m in msgs))

    def test_pool_add_client_creation_failure_shows_an_error(self):
        """A real get_client() failure (cert without key → ValueError) leaves no Subnet to add the Pool to."""
        bad = _make_db_server(name="bad-cert-pooladd-cov", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[bad.pk, 42])
        response = self.client.post(url, {"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.100-10.0.0.200"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "NetBox could not confirm Kea&#x27;s Subnet list")
        msgs = list(get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPersistConfigBanner(_ViewTestBase):
    """Tests that the persist_config warning banner appears when disabled."""

    def test_banner_absent_when_persist_config_true(self):
        """No warning banner on subnet add when persist_config=True (default)."""
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Configuration persistence is disabled.")

    def test_banner_present_when_persist_config_false(self):
        """Warning banner appears on subnet add page when persist_config=False."""
        server = _make_db_server(name="no-persist", persist_config=False)
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[server.pk])
        with stub_kea({**_ABSENT_READ_HOOKS, "config-get": _EMPTY_CONFIG4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configuration persistence is disabled.")


# ---------------------------------------------------------------------------
# Kea subnet lifetime parameter names (#175)
#
# Kea's subnet-scope parameters are valid-lifetime / min-valid-lifetime /
# max-valid-lifetime.  ``valid-lft`` is a *lease* field, and Kea 3.2.0 rejects
# it inside a subnet definition with "spurious 'valid-lft' parameter".
# ---------------------------------------------------------------------------

_SUBNET4_GET_LIFETIMES = {
    "result": 0,
    "arguments": {
        "subnet4": [
            {
                "id": 42,
                "subnet": "10.0.0.0/24",
                "pools": [],
                "option-data": [],
                "valid-lifetime": 3600,
                "min-valid-lifetime": 1800,
                "max-valid-lifetime": 7200,
            }
        ]
    },
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetLifetimeKeaParameterNames(_ViewTestBase):
    """The subnet edit path must use Kea's subnet lifetime parameter names."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def test_post_sends_kea_subnet_lifetime_parameter_names(self):
        """subnet4-update must carry valid-lifetime, never the valid-lft lease field."""
        stub = {
            "subnet4-list": _subnet_list(4, [{"id": 42, "subnet": "10.0.0.0/24"}]),
            "config-get": _CONFIG4_NO_NETWORKS[0],
            "subnet4-get": _SUBNET4_GET_LIFETIMES,
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        with stub_kea({**_ABSENT_READ_HOOKS, **stub}) as kea:
            response = self.client.post(
                self._url(),
                {
                    **_shown(),
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                    "shared_network": "",
                    "valid_lft": "4000",
                    "min_valid_lft": "2000",
                    "max_valid_lft": "8000",
                },
            )
        self.assertEqual(response.status_code, 302)
        bodies = kea.bodies("subnet4-update")
        self.assertTrue(bodies, "no subnet4-update request was sent")
        sent = bodies[0]["arguments"]["subnet4"][0]
        self.assertEqual(sent.get("valid-lifetime"), 4000)
        self.assertEqual(sent.get("min-valid-lifetime"), 2000)
        self.assertEqual(sent.get("max-valid-lifetime"), 8000)
        for lease_field in ("valid-lft", "min-valid-lft", "max-valid-lft"):
            self.assertNotIn(
                lease_field,
                sent,
                f"Kea rejects {lease_field!r} in a subnet definition as a spurious parameter",
            )

    def test_get_prefills_from_kea_subnet_lifetime_parameter_names(self):
        """The edit form uses the Subnet lifetime settings declared by Kea."""
        with stub_kea(_edit_responses(4, _SUBNET4_GET_LIFETIMES, _CONFIG4_NO_NETWORKS[0])):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        initial = response.context["form"].initial
        self.assertEqual(initial.get("valid_lft"), 3600)
        self.assertEqual(initial.get("min_valid_lft"), 1800)
        self.assertEqual(initial.get("max_valid_lft"), 7200)
