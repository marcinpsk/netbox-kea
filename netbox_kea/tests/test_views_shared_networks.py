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

from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse

from .utils import _PLUGINS_CONFIG, _kea_command_side_effect, _make_db_server, _ViewTestBase

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


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetworks4View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/shared_networks4/"""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_networks4", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_shows_shared_network_name(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        self.assertContains(response, "net-alpha")

    @patch("netbox_kea.models.KeaClient")
    def test_shows_subnet_count(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        # 2 subnets in net-alpha — check the Subnets column header is present
        self.assertContains(response, "Subnets")

    @patch("netbox_kea.models.KeaClient")
    def test_shows_subnet_cidrs(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        self.assertContains(response, "10.0.0.0/24")
        self.assertContains(response, "10.0.1.0/24")

    @patch("netbox_kea.models.KeaClient")
    def test_empty_table_when_no_shared_networks(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "net-alpha")

    def test_get_with_dhcp4_disabled_redirects(self):
        v6_only = _make_db_server(name="v6-only-sn", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[v6_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(v6_only.pk), response.url)

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerSharedNetworks4View

        MockKeaClient.return_value.command.return_value = _SHARED_NETWORKS_CONFIG_V4
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerSharedNetworks4View.tab)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetworks6View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/shared_networks6/"""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_networks6", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V6
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_shows_shared_network_name(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V6
        response = self.client.get(self._url())
        self.assertContains(response, "net-beta")

    @patch("netbox_kea.models.KeaClient")
    def test_shows_subnet_cidrs(self, MockKeaClient):
        mock_client = MockKeaClient.return_value
        mock_client.command.return_value = _SHARED_NETWORKS_CONFIG_V6
        response = self.client.get(self._url())
        self.assertContains(response, "2001:db8::/48")

    @patch("netbox_kea.models.KeaClient")
    def test_get_sets_tab_in_context(self, MockKeaClient):
        """F2: GET response must include 'tab' in context for tab bar highlighting."""
        from netbox_kea.views import ServerSharedNetworks6View

        MockKeaClient.return_value.command.return_value = _SHARED_NETWORKS_CONFIG_V6
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context["tab"], ServerSharedNetworks6View.tab)


# ---------------------------------------------------------------------------
# Shared Network Add / Delete views (TDD — RED until views + URLs implemented)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4AddView(_ViewTestBase):
    """Tests for ServerSharedNetwork4AddView: GET form + POST create."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_network4_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200_with_form(self, MockKeaClient):
        """GET must render the add-network form with status 200."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_creates_network(self, MockKeaClient):
        """POST with valid name must call network_add and redirect."""
        MockKeaClient.return_value.network_add.return_value = None
        response = self.client.post(self._url(), {"name": "net-prod"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.network_add.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_add_with_correct_version(self, MockKeaClient):
        """POST must call network_add with version=4."""
        MockKeaClient.return_value.network_add.return_value = None
        self.client.post(self._url(), {"name": "net-prod"})
        call_args = MockKeaClient.return_value.network_add.call_args
        version = self._call_version(call_args)
        self.assertEqual(version, 4)

    @patch("netbox_kea.models.KeaClient")
    def test_post_empty_name_shows_form_errors(self, MockKeaClient):
        """POST with empty name must re-render form (no Kea call)."""
        response = self.client.post(self._url(), {"name": ""})
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.network_add.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST that raises KeaException must redirect with an error (no 500)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.network_add.side_effect = KeaException(
            {"result": 1, "text": "subnet_cmds not loaded"}, index=0
        )
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
    """Tests for ServerSharedNetwork6AddView — verifies v6 variant uses version=6."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_shared_network6_add", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET must render the add-network form with status 200."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_add_with_version_6(self, MockKeaClient):
        """POST must call network_add with version=6."""
        MockKeaClient.return_value.network_add.return_value = None
        self.client.post(self._url(), {"name": "net6-prod"})
        call_args = MockKeaClient.return_value.network_add.call_args
        version = self._call_version(call_args)
        self.assertEqual(version, 6)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4DeleteView(_ViewTestBase):
    """Tests for ServerSharedNetwork4DeleteView: GET confirm + POST delete."""

    def _url(self, name="net-alpha"):
        return reverse("plugins:netbox_kea:server_shared_network4_delete", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200_with_confirmation_page(self, MockKeaClient):
        """GET must render a confirmation page mentioning the network name."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "net-alpha")

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_del_and_redirects(self, MockKeaClient):
        """POST must call network_del and redirect to the shared networks tab."""
        MockKeaClient.return_value.network_del.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.network_del.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_correct_version_and_name(self, MockKeaClient):
        """POST must call network_del with version=4 and the correct network name."""
        MockKeaClient.return_value.network_del.return_value = None
        self.client.post(self._url(name="net-alpha"))
        call_args = MockKeaClient.return_value.network_del.call_args
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        version = self._call_version(call_args)
        name = kwargs.get("name") or (args[1] if len(args) > 1 else None)
        self.assertEqual(version, 4)
        self.assertEqual(name, "net-alpha")

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_redirects_with_error(self, MockKeaClient):
        """POST that raises KeaException must redirect with an error (no 500)."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.network_del.side_effect = KeaException(
            {"result": 1, "text": "network not found"}, index=0
        )
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
    """Tests for ServerSharedNetwork6DeleteView — verifies v6 variant uses version=6."""

    def _url(self, name="net-beta"):
        return reverse("plugins:netbox_kea:server_shared_network6_delete", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET must render confirmation page with status 200."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_del_with_version_6(self, MockKeaClient):
        """POST must call network_del with version=6."""
        MockKeaClient.return_value.network_del.return_value = None
        self.client.post(self._url(name="net-beta"))
        call_args = MockKeaClient.return_value.network_del.call_args
        version = self._call_version(call_args)
        self.assertEqual(version, 6)


# ─────────────────────────────────────────────────────────────────────────────
# TestServerSharedNetwork4EditView (F2b)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSharedNetwork4EditView(_ViewTestBase):
    """Tests for ServerSharedNetwork4EditView: GET form + POST update."""

    def _url(self, name="prod-net"):
        return reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET renders the edit form with status 200."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {"name": "prod-net", "description": "Old", "option-data": [], "subnet4": []}
                        ],
                        "subnet4": [],
                    }
                },
            }
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_valid_calls_network_update_and_redirects(self, MockKeaClient):
        """POST with valid data calls network_update and redirects."""
        MockKeaClient.return_value.network_update.return_value = None
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "Updated description",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        MockKeaClient.return_value.network_update.assert_called_once()

    @patch("netbox_kea.models.KeaClient")
    def test_post_passes_version_4_to_network_update(self, MockKeaClient):
        """POST must call network_update with version=4."""
        MockKeaClient.return_value.network_update.return_value = None
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        call_args = MockKeaClient.return_value.network_update.call_args
        version = self._call_version(call_args)
        self.assertEqual(version, 4)

    @patch("netbox_kea.models.KeaClient")
    def test_post_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST that raises KeaException must redirect, show a generic error, and not leak raw Kea text."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.network_update.side_effect = KeaException(
            {"result": 1, "text": "config error"}, index=0
        )
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
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

    @patch("netbox_kea.models.KeaClient")
    def test_post_partial_persist_error_shows_warning(self, MockKeaClient):
        """POST that raises PartialPersistError redirects with a warning (no 500)."""
        from netbox_kea.kea import PartialPersistError

        MockKeaClient.return_value.network_update.side_effect = PartialPersistError("dhcp4", Exception("write failed"))
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
            follow=True,
        )
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
    """Tests for ServerSharedNetwork6EditView — verifies version=6."""

    def _url(self, name="prod-net6"):
        return reverse("plugins:netbox_kea:server_shared_network6_edit", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_200(self, MockKeaClient):
        """GET returns 200 for DHCPv6 edit view."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp6": {
                        "shared-networks": [{"name": "prod-net6", "description": "", "option-data": [], "subnet6": []}],
                        "subnet6": [],
                    }
                },
            }
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_network_update_with_version_6(self, MockKeaClient):
        """POST must call network_update with version=6."""
        MockKeaClient.return_value.network_update.return_value = None
        MockKeaClient.return_value.command.return_value = _CONFIG6_WITH_PROD_NET
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
        call_args = MockKeaClient.return_value.network_update.call_args
        version = self._call_version(call_args)
        self.assertEqual(version, 6)


# ---------------------------------------------------------------------------
# Tests for shared network POST — option-data preservation
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkOptionDataPreservation(_ViewTestBase):
    """Verify non-DNS/NTP option-data entries are preserved on shared network save."""

    def _url(self, name="prod-net"):
        return reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, name])

    @patch("netbox_kea.models.KeaClient")
    def test_post_preserves_non_dns_options(self, MockKeaClient):
        """network_update receives non-DNS/NTP option-data that was fetched from Kea."""
        custom_option = {"name": "vendor-specific", "data": "deadbeef"}
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {
                                "name": "prod-net",
                                "option-data": [
                                    {"name": "domain-name-servers", "data": "8.8.8.8"},
                                    custom_option,
                                ],
                                "subnet4": [],
                            }
                        ],
                        "subnet4": [],
                    }
                },
            }
        ]
        MockKeaClient.return_value.network_update.return_value = None
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
        call_kwargs = MockKeaClient.return_value.network_update.call_args
        kwargs = call_kwargs.kwargs or call_kwargs[1]
        options = kwargs.get("options", [])
        option_names = [o["name"] for o in options]
        # The custom non-DNS option must be preserved.
        self.assertIn("vendor-specific", option_names)
        # The new DNS from the form must also be present.
        self.assertIn("domain-name-servers", option_names)

    @patch("netbox_kea.models.KeaClient")
    def test_post_replaces_dns_servers_not_duplicates(self, MockKeaClient):
        """Old DNS option from Kea is dropped; only the form-supplied DNS value is written."""
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {
                                "name": "prod-net",
                                "option-data": [{"name": "domain-name-servers", "data": "8.8.8.8"}],
                                "subnet4": [],
                            }
                        ],
                        "subnet4": [],
                    }
                },
            }
        ]
        MockKeaClient.return_value.network_update.return_value = None
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
        call_kwargs = MockKeaClient.return_value.network_update.call_args
        kwargs = call_kwargs.kwargs or call_kwargs[1]
        options = kwargs.get("options", [])
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

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_when_network_not_found(self, MockKeaClient):
        """GET must redirect when config-get returns a config with no matching network."""
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"shared-networks": [], "subnet4": []}}}
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @patch("netbox_kea.models.KeaClient")
    def test_get_redirects_when_fetch_raises_kea_exception(self, MockKeaClient):
        """GET must redirect when config-get raises KeaException."""
        from netbox_kea.kea import KeaException

        MockKeaClient.return_value.command.side_effect = KeaException({"result": 1, "text": "err"}, index=0)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_post_aborts_when_reload_returns_empty(self, MockKeaClient):
        """POST aborts with error when _fetch_network (reload before write) returns empty."""
        # POST path has only ONE _fetch_network call (for reload). Make it return empty
        # (prod-net not in shared-networks) to trigger the abort path.
        MockKeaClient.return_value.command.return_value = [
            {"result": 0, "arguments": {"Dhcp4": {"shared-networks": [], "subnet4": []}}}
        ]
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        # Must re-render (not crash) with error message
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not reload")

    @patch("netbox_kea.models.KeaClient")
    def test_post_sets_ntp_servers_option(self, MockKeaClient):
        """POST with ntp_servers populates option-data with ntp-servers entry."""
        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        MockKeaClient.return_value.network_update.return_value = None
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
        call_kwargs = MockKeaClient.return_value.network_update.call_args.kwargs or {}
        options = call_kwargs.get("options", [])
        ntp_opts = [o for o in options if o.get("name") == "ntp-servers"]
        self.assertEqual(len(ntp_opts), 1)
        self.assertEqual(ntp_opts[0]["data"], "10.0.0.1")

    @patch("netbox_kea.models.KeaClient")
    def test_post_generic_exception_rerenders(self, MockKeaClient):
        """POST that raises generic Exception must not crash (no 500)."""
        import requests as _requests

        MockKeaClient.return_value.command.return_value = _CONFIG4_WITH_PROD_NET
        MockKeaClient.return_value.network_update.side_effect = _requests.RequestException("unexpected")
        response = self.client.post(
            self._url(),
            {
                "name": "prod-net",
                "description": "x",
                "interface": "",
                "relay_addresses": "",
                "dns_servers": "",
                "ntp_servers": "",
            },
        )
        self.assertIn(response.status_code, (200, 302))

    @patch("netbox_kea.models.KeaClient")
    def test_post_invalid_form_rerenders(self, MockKeaClient):
        """POST with missing required field must re-render the form (200)."""
        response = self.client.post(self._url(), {"description": "x"})
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Shared network list — dhcp disabled + null config
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkListEdgeCases(_ViewTestBase):
    """Lines 1237, 1241: shared network list when dhcp disabled or null config."""

    @patch("netbox_kea.models.KeaClient")
    def test_dhcp4_disabled_redirects(self, MockKeaClient):
        """Line 1278: when dhcp4 disabled, get() returns redirect (302)."""
        server_no4 = _make_db_server(name="no-dhcp4", server_url="https://kea.example.com", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[server_no4.pk])
        MockKeaClient.return_value.command.side_effect = _kea_command_side_effect
        response = self.client.get(url)
        # check_dhcp_enabled returns redirect when dhcp4=False
        self.assertEqual(response.status_code, 302)

    @patch("netbox_kea.models.KeaClient")
    def test_null_config_returns_empty(self, MockKeaClient):
        """Line 1241: get_children returns [] when config-get returns null args."""
        MockKeaClient.return_value.command.return_value = [{"result": 0, "arguments": None}]
        url = reverse("plugins:netbox_kea:server_shared_networks4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# SharedNetworkAdd/Delete — generic exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkCRUDGenericException(_ViewTestBase):
    """Lines 1371-1373, 1426-1428: generic exception on add/delete shows error."""

    @patch("netbox_kea.models.KeaClient")
    def test_add_generic_exception_shows_error(self, MockKeaClient):
        """Lines 1371-1373: transport exception on network_add redirects with error."""
        import requests as _requests

        MockKeaClient.return_value.network_add.side_effect = _requests.RequestException("connection reset")
        url = reverse("plugins:netbox_kea:server_shared_network4_add", args=[self.server.pk])
        response = self.client.post(url, {"name": "new-net"}, follow=True)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("internal error" in m.lower() for m in msgs))

    @patch("netbox_kea.models.KeaClient")
    def test_delete_generic_exception_shows_error(self, MockKeaClient):
        """Lines 1426-1428: transport exception on network_del redirects with error."""
        import requests as _requests

        MockKeaClient.return_value.network_del.side_effect = _requests.RequestException("timeout")
        url = reverse("plugins:netbox_kea:server_shared_network4_delete", args=[self.server.pk, "old-net"])
        response = self.client.post(url, {}, follow=True)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("internal error" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Shared network edit — option parsing in GET
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworkEditOptionParsing(_ViewTestBase):
    """Lines 1488-1492: GET populates dns_servers/ntp_servers from option-data."""

    @patch("netbox_kea.models.KeaClient")
    def test_get_populates_dns_and_ntp_from_option_data(self, MockKeaClient):
        MockKeaClient.return_value.command.return_value = [
            {
                "result": 0,
                "arguments": {
                    "Dhcp4": {
                        "shared-networks": [
                            {
                                "name": "prod-net",
                                "option-data": [
                                    {"name": "domain-name-servers", "data": "8.8.8.8"},
                                    {"name": "ntp-servers", "data": "192.0.2.1"},
                                ],
                                "subnet4": [],
                            }
                        ]
                    }
                },
            }
        ]
        url = reverse("plugins:netbox_kea:server_shared_network4_edit", args=[self.server.pk, "prod-net"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "8.8.8.8")
        self.assertContains(response, "192.0.2.1")


# ---------------------------------------------------------------------------
# Shared networks tab disabled (line 1237)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSharedNetworksTabDisabled(_ViewTestBase):
    """Line 1237: server.dhcp4=False → get_children returns [] immediately."""

    def test_dhcp4_disabled_returns_empty_children(self):
        """Line 1237: dhcp4=False → get_children returns [] immediately (defensive guard)."""
        from django.test import RequestFactory

        from netbox_kea.views import ServerSharedNetworks4View

        server = _make_db_server(name="no-dhcp4-children", dhcp4=False, dhcp6=True)
        view = ServerSharedNetworks4View()
        request = RequestFactory().get("/")
        request.user = self.user
        result = view.get_children(request, server)
        self.assertEqual(result, [])
