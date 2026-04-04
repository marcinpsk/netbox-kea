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
from django.test import override_settings
from django.urls import reverse

from .utils import _PLUGINS_CONFIG, _kea_command_side_effect, _ViewTestBase


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDHCP4EnableView(_ViewTestBase):
    """Tests for ServerDHCP4EnableView (GET confirmation + POST enable)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_enable", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_confirmation_page(self, MockKeaClient):
        """GET must render the enable confirmation page with dhcp_version=4."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "4")

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_dhcp_enable_and_redirects(self, MockKeaClient):
        """POST must call dhcp_enable('dhcp4') and redirect to status tab."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_enable.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        mock_client.dhcp_enable.assert_called_once_with("dhcp4")

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST with KeaException must flash a generic error; raw Kea text must not be leaked."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect
        mock_client.dhcp_enable.side_effect = KeaException(
            {"result": 1, "text": "kea_secret_internal_url=https://kea.internal:8080/api/"},
            index=0,
        )
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("kea_secret_internal_url", response.content.decode())
        self.assertIn("Failed to enable DHCPv4:", response.content.decode())

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_unexpected_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST with transport exception must not leak the raw exception text."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect
        mock_client.dhcp_enable.side_effect = requests.RequestException(
            "kea_secret_transport_marker=https://kea.internal/"
        )
        response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("kea_secret_transport_marker", response.content.decode())
        self.assertIn("An internal error occurred.", response.content.decode())

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
class TestServerDHCP6EnableView(_ViewTestBase):
    """Tests for ServerDHCP6EnableView — verifies v6 variant uses dhcp6 service."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp6_enable", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_dhcp_enable_v6(self, MockKeaClient):
        """POST must call dhcp_enable('dhcp6')."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_enable.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        mock_client.dhcp_enable.assert_called_once_with("dhcp6")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDHCP4DisableView(_ViewTestBase):
    """Tests for ServerDHCP4DisableView (GET form + POST disable with optional max_period)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_disable", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_get_returns_form_page(self, MockKeaClient):
        """GET must render the disable form with max_period field."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "max_period")

    @patch("netbox_kea.models.KeaClient")
    def test_post_without_max_period_calls_disable_no_period(self, MockKeaClient):
        """POST without max_period must call dhcp_disable(service) with max_period=None."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_disable.return_value = None
        response = self.client.post(self._url(), {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        mock_client.dhcp_disable.assert_called_once_with("dhcp4", max_period=None)

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_max_period_passes_value(self, MockKeaClient):
        """POST with max_period=300 must pass max_period=300 to dhcp_disable."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_disable.return_value = None
        response = self.client.post(self._url(), {"confirm": "1", "max_period": "300"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        mock_client.dhcp_disable.assert_called_once_with("dhcp4", max_period=300)

    @patch("netbox_kea.models.KeaClient")
    def test_post_with_invalid_max_period_rerenders_form(self, MockKeaClient):
        """POST with non-integer max_period must re-render the form (not redirect)."""
        response = self.client.post(self._url(), {"confirm": "1", "max_period": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        MockKeaClient.return_value.dhcp_disable.assert_not_called()

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_kea_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST with KeaException must flash a generic error; raw Kea text must not be leaked."""
        from netbox_kea.kea import KeaException

        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect
        mock_client.dhcp_disable.side_effect = KeaException(
            {"result": 1, "text": "kea_secret_internal_url=https://kea.internal:8080/api/"},
            index=0,
        )
        response = self.client.post(self._url(), {"confirm": "1"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("kea_secret_internal_url", response.content.decode())
        self.assertIn("Failed to disable DHCPv4:", response.content.decode())

    @patch("netbox_kea.models.KeaClient")
    def test_post_on_unexpected_exception_shows_error_and_redirects(self, MockKeaClient):
        """POST with transport exception must not leak the raw exception text."""
        mock_client = MockKeaClient.return_value
        mock_client.command.side_effect = _kea_command_side_effect
        mock_client.dhcp_disable.side_effect = requests.RequestException(
            "kea_secret_transport_marker=https://kea.internal/"
        )
        response = self.client.post(self._url(), {"confirm": "1"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("kea_secret_transport_marker", response.content.decode())
        self.assertIn("An internal error occurred.", response.content.decode())

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
class TestServerDHCP6DisableView(_ViewTestBase):
    """Tests for ServerDHCP6DisableView — verifies v6 variant uses dhcp6 service."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp6_disable", args=[self.server.pk])

    @patch("netbox_kea.models.KeaClient")
    def test_post_calls_dhcp_disable_v6(self, MockKeaClient):
        """POST must call dhcp_disable('dhcp6')."""
        mock_client = MockKeaClient.return_value
        mock_client.dhcp_disable.return_value = None
        response = self.client.post(self._url(), {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        mock_client.dhcp_disable.assert_called_once_with("dhcp6", max_period=None)
