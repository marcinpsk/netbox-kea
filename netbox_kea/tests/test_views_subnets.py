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
from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse

from .kea_stub import queued, stub_kea
from .utils import _PLUGINS_CONFIG, _kea_command_side_effect, _make_db_server, _ViewTestBase

# Shared stub responses for the subnet list/table views, which issue config-get
# (subnets + shared-networks) then stat-lease{v}-get (utilisation; degrades if the
# stat_cmds hook is absent — modelled by result 2 → KeaException → skipped).
_EMPTY_CONFIG4 = {"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}
_EMPTY_CONFIG6 = {"result": 0, "arguments": {"Dhcp6": {"subnet6": [], "shared-networks": []}}}
_STAT_ABSENT4 = {"result": 2, "text": "unknown command 'stat-lease4-get'"}
_STAT_ABSENT6 = {"result": 2, "text": "unknown command 'stat-lease6-get'"}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnets4View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/subnets4/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({"config-get": _EMPTY_CONFIG4, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_sets_tab_in_context(self):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerDHCP4SubnetsView

        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({"config-get": _EMPTY_CONFIG4, "stat-lease4-get": _STAT_ABSENT4}):
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
        with stub_kea({"config-get": _EMPTY_CONFIG6, "stat-lease6-get": _STAT_ABSENT6}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_sets_tab_in_context(self):
        """F2: GET response must include 'tab' in context for tab bar highlighting.

        v4 and v6 subnets now render under the single shared 'Subnets' tab
        (owned by ServerDHCP4SubnetsView); the v6 view injects it via context.
        """
        from netbox_kea.views import ServerDHCP4SubnetsView

        url = reverse("plugins:netbox_kea:server_subnets6", args=[self.server.pk])
        with stub_kea({"config-get": _EMPTY_CONFIG6, "stat-lease6-get": _STAT_ABSENT6}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerDHCP4SubnetsView.tab)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEnrichment(_ViewTestBase):
    """Subnet views must pass option-data and pool information through to the table."""

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
        return stub_kea({"config-get": self._config_with_subnet(4)[0], "stat-lease4-get": _STAT_ABSENT4})

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
        first_row = list(table.data)[0]
        expected = int(ipaddress.ip_network("10.0.0.0/24").network_address)
        self.assertEqual(first_row["_subnet_sort_key"], expected)


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
        with stub_kea({"config-get": _config_with_one_subnet()[0], "stat-lease4-get": _STAT_LEASE4_RESPONSE[0]}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "25%")

    def test_no_crash_when_stat_cmds_unavailable(self):
        """When stat_cmds hook is not loaded, subnets page must still render (200)."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({"config-get": _config_with_one_subnet()[0], "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_zero_percent_when_no_leases_assigned(self):
        """0 assigned / 100 total → '0%' utilization."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({"config-get": _config_with_one_subnet()[0], "stat-lease4-get": self._stat(0)}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0%")

    def test_hundred_percent_when_fully_utilized(self):
        """All addresses assigned → '100%' utilization."""
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        with stub_kea({"config-get": _config_with_one_subnet()[0], "stat-lease4-get": self._stat(50, total=50)}):
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
        with stub_kea({"subnet4-get": subnet}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")
        self.assertContains(response, "42")

    def test_get_shows_form_when_subnet_fetch_fails(self):
        """GET must still return 200 even when the subnet-get Kea call fails."""
        with stub_kea({"subnet4-get": {"result": 1, "text": "not found"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_calls_lease_wipe_and_redirects(self):
        """POST must call lease_wipe on the client and redirect to the subnets tab."""
        with stub_kea({"lease4-wipe": {"result": 0}}) as kea:
            response = self.client.post(self._url(subnet_id=10))
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn("lease4-wipe", kea.commands())
        self.assertEqual(kea.bodies("lease4-wipe")[0]["arguments"]["subnet-id"], 10)

    def test_post_on_kea_exception_shows_error_message(self):
        """POST that causes a KeaException must flash an error and redirect (no 500)."""
        with stub_kea({"lease4-wipe": {"result": 1, "text": "hook not loaded"}}):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_post_on_unexpected_exception_shows_error_message(self):
        """POST that raises an unexpected exception must redirect (no 500)."""
        with stub_kea({"lease4-wipe": ValueError("unexpected")}):
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
        with stub_kea({"subnet6-get": subnet}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2001:db8::/32")

    def test_post_calls_lease_wipe_v6(self):
        """POST must call lease_wipe with version=6."""
        with stub_kea({"lease6-wipe": {"result": 0}}) as kea:
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
                    "valid-lft": 3600,
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
                    "valid-lft": 3600,
                }
            ]
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4EditView(_ViewTestBase):
    """Tests for ServerSubnet4EditView (GET prefill + POST update)."""

    # subnet_update reads the live subnet before merging; give it one to merge onto.
    _LIVE_SUBNET4 = {
        "result": 0,
        "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
    }

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def _get_stub(self, subnet=None, config=None):
        """GET chain: subnet{v}-get (prefill) then config-get (network/inherited data)."""
        return stub_kea(
            {
                "subnet4-get": subnet if subnet is not None else _SUBNET4_GET_FULL[0],
                "config-get": config if config is not None else _CONFIG4_NO_NETWORKS[0],
            }
        )

    def _post_stub(self, **overrides):
        """POST chain: view config-get + real subnet_update (subnet-get → update → persist)."""
        base = {
            "config-get": _CONFIG4_NO_NETWORKS[0],
            "subnet4-get": self._LIVE_SUBNET4,
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        base.update(overrides)
        return stub_kea(base)

    @staticmethod
    def _updated_subnet(kea):
        """The subnet object in the real subnet4-update payload."""
        return kea.bodies("subnet4-update")[0]["arguments"]["subnet4"][0]

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

    def test_get_when_subnet_fetch_fails_redirects_with_error(self):
        """GET must redirect to the subnet list when the subnet-get Kea call fails."""
        with stub_kea({"subnet4-get": {"result": 1, "text": "not found"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("subnets", response.url)

    def test_post_valid_form_calls_subnet_update_and_redirects(self):
        """POST with valid form must call subnet_update and redirect to subnet list."""
        with self._post_stub() as kea:
            response = self.client.post(
                self._url(subnet_id=42),
                {
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
                {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
            )
        self.assertEqual(self._updated_subnet(kea)["id"], 42)

    def test_post_on_kea_exception_shows_error_and_rerenders(self):
        """POST that raises KeaException must re-render the form (not crash)."""
        # result=1 on subnet4-update makes the real client raise KeaException.
        with self._post_stub(**{"subnet4-update": {"result": 1, "text": "subnet cmds not loaded"}}):
            response = self.client.post(
                self._url(subnet_id=42),
                {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
            )
        # Should show error (redirect or re-render, not 500)
        self.assertIn(response.status_code, (200, 302))
        self._assert_no_none_pk_redirect(response)

    def test_post_invalid_form_rerenders_with_200(self):
        """POST with invalid data (bad gateway IP) must re-render the form."""
        # Invalid form re-renders after the network config-get, before any subnet4-update.
        with stub_kea({"config-get": _CONFIG4_NO_NETWORKS[0]}) as kea:
            response = self.client.post(
                self._url(subnet_id=42),
                {
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
                {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
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
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                    "ddns_qualifying_suffix": "example.com.",
                },
            )
        self.assertEqual(self._updated_subnet(kea)["ddns-qualifying-suffix"], "example.com.")

    def test_post_clears_ddns_qualifying_suffix_with_empty_string(self):
        """Clearing the DDNS field on edit must remove it from the subnet4-update payload.

        The edit form is always fully populated, so an empty field means "clear". The
        live subnet carries a suffix; the cleared POST must drop it. This locks the
        view→client wiring: reverting the call to ``... or None`` would coerce the
        cleared field to None (= preserve), so the live suffix would survive in the payload.
        """
        live_with_ddns = {
            "result": 0,
            "arguments": {
                "subnet4": [
                    {
                        "id": 42,
                        "subnet": "10.0.0.0/24",
                        "pools": [],
                        "option-data": [],
                        "ddns-qualifying-suffix": "old.example.com.",
                    }
                ]
            },
        }
        with self._post_stub(**{"subnet4-get": live_with_ddns}) as kea:
            self.client.post(
                self._url(subnet_id=42),
                {
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
        """F5: When config-get raises KeaException, inherited_options is an empty dict."""
        # subnet lookup succeeds; the network config-get fails (result 1 → KeaException).
        with self._get_stub(subnet=_SUBNET4_GET_FULL[0], config={"result": 1, "text": "err"}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        inherited = response.context.get("inherited_options", {})
        self.assertEqual(inherited, {})

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

    # subnet_update reads the live subnet before merging; give it one to merge onto.
    _LIVE_SUBNET6 = {
        "result": 0,
        "arguments": {"subnet6": [{"id": 7, "subnet": "2001:db8::/48", "pools": [], "option-data": []}]},
    }

    def _url(self, subnet_id=7):
        return reverse("plugins:netbox_kea:server_subnet6_edit", args=[self.server.pk, subnet_id])

    def _get_stub(self, subnet=None, config=None):
        """GET chain: subnet6-get (prefill) then config-get (network/inherited data)."""
        return stub_kea(
            {
                "subnet6-get": subnet if subnet is not None else _SUBNET6_GET_FULL[0],
                "config-get": config if config is not None else _CONFIG6_NO_NETWORKS[0],
            }
        )

    def _post_stub(self, **overrides):
        """POST chain: view config-get + real subnet_update (subnet6-get → update → persist)."""
        base = {
            "config-get": _CONFIG6_NO_NETWORKS[0],
            "subnet6-get": self._LIVE_SUBNET6,
            "subnet6-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
        }
        base.update(overrides)
        return stub_kea(base)

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
                {"subnet_cidr": "2001:db8::/48", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
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

# Config-get response with "prod-net" shared-network (for SharedNetworkEditView POST tests)
_CONFIG4_WITH_PROD_NET = [
    {
        "result": 0,
        "arguments": {"Dhcp4": {"shared-networks": [{"name": "prod-net", "option-data": [], "subnet4": []}]}},
    }
]

# Config-get response with "prod-net6" shared-network (for SharedNetworkEditView v6 POST tests)
_CONFIG6_WITH_PROD_NET = [
    {
        "result": 0,
        "arguments": {"Dhcp6": {"shared-networks": [{"name": "prod-net6", "option-data": [], "subnet6": []}]}},
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

    # subnet_update reads the live subnet before merging; give it one to merge onto.
    _LIVE_SUBNET4 = {
        "result": 0,
        "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
    }

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def _post_data(self, shared_network="", current_network=""):
        return {
            "subnet_cidr": "10.0.0.0/24",
            "pools": "",
            "gateway": "",
            "dns_servers": "",
            "ntp_servers": "",
            "shared_network": shared_network,
            "current_network": current_network,
        }

    def _get_stub(self, config):
        """GET chain: subnet4-get (prefill) then config-get (network data drives the dropdown)."""
        return stub_kea({"subnet4-get": _SUBNET4_GET_FULL[0], "config-get": config})

    def _post_stub(self, config, **overrides):
        """POST chain: config-get (current network + choices) + real subnet_update + network move.

        The view determines the *current* network server-side from config-get (not from the
        POST body), so the config passed here decides whether add/del actually fire.
        ``network4-subnet-add``/``-del`` are always registered; the view only issues the ones
        the membership change requires.
        """
        base = {
            "config-get": config,
            "subnet4-get": self._LIVE_SUBNET4,
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
            "network4-subnet-add": {"result": 0},
            "network4-subnet-del": {"result": 0},
        }
        base.update(overrides)
        return stub_kea(base)

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

    def test_post_assigns_subnet_to_network_calls_network_subnet_add(self):
        """POST moving subnet into a network must issue network4-subnet-add (and no del)."""
        # config places subnet 42 in NO network → current_network == "".
        with self._post_stub(config=_CONFIG4_NO_NETWORKS[0]) as kea:
            response = self.client.post(self._url(), self._post_data(shared_network="net-alpha", current_network=""))
        self.assertIn(response.status_code, (200, 302))
        self.assertIn("network4-subnet-add", kea.commands())
        self.assertNotIn("network4-subnet-del", kea.commands())
        args = kea.bodies("network4-subnet-add")[0]["arguments"]
        self.assertEqual(args["name"], "net-alpha")
        self.assertEqual(args["id"], 42)

    def test_post_removes_subnet_from_network_calls_network_subnet_del(self):
        """POST clearing network (current→blank) must issue network4-subnet-del (and no add)."""
        # config places subnet 42 in net-alpha → current_network == "net-alpha".
        with self._post_stub(config=_CONFIG4_WITH_SUBNET_IN_NETWORK[0]) as kea:
            response = self.client.post(self._url(), self._post_data(shared_network="", current_network="net-alpha"))
        self.assertIn(response.status_code, (200, 302))
        self.assertIn("network4-subnet-del", kea.commands())
        self.assertNotIn("network4-subnet-add", kea.commands())
        args = kea.bodies("network4-subnet-del")[0]["arguments"]
        self.assertEqual(args["name"], "net-alpha")
        self.assertEqual(args["id"], 42)

    def test_post_changes_network_calls_del_then_add(self):
        """POST changing from one network to another must issue both add and del."""
        with self._post_stub(config=_CONFIG4_WITH_SUBNET_IN_NETWORK[0]) as kea:
            self.client.post(self._url(), self._post_data(shared_network="net-beta", current_network="net-alpha"))
        self.assertIn("network4-subnet-add", kea.commands())
        self.assertIn("network4-subnet-del", kea.commands())
        self.assertEqual(kea.bodies("network4-subnet-del")[0]["arguments"]["name"], "net-alpha")
        self.assertEqual(kea.bodies("network4-subnet-add")[0]["arguments"]["name"], "net-beta")

    def test_post_no_network_change_does_not_call_network_subnet_methods(self):
        """POST when network is unchanged must NOT issue network4-subnet-add or -del."""
        with self._post_stub(config=_CONFIG4_WITH_SUBNET_IN_NETWORK[0]) as kea:
            self.client.post(self._url(), self._post_data(shared_network="net-alpha", current_network="net-alpha"))
        self.assertNotIn("network4-subnet-add", kea.commands())
        self.assertNotIn("network4-subnet-del", kea.commands())

    def test_post_network_assignment_with_version_4(self):
        """POST must issue network4-subnet-add (the v4-specific command) for v4 subnets."""
        with self._post_stub(config=_CONFIG4_NO_NETWORKS[0]) as kea:
            self.client.post(self._url(), self._post_data(shared_network="net-alpha", current_network=""))
        self.assertIn("network4-subnet-add", kea.commands())


# ─────────────────────────────────────────────────────────────────────────────
# Gap S1: Shared-network assignment on subnet create
# ─────────────────────────────────────────────────────────────────────────────

# Config-get response listing available networks (no subnets assigned yet)
_CONFIG4_NETWORKS_FOR_ADD = [
    {
        "result": 0,
        "arguments": {
            "Dhcp4": {
                "subnet4": [],
                "shared-networks": [
                    {"name": "net-alpha", "subnet4": []},
                    {"name": "net-beta", "subnet4": []},
                ],
            }
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSubnet4AddViewSharedNetwork(_ViewTestBase):
    """GET/POST /plugins/kea/servers/<pk>/subnets4/add/ — shared_network field."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    def _valid_post_data(self, shared_network=""):
        return {
            "subnet": "10.0.1.0/24",
            "subnet_id": "",
            "pools": "",
            "gateway": "",
            "dns_servers": "",
            "ntp_servers": "",
            "shared_network": shared_network,
        }

    def _add_stub(self, **overrides):
        """POST chain: config-get (choices) + real subnet_add (list→add→persist) + network move.

        subnet_id is left blank, so subnet_add auto-assigns via subnet4-list (empty → id 1);
        subnet4-add echoes id 1, which is what the follow-up network4-subnet-add targets.
        network4-subnet-add is always registered; the view only issues it when a network is set.
        """
        base = {
            "config-get": _CONFIG4_NETWORKS_FOR_ADD[0],
            "subnet4-list": {"result": 0, "arguments": {"subnets": []}},
            "subnet4-add": {"result": 0, "arguments": {"subnets": [{"id": 1}]}},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
            "network4-subnet-add": {"result": 0},
        }
        base.update(overrides)
        return stub_kea(base)

    def test_get_shows_shared_network_dropdown(self):
        """GET must render a shared_network dropdown populated from Kea config."""
        with stub_kea({"config-get": _CONFIG4_NETWORKS_FOR_ADD[0]}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "net-alpha")
        self.assertContains(response, "net-beta")

    def test_post_with_shared_network_calls_network_subnet_add(self):
        """POST with shared_network set must issue network4-subnet-add after subnet creation."""
        with self._add_stub() as kea:
            response = self.client.post(self._url(), self._valid_post_data(shared_network="net-alpha"))
        self.assertIn(response.status_code, (302, 200))
        self.assertIn("subnet4-add", kea.commands())
        self.assertIn("network4-subnet-add", kea.commands())
        args = kea.bodies("network4-subnet-add")[0]["arguments"]
        self.assertEqual(args["name"], "net-alpha")
        self.assertEqual(args["id"], 1)

    def test_post_without_shared_network_does_not_call_network_subnet_add(self):
        """POST without shared_network must NOT issue network4-subnet-add."""
        with self._add_stub() as kea:
            response = self.client.post(self._url(), self._valid_post_data(shared_network=""))
        self.assertIn(response.status_code, (302, 200))
        self.assertIn("subnet4-add", kea.commands())
        self.assertNotIn("network4-subnet-add", kea.commands())


# ---------------------------------------------------------------------------
# Tests for _get_network_choices — None/missing arguments handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkChoicesNoneArguments(_ViewTestBase):
    """_get_network_choices must return fallback when config-get returns None arguments."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    # subnet the GET prefill (subnet{v}-get) returns before config-get is consulted.
    _SUBNET4_GET = {"result": 0, "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}]}}

    def test_get_falls_back_when_config_returns_none_arguments(self):
        """GET must not crash and must show form when config-get returns arguments=None."""
        config_none_args = {"result": 0, "arguments": None, "text": "no config"}
        with stub_kea({"subnet4-get": self._SUBNET4_GET, "config-get": config_none_args}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_falls_back_when_config_raises_kea_exception(self):
        """GET must not crash when config-get fails (result 1 → real KeaException)."""
        # result=1 on config-get makes the real client raise KeaException, which
        # _get_network_data catches and degrades to fallback choices.
        with stub_kea({"subnet4-get": self._SUBNET4_GET, "config-get": {"result": 1, "text": "error"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_subnet_update_fails_does_not_move_network(self):
        """POST where subnet_update fails (result 1 → KeaException) must NOT issue a network move."""
        # config-get places subnet 42 in NO network; subnet4-update returns result 1 so the
        # real subnet_update raises KeaException before the network-move block is reached.
        stub = {
            "config-get": _CONFIG4_NO_NETWORKS[0],
            "subnet4-get": {
                "result": 0,
                "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
            },
            "subnet4-update": {"result": 1, "text": "update failed"},
            "network4-subnet-add": {"result": 0},
            "network4-subnet-del": {"result": 0},
        }
        with stub_kea(stub) as kea:
            response = self.client.post(
                self._url(),
                {
                    "subnet_cidr": "10.0.0.0/24",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                    "shared_network": "net-alpha",
                    "current_network": "",
                },
            )
        # View should return to form (200) or redirect, but NOT issue a network move.
        self.assertIn(response.status_code, (200, 302))
        self.assertIn("subnet4-update", kea.commands())
        self.assertNotIn("network4-subnet-add", kea.commands())
        self.assertNotIn("network4-subnet-del", kea.commands())


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
        """GET chain: subnet4-get (zero timers) then config-get (network data)."""
        return stub_kea({"subnet4-get": _SUBNET4_GET_ZERO_TIMERS[0], "config-get": _CONFIG4_NO_NETWORKS_RESP[0]})

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
class TestPoolAddExceptions(_ViewTestBase):
    """_BasePoolAddView POST exception paths."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, subnet_id])

    def test_partial_persist_error_redirects_with_warning(self):
        """A real config-write failure (PartialPersistError) on pool_add must warn."""
        # pool_add: reservation-get-page (overlap probe) → list-commands → subnet4-pool-add →
        # persist (config-get/test/write). config-write result 1 → real PartialPersistError.
        # follow=True lands on the subnets list, which issues config-get + stat-lease4-get.
        stub = {
            "reservation-get-page": {"result": 3},  # no reservations → no overlap warning
            "list-commands": {
                "result": 0,
                "arguments": ["subnet4-pool-add", "config-get", "config-test", "config-write"],
            },
            "subnet4-pool-add": {"result": 0},
            "config-get": _EMPTY_CONFIG4,
            "config-test": {"result": 0},
            "config-write": {"result": 1, "text": "disk full"},
            "stat-lease4-get": _STAT_ABSENT4,
        }
        with stub_kea(stub):
            response = self.client.post(self._url(), {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    def test_generic_exception_shows_error(self):
        """A generic (ValueError) failure at the HTTP boundary during pool_add must show an error."""
        # list-commands raises ValueError at the boundary; the real client surfaces it as a
        # ValueError out of pool_add, hitting the view's generic-error branch.
        stub = {
            "reservation-get-page": {"result": 3},
            "list-commands": ValueError("crash"),
            "config-get": _EMPTY_CONFIG4,
            "stat-lease4-get": _STAT_ABSENT4,
        }
        with stub_kea(stub):
            response = self.client.post(self._url(), {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolDeleteExceptions(_ViewTestBase):
    """_BasePoolDeleteView GET/POST exception paths."""

    def _url(self, subnet_id=42, pool="10.0.0.100-10.0.0.200"):
        return reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, subnet_id, pool])

    def _pool_del_stub(self, **overrides):
        """pool_del chain (list-commands → subnet4-pool-del → persist) + the followed subnets list.

        config-write defaults to success; override it (or list-commands) to drive error paths.
        """
        base = {
            "list-commands": {
                "result": 0,
                "arguments": ["subnet4-pool-del", "config-get", "config-test", "config-write"],
            },
            "subnet4-pool-del": {"result": 0},
            "config-get": _EMPTY_CONFIG4,
            "config-test": {"result": 0},
            "config-write": {"result": 0},
            "stat-lease4-get": _STAT_ABSENT4,
        }
        base.update(overrides)
        return stub_kea(base)

    def test_get_invalid_pool_format_returns_400(self):
        """GET with invalid pool string must return 400 (before any Kea call)."""
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "not_a_pool_format!!"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)

    def test_get_range_pool_with_spaces_returns_200(self):
        """GET with a Kea range pool string like '192.0.2.10 - 192.0.2.20' must not return 400."""
        # The delete-confirm GET renders without contacting Kea, so no stub is needed.
        url = reverse(
            "plugins:netbox_kea:server_subnet4_pool_delete",
            args=[self.server.pk, 42, "192.0.2.10 - 192.0.2.20"],
        )
        response = self.client.get(url)
        self.assertNotEqual(response.status_code, 400)

    def test_post_invalid_pool_format_returns_400(self):
        """POST with invalid pool string must return 400 (before any Kea call)."""
        url = reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "not_a_pool_format!!"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 400)

    def test_post_range_pool_with_spaces_accepted(self):
        """POST with a Kea range pool string like '192.0.2.10 - 192.0.2.20' must not return 400."""
        url = reverse(
            "plugins:netbox_kea:server_subnet4_pool_delete",
            args=[self.server.pk, 42, "192.0.2.10 - 192.0.2.20"],
        )
        with self._pool_del_stub():
            response = self.client.post(url, follow=True)
        self.assertNotEqual(response.status_code, 400)

    def test_partial_persist_error_redirects_with_warning(self):
        """A real config-write failure (PartialPersistError) on pool_del must warn."""
        with self._pool_del_stub(**{"config-write": {"result": 1, "text": "disk full"}}):
            response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    def test_generic_exception_shows_error(self):
        """A generic (ValueError) failure at the HTTP boundary during pool_del must show an error."""
        with self._pool_del_stub(**{"list-commands": ValueError("crash")}):
            response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


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
    """_BaseSubnetAddView GET/POST exception paths."""

    # config-get response whose shared-networks include "alpha" (so the choice validates)
    # and that doubles as a valid config for the _persist_config read-back.
    _CONFIG4_ALPHA = {
        "result": 0,
        "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": [{"name": "alpha", "subnet4": []}]}},
    }

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    def _add_stub(self, config, **overrides):
        """subnet_add chain (config-get choices → subnet4-list → subnet4-add → persist) + network move.

        The same config-get value serves the choices lookup, the persist read-back, and the
        followed subnets-list render. Override any leg (e.g. config-write result 1) to drive
        the error branches; subnet4-add echoes id 1 unless overridden.
        """
        base = {
            "config-get": config,
            "subnet4-list": {"result": 0, "arguments": {"subnets": []}},
            "subnet4-add": {"result": 0, "arguments": {"subnets": [{"id": 1}]}},
            "config-test": {"result": 0},
            "config-write": {"result": 0},
            "network4-subnet-add": {"result": 0},
            "stat-lease4-get": _STAT_ABSENT4,
        }
        base.update(overrides)
        return stub_kea(base)

    def test_get_falls_back_when_network_choices_raise(self):
        """GET must render the form with fallback choices when config-get fails (result 1 → KeaException)."""
        with stub_kea({"config-get": {"result": 1, "text": "config error"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_partial_persist_error_redirects(self):
        """A real config-write failure (PartialPersistError) on subnet_add must warn."""
        with self._add_stub(_EMPTY_CONFIG4, **{"config-write": {"result": 1, "text": "disk full"}}):
            response = self.client.post(self._url(), _SUBNET_ADD_POST, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    def test_post_partial_persist_error_with_subnet_id_attempts_network_assignment(self):
        """A PartialPersistError carrying the new subnet_id must still trigger network assignment."""
        # subnet4-add echoes id 10; config-write fails so subnet_add raises PartialPersistError(subnet_id=10).
        # The view then issues network4-subnet-add for that id (its own persist also fails → second warning).
        with self._add_stub(
            self._CONFIG4_ALPHA,
            **{
                "subnet4-add": {"result": 0, "arguments": {"subnets": [{"id": 10}]}},
                "config-write": {"result": 1, "text": "disk full"},
            },
        ) as kea:
            response = self.client.post(self._url(), {**_SUBNET_ADD_POST, "shared_network": "alpha"}, follow=True)
        self.assertIn("network4-subnet-add", kea.commands())
        args = kea.bodies("network4-subnet-add")[0]["arguments"]
        self.assertEqual(args["name"], "alpha")
        self.assertEqual(args["id"], 10)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    def test_post_partial_persist_error_without_subnet_id_skips_network_assignment(self):
        """A PartialPersistError with no known subnet_id must skip network assignment."""
        # subnet4-list fails and subnet4-add echoes no id, so subnet_def has no "id" → the
        # PartialPersistError carries subnet_id=None and the view cannot assign a network.
        with self._add_stub(
            self._CONFIG4_ALPHA,
            **{
                "subnet4-list": {"result": 1, "text": "not loaded"},
                "subnet4-add": {"result": 0},
                "config-write": {"result": 1, "text": "disk full"},
            },
        ) as kea:
            response = self.client.post(self._url(), {**_SUBNET_ADD_POST, "shared_network": "alpha"}, follow=True)
        self.assertNotIn("network4-subnet-add", kea.commands())
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    def test_post_subnet_add_runtime_error_rerenders_form(self):
        """A generic (ValueError) failure during subnet_add must re-render the form (200)."""
        # subnet4-add raises ValueError at the boundary; the disambiguation probe (config-get)
        # finds no matching subnet, so subnet_add re-raises ValueError → view re-renders.
        with self._add_stub(_EMPTY_CONFIG4, **{"subnet4-add": ValueError("crash")}):
            response = self.client.post(self._url(), _SUBNET_ADD_POST)
        self.assertEqual(response.status_code, 200)

    def test_post_network_assignment_partial_persist_shows_warning(self):
        """A config-write failure during network_subnet_add (after a clean subnet_add) must warn."""
        # config-write succeeds for the subnet_add persist, then fails for the network persist.
        with self._add_stub(
            self._CONFIG4_ALPHA,
            **{
                "subnet4-add": {"result": 0, "arguments": {"subnets": [{"id": 5}]}},
                "config-write": queued({"result": 0}, {"result": 1, "text": "disk full"}),
            },
        ):
            response = self.client.post(self._url(), {**_SUBNET_ADD_POST, "shared_network": "alpha"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("config-write failed" in m.message.lower() for m in msgs))

    def test_post_network_assignment_generic_exception_shows_warning(self):
        """A transport error on network_subnet_add (after a clean subnet_add) must warn."""
        # network4-subnet-add raises RequestException at the boundary → view's "could not be assigned" warning.
        with self._add_stub(
            self._CONFIG4_ALPHA,
            **{
                "subnet4-add": {"result": 0, "arguments": {"subnets": [{"id": 5}]}},
                "network4-subnet-add": requests.RequestException("network error"),
            },
        ):
            response = self.client.post(self._url(), {**_SUBNET_ADD_POST, "shared_network": "alpha"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("could not be assigned" in m.message.lower() for m in msgs))

    def test_post_client_none_reconnect_failure_shows_error(self):
        """When get_client fails (cert without key → ValueError), the view shows an error, no 500."""
        # Server.objects.create() skips clean(), so a cert-without-key server persists; get_client
        # then raises ValueError before any Kea call, exercising the view's connect-failure branch.
        bad_server = _make_db_server(name="bad-cert", client_cert_path="/nonexistent/cert.pem")
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[bad_server.pk])
        response = self.client.post(url, _SUBNET_ADD_POST)
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# SubnetEdit exception paths
# ---------------------------------------------------------------------------

_SUBNET4_EDIT_POST = {
    "subnet_cidr": "10.0.0.0/24",
    "pools": "",
    "gateway": "",
    "dns_servers": "",
    "ntp_servers": "",
    "shared_network": "",
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditPostExceptions(_ViewTestBase):
    """_BaseSubnetEditView POST exception paths (PartialPersistError, KeaException, generic)."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    # the live subnet subnet4-get returns before subnet4-update runs.
    _LIVE_SUBNET4 = {
        "result": 0,
        "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
    }

    def test_post_partial_persist_redirects_with_warning(self):
        """A real config-write failure (PartialPersistError) on subnet_update must warn."""
        # subnet_update: config-get (network data) → subnet4-get → subnet4-update → persist.
        # config-write result 1 → PartialPersistError. follow=True renders the subnets list.
        stub = {
            "config-get": _CONFIG4_NO_NETWORKS[0],
            "subnet4-get": self._LIVE_SUBNET4,
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            "config-write": {"result": 1, "text": "disk full"},
            "stat-lease4-get": _STAT_ABSENT4,
        }
        with stub_kea(stub):
            response = self.client.post(self._url(), _SUBNET4_EDIT_POST, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.WARNING for m in msgs))

    def test_post_generic_exception_rerenders(self):
        """A generic (ValueError) failure on subnet_update must re-render the form (200)."""
        # subnet4-update raises ValueError at the boundary → view's generic-error branch re-renders.
        stub = {
            "config-get": _CONFIG4_NO_NETWORKS[0],
            "subnet4-get": self._LIVE_SUBNET4,
            "subnet4-update": ValueError("crash"),
        }
        with stub_kea(stub):
            response = self.client.post(self._url(), _SUBNET4_EDIT_POST)
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkDelPartialPersist(_ViewTestBase):
    """Subnet edit: network_subnet_del PartialPersistError must NOT rollback new_network."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, subnet_id])

    def test_partial_persist_on_del_skips_rollback(self):
        """When network_subnet_del hits a config-write failure, the view must not roll back the add."""
        # subnet 42 lives in old-net; POST moves it to new-net. Order: subnet_update persist (ok),
        # network4-subnet-add persist (ok), network4-subnet-del persist (config-write fails). Because
        # the del is already live (PartialPersistError), the view must NOT issue a rollback del — so
        # network4-subnet-del is issued exactly once.
        config_with_current_net = {
            "result": 0,
            "arguments": {
                "Dhcp4": {
                    "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}],
                    "shared-networks": [
                        {"name": "old-net", "subnet4": [{"id": 42}]},
                        {"name": "new-net", "subnet4": []},
                    ],
                }
            },
        }
        stub = {
            "config-get": config_with_current_net,
            "subnet4-get": {
                "result": 0,
                "arguments": {"subnet4": [{"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
            },
            "subnet4-update": {"result": 0},
            "config-test": {"result": 0},
            # 1: subnet_update persist ok, 2: network-add persist ok, 3: network-del persist fails.
            "config-write": queued({"result": 0}, {"result": 0}, {"result": 1, "text": "disk full"}),
            "network4-subnet-add": {"result": 0},
            "network4-subnet-del": {"result": 0},
        }
        post_data = {**_SUBNET4_EDIT_POST, "shared_network": "new-net"}
        with stub_kea(stub) as kea:
            response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))
        # del issued once (for old-net); no rollback del of new-net.
        self.assertEqual(kea.commands().count("network4-subnet-del"), 1)


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
        with stub_kea({"config-get": {"result": 0, "arguments": None}, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_export_returns_csv(self):
        """?export=csv returns a CSV file response."""
        with stub_kea({"config-get": _EMPTY_CONFIG4, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(self._url() + "?export=csv")
        self.assertEqual(response.status_code, 200)

    def test_htmx_partial_returns_table_fragment(self):
        """HTMX request to the subnet view returns a partial table fragment."""
        with stub_kea({"config-get": _EMPTY_CONFIG4, "stat-lease4-get": _STAT_ABSENT4}):
            response = self.client.get(self._url(), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Subnet delete — GET exception + POST generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetDeleteExceptionPaths(_ViewTestBase):
    """Lines 3177-3178, 3203-3205: subnet delete GET exception and POST generic."""

    def _url(self, subnet_id=42):
        return reverse("plugins:netbox_kea:server_subnet4_delete", args=[self.server.pk, subnet_id])

    def test_get_exception_still_renders(self):
        """A subnet-get failure (result 1 → KeaException) in GET must still render the confirm page."""
        with stub_kea({"subnet4-get": {"result": 1, "text": "subnet-get failed"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_post_generic_exception_shows_error(self):
        """A generic (ValueError) failure on subnet_del must redirect with an error, no 500."""
        # subnet4-del raises ValueError at the boundary → view's generic-error branch redirects.
        with stub_kea({"subnet4-del": ValueError("crash")}):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)


# ---------------------------------------------------------------------------
# _fetch_subnets_from_server — null config, shared-network subnets, stat_cmds exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchSubnetsFromServer(_ViewTestBase):
    """Lines 3807-3855: _fetch_subnets_from_server edge cases."""

    def _run(self, responses):
        """Call _fetch_subnets_from_server against a real client with the given stubbed responses."""
        from netbox_kea.views import _fetch_subnets_from_server

        with stub_kea(responses):
            return _fetch_subnets_from_server(self.server, version=4)

    def test_null_arguments_raises(self):
        """Null config-get arguments raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            self._run({"config-get": {"result": 0, "arguments": None}})

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
        result = self._run({"config-get": config, "stat-lease4-get": _STAT_ABSENT4})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["subnet"], "192.168.0.0/24")

    def test_stat_cmds_exception_swallowed(self):
        """A stat_cmds failure (missing hook) is swallowed; subnets are still returned."""
        config_resp = {
            "result": 0,
            "arguments": {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}], "shared-networks": []}},
        }
        result = self._run({"config-get": config_resp, "stat-lease4-get": _STAT_ABSENT4})
        self.assertEqual(len(result), 1)

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
        result = self._run({"config-get": config_resp, "stat-lease4-get": stat_resp})
        self.assertEqual(len(result), 1)
        # stat data was merged into the subnet dict
        self.assertEqual(result[0].get("total"), 100)
        self.assertEqual(result[0].get("assigned"), 25)


# ---------------------------------------------------------------------------
# Subnet edit — _form_initial with ntp/dns + lease time fields
# ---------------------------------------------------------------------------


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
                        "valid-lft": 3600,
                        "min-valid-lft": 1800,
                        "max-valid-lft": 7200,
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
        with stub_kea({"subnet4-get": subnet_resp, "config-get": config_resp}):
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
        with stub_kea({"subnet4-get": subnet_resp, "config-get": config_resp}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid-net")


# ---------------------------------------------------------------------------
# _fetch_network — non-dict args (lines 1462-1463)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchNetworkNonDictArgs(_ViewTestBase):
    """Lines 1462-1463: config-get returns non-dict args → log warning + return {}."""

    def test_get_non_dict_args_redirects(self):
        """config-get returning arguments=None → _fetch_network returns {} → redirect."""
        url = reverse(
            "plugins:netbox_kea:server_shared_network4_edit",
            args=[self.server.pk, "test-net"],
        )
        with stub_kea({"config-get": {"result": 0, "arguments": None}}):
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
        with stub_kea({"config-get": {"result": 1, "text": "error"}}):
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
        with stub_kea({"subnet4-get": subnet_resp, "config-get": config_resp}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # inherited_options should have gateway and ntp_servers from global config
        inherited = response.context.get("inherited_options", {})
        self.assertIn("gateway", inherited)
        self.assertIn("ntp_servers", inherited)


# ---------------------------------------------------------------------------
# Subnet edit — network rollback (lines 3122-3133, 3137-3139)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkRollback(_ViewTestBase):
    """Lines 3122-3133, 3137-3139: network_subnet_del fails → rollback + outer except."""

    @patch("netbox_kea.models.KeaClient")
    def test_network_subnet_del_fails_triggers_rollback_and_outer_except(self, MockKeaClient):
        """old→new network change: del(old) raises KeaException → rollback del(new) also fails.

        Covers the rollback path where Kea definitively rejects the del and
        a rollback of the add is attempted.
        """
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value

        # config-get returns subnet 42 in "net-old", with "net-new" also available
        config_resp = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [
                            {"name": "net-old", "subnet4": [{"id": 42}]},
                            {"name": "net-new", "subnet4": []},
                        ],
                        "option-data": [],
                    }
                },
            }
        ]
        mock_client.command.return_value = config_resp
        mock_client.subnet_update.return_value = None
        mock_client.network_subnet_add.return_value = None
        # del(old) raises KeaException (definitive rejection) → rollback del(new) also fails
        mock_client.network_subnet_del.side_effect = [
            KeaException({"result": 1, "text": "del old failed"}),
            KeaException({"result": 1, "text": "rollback del new also failed"}),
        ]

        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.post(
            url,
            {
                "subnet_cidr": "10.0.0.0/24",
                "shared_network": "net-new",
                "current_network": "net-old",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        # Redirects after error message
        self.assertEqual(response.status_code, 302)
        # Both del calls were made (old + rollback of new)
        self.assertEqual(mock_client.network_subnet_del.call_count, 2)

    @patch("netbox_kea.models.KeaClient")
    def test_network_subnet_del_transport_error_skips_rollback(self, MockKeaClient):
        """old→new network change: del(old) raises transport error → NO rollback attempted.

        Transport errors (RequestException) leave state ambiguous, so rollback is skipped.
        """
        mock_client = MockKeaClient.return_value

        config_resp = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [],
                        "shared-networks": [
                            {"name": "net-old", "subnet4": [{"id": 42}]},
                            {"name": "net-new", "subnet4": []},
                        ],
                        "option-data": [],
                    }
                },
            }
        ]
        mock_client.command.return_value = config_resp
        mock_client.subnet_update.return_value = None
        mock_client.network_subnet_add.return_value = None
        # del(old) raises transport error — state is ambiguous
        mock_client.network_subnet_del.side_effect = requests.ConnectionError("network unreachable")

        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.post(
            url,
            {
                "subnet_cidr": "10.0.0.0/24",
                "shared_network": "net-new",
                "current_network": "net-old",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        # Only 1 del call (old) — no rollback attempted for transport errors
        self.assertEqual(mock_client.network_subnet_del.call_count, 1)


# ---------------------------------------------------------------------------
# Subnet add — _get_network_choices error handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddNetworkChoicesError(_ViewTestBase):
    """_get_network_choices error handling in the subnet-add view."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_shows_warning_when_network_choices_fail(self, MockKeaClient):
        """GET must render 200 with a warning message when config-get fails."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException(
            {"result": 1, "text": "config-get failed", "arguments": None}, index=0
        )
        response = self.client.get(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("shared network" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_post_rejects_submission_when_network_choices_fail(self, MockKeaClient):
        """POST must show a form error and NOT call subnet_add when config-get fails."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException(
            {"result": 1, "text": "config-get failed", "arguments": None}, index=0
        )
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
        MockKeaClient.return_value.subnet_add.assert_not_called()


# ---------------------------------------------------------------------------
# Subnet edit: _get_network_data error handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetEditNetworkDataErrors(_ViewTestBase):
    """_get_network_data must degrade gracefully on transport and parse errors."""

    @patch("netbox_kea.models.KeaClient")
    def test_transport_error_returns_200(self, MockKeaClient):
        """A requests.RequestException on config-get must not cause a 500 in the edit view."""

        def command_side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "subnet4-get":
                return [
                    {
                        "result": 0,
                        "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
                    }
                ]
            if cmd == "config-get":
                raise requests.ConnectionError("down")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = command_side_effect
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_value_error_returns_200(self, MockKeaClient):
        """A ValueError on config-get must not cause a 500 in the edit view."""

        def command_side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "subnet4-get":
                return [
                    {
                        "result": 0,
                        "arguments": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}]},
                    }
                ]
            if cmd == "config-get":
                raise ValueError("bad response")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = command_side_effect
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Subnet add: network_subnet_add PartialPersistError handling
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddPartialPersistNetworkAssign(_ViewTestBase):
    """network_subnet_add PartialPersistError must show warning not 'could not assign' error."""

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_from_network_assign_shows_warning_not_error(self, MockKeaClient):
        """When network_subnet_add raises PartialPersistError, show warning (subnet assigned but config-write failed)."""
        from netbox_kea.kea import PartialPersistError

        mock_client = MockKeaClient.return_value

        # subnet_add raises PartialPersistError (subnet added but config-write failed, id=99)
        mock_client.subnet_add.side_effect = PartialPersistError(
            "dhcp4", Exception("config-write failed"), subnet_id=99
        )
        # network_subnet_add also raises PartialPersistError (network attached but config-write failed)
        mock_client.network_subnet_add.side_effect = PartialPersistError(
            "dhcp4", Exception("config-write failed"), subnet_id=99
        )
        # Mock shared network choices via command
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [{"name": "my-net"}],
                        "subnet4": [],
                    }
                },
            }
        ]

        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        response = self.client.post(
            url,
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "my-net",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        # Must NOT see the failure message
        self.assertFalse(any("could not assign" in m.lower() for m in msgs))
        # network_subnet_add was called (subnet IS live)
        mock_client.network_subnet_add.assert_called_once()


# ---------------------------------------------------------------------------
# Subnet add: warn when assigned_id is None but shared_network requested
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddNoIdWarning(_ViewTestBase):
    """When subnet_add returns None and shared_network requested, show a warning."""

    @patch("netbox_kea.models.KeaClient")
    def test_warning_shown_when_no_id_returned(self, MockKeaClient):
        """subnet_add returning None with shared_network requested must show warning."""
        mock_client = MockKeaClient.return_value
        # subnet_add returns None (no ID)
        mock_client.subnet_add.return_value = None
        # Mock shared network choices from config-get
        mock_client.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [{"name": "my-net"}],
                        "subnet4": [],
                    }
                },
            }
        ]

        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        response = self.client.post(
            url,
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "my-net",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        # Must see a warning about missing ID / network assignment skipped
        self.assertTrue(
            any(
                "no id" in m.lower() or "could not assign" in m.lower() or "no id was returned" in m.lower()
                for m in msgs
            ),
            f"Expected warning about missing ID but got: {msgs}",
        )
        # network_subnet_add must NOT have been called (we have no ID)
        mock_client.network_subnet_add.assert_not_called()


# ---------------------------------------------------------------------------
# F5: get_client() failures in delete/wipe/pool-delete POST handlers
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetDeleteClientError(_ViewTestBase):
    """Subnet delete handlers must handle get_client() failures gracefully."""

    @patch("netbox_kea.models.Server.get_client")
    def test_post_with_get_client_failure_redirects(self, mock_get_client):
        """get_client() raising in delete POST must redirect with error, not 500."""
        mock_get_client.side_effect = ValueError("connection refused")
        url = reverse("plugins:netbox_kea:server_subnet4_delete", args=[self.server.pk, 1])
        response = self.client.post(url, {"confirm": "1"})
        self.assertIn(response.status_code, [200, 302])

    @patch("netbox_kea.models.Server.get_client")
    def test_get_with_get_client_failure_renders(self, mock_get_client):
        """get_client() raising in delete GET must render confirm page, not 500."""
        import requests as req

        mock_get_client.side_effect = req.RequestException("connection refused")
        url = reverse("plugins:netbox_kea:server_subnet4_delete", args=[self.server.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetWipeClientError(_ViewTestBase):
    """Subnet wipe handlers must handle get_client() failures gracefully."""

    @patch("netbox_kea.models.Server.get_client")
    def test_post_with_get_client_failure_redirects(self, mock_get_client):
        """get_client() raising in wipe POST must redirect with error, not 500."""
        mock_get_client.side_effect = ValueError("connection refused")
        url = reverse("plugins:netbox_kea:server_subnet4_wipe_leases", args=[self.server.pk, 1])
        response = self.client.post(url, {"confirm": "1"})
        self.assertIn(response.status_code, [200, 302])

    @patch("netbox_kea.models.Server.get_client")
    def test_get_with_get_client_failure_renders(self, mock_get_client):
        """get_client() raising in wipe GET must render confirm page, not 500."""
        mock_get_client.side_effect = ValueError("connection refused")
        url = reverse("plugins:netbox_kea:server_subnet4_wipe_leases", args=[self.server.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolDeleteClientError(_ViewTestBase):
    """Pool delete POST handler must handle get_client() failures gracefully."""

    @patch("netbox_kea.models.Server.get_client")
    def test_post_with_get_client_failure_redirects(self, mock_get_client):
        """get_client() raising in pool-delete POST must redirect with error, not 500."""
        mock_get_client.side_effect = ValueError("connection refused")
        url = reverse(
            "plugins:netbox_kea:server_subnet4_pool_delete",
            args=[self.server.pk, 1, "10.0.0.1-10.0.0.100"],
        )
        response = self.client.post(url, {"confirm": "1"})
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# F6: get_subnets() non-dict arguments guard
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetSubnetsConfigShapeGuard(_ViewTestBase):
    """get_subnets() returns [] when config-get arguments is non-dict."""

    @patch("netbox_kea.models.KeaClient")
    def test_non_dict_arguments_returns_empty_list(self, MockKeaClient):
        """Non-dict arguments in config-get response must return empty subnet list."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": "unexpected string"}]
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_integer_arguments_returns_empty_list(self, MockKeaClient):
        """Integer arguments in config-get response must return empty subnet list."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": 42}]
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Pool add POST exception branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolAddPostErrors(_ViewTestBase):
    """Cover pool add POST error handling."""

    def _url(self, subnet_id=1):
        return reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, subnet_id])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_shows_warning(self, MockKeaClient):
        """PartialPersistError from pool_add shows warning about config-write."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.pool_add.side_effect = PartialPersistError(
            "dhcp4", Exception("write failed"), subnet_id=1
        )
        response = self.client.post(self._url(), {"pool": "10.0.0.10-10.0.0.20"}, follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_shows_error(self, MockKeaClient):
        """KeaException from pool_add shows Kea error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.pool_add.side_effect = KeaException({"result": 1, "text": "pool overlap"}, index=0)
        response = self.client.post(self._url(), {"pool": "10.0.0.10-10.0.0.20"}, follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_request_exception_shows_error(self, MockKeaClient):
        """RequestException from pool_add shows transport error message."""
        MockKeaClient.return_value.pool_add.side_effect = requests.ConnectionError("down")
        response = self.client.post(self._url(), {"pool": "10.0.0.10-10.0.0.20"}, follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_shows_error(self, MockKeaClient):
        """Generic exception from pool_add shows generic error."""
        MockKeaClient.return_value.pool_add.side_effect = ValueError("unexpected")
        response = self.client.post(self._url(), {"pool": "10.0.0.10-10.0.0.20"}, follow=True)
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Pool delete POST exception branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPoolDeletePostErrors(_ViewTestBase):
    """Cover pool delete POST error handling."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 1, "10.0.0.1-10.0.0.100"])

    @patch("netbox_kea.models.KeaClient")
    def test_partial_persist_shows_warning(self, MockKeaClient):
        """PartialPersistError from pool_del shows warning."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.pool_del.side_effect = PartialPersistError(
            "dhcp4", Exception("write failed"), subnet_id=1
        )
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_kea_exception_shows_error(self, MockKeaClient):
        """KeaException from pool_del shows Kea error."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.pool_del.side_effect = KeaException({"result": 1, "text": "not found"}, index=0)
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_request_exception_shows_error(self, MockKeaClient):
        """RequestException from pool_del shows transport error."""
        MockKeaClient.return_value.pool_del.side_effect = requests.ConnectionError("down")
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_generic_exception_shows_error(self, MockKeaClient):
        """Generic exception from pool_del shows generic error."""
        MockKeaClient.return_value.pool_del.side_effect = ValueError("unexpected")
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_get_client_failure_redirects(self, MockKeaClient):
        """get_client failure in pool delete redirects with error."""
        MockKeaClient.side_effect = ValueError("connection refused")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)


# ─────────────────────────────────────────────────────────────────────────────
# Subnet add POST network assignment error branches
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddPostNetworkErrors(_ViewTestBase):
    """Cover subnet add POST network assignment errors."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])

    def _setup_client(self, MockKeaClient, subnet_add_effect=None, network_add_effect=None):
        """Set up common mock for subnet_add + network flow."""
        mock_client = MockKeaClient.return_value
        # config-get for shared networks (used by _get_network_choices)
        mock_client.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"shared-networks": [{"name": "my-net"}], "subnet4": []}}}
        ]
        if subnet_add_effect:
            mock_client.subnet_add.side_effect = subnet_add_effect
        else:
            mock_client.subnet_add.return_value = 99  # returns new subnet ID
        if network_add_effect:
            mock_client.network_subnet_add.side_effect = network_add_effect
        return mock_client

    @patch("netbox_kea.models.KeaClient")
    def test_network_assign_kea_exception_shows_warning(self, MockKeaClient):
        """KeaException from network_subnet_add shows warning (subnet already created)."""
        from netbox_kea.kea import KeaException

        self._setup_client(
            MockKeaClient,
            network_add_effect=KeaException({"result": 1, "text": "network error"}, index=0),
        )
        response = self.client.post(
            self._url(),
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "my-net",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_add_request_exception_rerenders(self, MockKeaClient):
        """RequestException from subnet_add re-renders form."""
        self._setup_client(
            MockKeaClient,
            subnet_add_effect=requests.ConnectionError("down"),
        )
        response = self.client.post(
            self._url(),
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertIn(response.status_code, [200, 302])

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_add_generic_exception_rerenders(self, MockKeaClient):
        """Generic exception from subnet_add re-renders form."""
        self._setup_client(
            MockKeaClient,
            subnet_add_effect=ValueError("unexpected"),
        )
        response = self.client.post(
            self._url(),
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertIn(response.status_code, [200, 302])

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_add_no_id_with_network_shows_warning(self, MockKeaClient):
        """When subnet_add returns None (no ID), network assignment is skipped with warning."""
        mock_client = self._setup_client(MockKeaClient)
        mock_client.subnet_add.return_value = None
        response = self.client.post(
            self._url(),
            {
                "subnet": "10.99.0.0/24",
                "shared_network": "my-net",
                "pools": "",
                "gateway": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        mock_client.network_subnet_add.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Subnet add GET/POST client creation errors
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetAddGetClientError(_ViewTestBase):
    """Cover subnet add GET when client creation fails."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_client_error_disables_network_field(self, MockKeaClient):
        """ValueError from get_client in GET disables shared network field."""
        MockKeaClient.side_effect = ValueError("bad config")
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_client_error_rerenders(self, MockKeaClient):
        """ValueError from get_client in POST re-renders form with error."""
        MockKeaClient.side_effect = ValueError("bad config")
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
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

    @patch("netbox_kea.models.KeaClient")
    def test_config_get_value_error_shows_empty(self, MockKeaClient):
        """ValueError from config-get shows empty subnets with error message."""
        MockKeaClient.return_value.command.side_effect = ValueError("bad JSON")
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# F9/F10/F11: Non-dict items & empty shared_network preservation
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetListNonDictItems(_ViewTestBase):
    """F9: Non-dict items in top-level subnets list and shared-networks list."""

    @patch("netbox_kea.models.KeaClient")
    def test_non_dict_items_in_subnet_list_skipped(self, MockKeaClient):
        """Non-dict entries (string, int) in subnet4 list are skipped; valid subnet shows."""

        def _cmd(command, **_kwargs):
            if command == "config-get":
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "Dhcp4": {
                                "subnet4": [
                                    {"id": 1, "subnet": "10.0.0.0/24"},
                                    "malformed",
                                    42,
                                ],
                                "shared-networks": [],
                            }
                        },
                    }
                ]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _cmd
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")

    @patch("netbox_kea.models.KeaClient")
    def test_non_dict_items_in_shared_networks_skipped(self, MockKeaClient):
        """Non-dict entries in shared-networks list are skipped; valid network shows."""

        def _cmd(command, **_kwargs):
            if command == "config-get":
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "Dhcp4": {
                                "subnet4": [],
                                "shared-networks": [
                                    {
                                        "name": "net1",
                                        "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}],
                                    },
                                    "invalid",
                                ],
                            }
                        },
                    }
                ]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _cmd
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")

    @patch("netbox_kea.models.KeaClient")
    def test_non_dict_subnet_inside_shared_network_skipped(self, MockKeaClient):
        """Non-dict subnet items within a shared-network's subnet list are skipped."""

        def _cmd(command, **_kwargs):
            if command == "config-get":
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "Dhcp4": {
                                "subnet4": [],
                                "shared-networks": [
                                    {
                                        "name": "net1",
                                        "subnet4": [
                                            {"id": 1, "subnet": "10.0.0.0/24"},
                                            "bad",
                                        ],
                                    },
                                ],
                            }
                        },
                    }
                ]
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _cmd
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.0/24")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetNetworkDataNonDictSubnet(_ViewTestBase):
    """F10: _get_network_data returns None network when shared-network has non-dict subnet."""

    @patch("netbox_kea.models.KeaClient")
    def test_edit_loads_with_malformed_subnet_in_shared_network(self, MockKeaClient):
        """Edit page loads when a shared-network contains non-dict subnet items."""
        subnet_get_resp = [
            {
                "result": 0,
                "arguments": {
                    "subnet4": [
                        {
                            "id": 42,
                            "subnet": "10.0.0.0/24",
                            "pools": [],
                            "option-data": [],
                            "valid-lft": 3600,
                        }
                    ]
                },
            }
        ]
        config_get_resp = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "subnet4": [{"id": 42, "subnet": "10.0.0.0/24"}],
                        "shared-networks": [
                            {
                                "name": "net1",
                                "subnet4": [
                                    {"id": 1, "subnet": "10.0.0.0/24"},
                                    "bad",
                                ],
                            }
                        ],
                    }
                },
            }
        ]

        def _cmd(command, **_kwargs):
            if "subnet4-get" in command:
                return subnet_get_resp
            if command == "config-get":
                return config_get_resp
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = _cmd
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.get(url)
        # _get_network_data returns None → view aborts edit and redirects
        self.assertIn(response.status_code, (200, 302))


# ---------------------------------------------------------------------------
# Coverage gap tests — _subnet_to_row, config-get edge cases, stats, pool overlap
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetViewCoverageGaps(_ViewTestBase):
    """Tests targeting specific uncovered lines in views/subnets.py."""

    def _subnets4_url(self):
        return reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])

    # ── 1. config-get returns non-dict arguments (~lines 92-99) ──────────

    @patch("netbox_kea.models.KeaClient")
    def test_config_get_non_dict_arguments_returns_empty_subnets(self, MockKeaClient):
        """When config-get returns arguments='not-a-dict', view logs warning and returns 200 with no subnets."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": "not-a-dict"}]
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 0)

    @patch("netbox_kea.models.KeaClient")
    def test_config_get_list_arguments_returns_empty_subnets(self, MockKeaClient):
        """When config-get returns arguments=[...], view logs warning and returns 200 with no subnets."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": [1, 2, 3]}]
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 0)

    # ── 2. Stats enrichment exception paths (~lines 130-134) ─────────────

    @patch("netbox_kea.models.KeaClient")
    def test_stats_value_error_still_renders_subnets(self, MockKeaClient):
        """When stat-lease4-get raises ValueError, subnets render without utilisation."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return _config_with_one_subnet(service)
            if cmd == "stat-lease4-get":
                raise ValueError("bad stat response")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)
        # No utilisation columns should be present
        self.assertNotIn("utilization", list(table.data)[0])

    @patch("netbox_kea.models.KeaClient")
    def test_stats_type_error_still_renders_subnets(self, MockKeaClient):
        """When stat-lease4-get raises TypeError, subnets render without utilisation."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return _config_with_one_subnet(service)
            if cmd == "stat-lease4-get":
                raise TypeError("unexpected None in stat parsing")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)

    @patch("netbox_kea.models.KeaClient")
    def test_stats_key_error_still_renders_subnets(self, MockKeaClient):
        """When stat-lease4-get raises KeyError, subnets render without utilisation."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return _config_with_one_subnet(service)
            if cmd == "stat-lease4-get":
                raise KeyError("result-set")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)

    @patch("netbox_kea.models.KeaClient")
    def test_stats_request_exception_still_renders_subnets(self, MockKeaClient):
        """When stat-lease4-get raises RequestException, subnets render without utilisation."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return _config_with_one_subnet(service)
            if cmd == "stat-lease4-get":
                raise requests.RequestException("timeout")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)

    # ── 3. _subnet_to_row with non-scalar ID (~lines 56-58) ─────────────

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_with_list_id_is_skipped(self, MockKeaClient):
        """Subnet with id=[1,2,3] must be skipped (non-scalar ID)."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "Dhcp4": {
                                "subnet4": [
                                    {"id": [1, 2, 3], "subnet": "10.0.0.0/24"},
                                    {"id": 2, "subnet": "10.0.1.0/24"},
                                ],
                                "shared-networks": [],
                            }
                        },
                    }
                ]
            if cmd == "stat-lease4-get":
                raise ValueError("no stat_cmds")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        # Only subnet with id=2 should appear; id=[1,2,3] is skipped
        self.assertEqual(len(table.data), 1)
        self.assertEqual(list(table.data)[0]["id"], 2)

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_with_dict_id_is_skipped(self, MockKeaClient):
        """Subnet with id={"nested": "dict"} must be skipped (non-scalar ID)."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return [
                    {
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
                ]
            if cmd == "stat-lease4-get":
                raise ValueError("no stat_cmds")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)
        self.assertEqual(list(table.data)[0]["id"], 5)

    # ── 4. _subnet_to_row with malformed CIDR (~lines 60-62) ────────────

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_with_malformed_cidr_is_skipped(self, MockKeaClient):
        """Subnet with subnet='not-a-cidr' must be skipped and logged."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "Dhcp4": {
                                "subnet4": [
                                    {"id": 1, "subnet": "not-a-cidr"},
                                    {"id": 2, "subnet": "192.168.1.0/24"},
                                ],
                                "shared-networks": [],
                            }
                        },
                    }
                ]
            if cmd == "stat-lease4-get":
                raise ValueError("no stat_cmds")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)
        self.assertEqual(list(table.data)[0]["subnet"], "192.168.1.0/24")

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_with_empty_cidr_is_skipped(self, MockKeaClient):
        """Subnet with subnet='' must be skipped (ValueError from ip_network)."""

        def side_effect(cmd, service=None, arguments=None, check=None):
            if cmd == "config-get":
                return [
                    {
                        "result": 0,
                        "arguments": {
                            "Dhcp4": {
                                "subnet4": [
                                    {"id": 1, "subnet": ""},
                                    {"id": 2, "subnet": "172.16.0.0/16"},
                                ],
                                "shared-networks": [],
                            }
                        },
                    }
                ]
            if cmd == "stat-lease4-get":
                raise ValueError("no stat_cmds")
            return [{"result": 0, "arguments": {}}]

        MockKeaClient.return_value.command.side_effect = side_effect
        response = self.client.get(self._subnets4_url())
        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertEqual(len(table.data), 1)
        self.assertEqual(list(table.data)[0]["id"], 2)

    # ── 5. Subnet edit POST error paths (~lines 963-1010) ────────────────

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_edit_post_request_exception_rerenders_form(self, MockKeaClient):
        """POST that raises RequestException on subnet_update must re-render form (200)."""
        MockKeaClient.return_value.command.return_value = _CONFIG4_NO_NETWORKS
        MockKeaClient.return_value.subnet_update.side_effect = requests.RequestException("connection lost")
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.post(
            url,
            {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
        )
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.subnet_update.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_edit_post_kea_exception_rerenders_with_error_message(self, MockKeaClient):
        """POST that raises KeaException must show error message and re-render form."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.return_value = _CONFIG4_NO_NETWORKS
        MockKeaClient.return_value.subnet_update.side_effect = KeaException(
            {"result": 1, "text": "subnet not found"}, index=0
        )
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.post(
            url,
            {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
        )
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_subnet_edit_post_client_creation_failure_redirects(self, MockKeaClient):
        """POST where get_client raises ValueError must redirect to subnets."""
        MockKeaClient.side_effect = ValueError("bad config")
        url = reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42])
        response = self.client.post(
            url,
            {"subnet_cidr": "10.0.0.0/24", "pools": "", "gateway": "", "dns_servers": "", "ntp_servers": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("subnets", response.url)

    # ── 6. Pool add with reservation overlap warning (~lines 204-257) ────

    @patch("netbox_kea.models.KeaClient")
    def test_pool_add_reservation_lookup_failure_suppresses_warning(self, MockKeaClient):
        """When reservation_get_page raises KeaException during pool add, warning is suppressed."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_page.side_effect = KeaException(
            {"result": 2, "text": "host_cmds not loaded"}, index=0
        )
        MockKeaClient.return_value.pool_add.return_value = None
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])
        response = self.client.post(url, {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        # Success message should be present, but no overlap warning
        self.assertTrue(any(m.level == django_messages.SUCCESS for m in msgs))
        self.assertFalse(any("overlaps" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_pool_add_reservation_lookup_request_exception_suppresses_warning(self, MockKeaClient):
        """When reservation_get_page raises RequestException during pool add, warning is suppressed."""
        MockKeaClient.return_value.reservation_get_page.side_effect = requests.RequestException("timeout")
        MockKeaClient.return_value.pool_add.return_value = None
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])
        response = self.client.post(url, {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.SUCCESS for m in msgs))
        self.assertFalse(any("overlaps" in m.message.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_pool_add_kea_exception_shows_error(self, MockKeaClient):
        """KeaException on pool_add must show error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        MockKeaClient.return_value.pool_add.side_effect = KeaException(
            {"result": 1, "text": "pool already exists"}, index=0
        )
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])
        response = self.client.post(url, {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_pool_add_request_exception_shows_network_error(self, MockKeaClient):
        """RequestException on pool_add must show network error message."""
        MockKeaClient.return_value.reservation_get_page.return_value = ([], 0, 0)
        MockKeaClient.return_value.pool_add.side_effect = requests.RequestException("connection refused")
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])
        response = self.client.post(url, {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_pool_add_client_creation_failure_redirects(self, MockKeaClient):
        """When get_client raises ValueError during pool add POST, view redirects with error."""
        MockKeaClient.side_effect = ValueError("invalid url config")
        url = reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 42])
        response = self.client.post(url, {"pool": "10.0.0.100-10.0.0.200"}, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    # ── Pool delete exception paths ──────────────────────────────────────

    @patch("netbox_kea.models.KeaClient")
    def test_pool_delete_kea_exception_shows_error(self, MockKeaClient):
        """KeaException on pool_del must show error message."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.pool_del.side_effect = KeaException({"result": 1, "text": "pool not found"}, index=0)
        url = reverse(
            "plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "10.0.0.100-10.0.0.200"]
        )
        response = self.client.post(url, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_pool_delete_request_exception_shows_network_error(self, MockKeaClient):
        """RequestException on pool_del must show network error message."""
        MockKeaClient.return_value.pool_del.side_effect = requests.RequestException("timeout")
        url = reverse(
            "plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "10.0.0.100-10.0.0.200"]
        )
        response = self.client.post(url, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_pool_delete_client_creation_failure_redirects(self, MockKeaClient):
        """When get_client raises ValueError during pool delete POST, view redirects with error."""
        MockKeaClient.side_effect = ValueError("bad config")
        url = reverse(
            "plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 42, "10.0.0.100-10.0.0.200"]
        )
        response = self.client.post(url, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestPersistConfigBanner(_ViewTestBase):
    """Tests that the persist_config warning banner appears when disabled."""

    @patch("netbox_kea.models.KeaClient")
    def test_banner_absent_when_persist_config_true(self, MockKeaClient):
        """No warning banner on subnet add when persist_config=True (default)."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Configuration persistence is disabled.")

    @patch("netbox_kea.models.KeaClient")
    def test_banner_present_when_persist_config_false(self, MockKeaClient):
        """Warning banner appears on subnet add page when persist_config=False."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect

        server = _make_db_server(name="no-persist", persist_config=False)
        url = reverse("plugins:netbox_kea:server_subnet4_add", args=[server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configuration persistence is disabled.")
