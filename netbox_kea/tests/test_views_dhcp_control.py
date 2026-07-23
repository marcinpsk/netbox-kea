# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""DHCP enable/disable control-view tests for the netbox_kea plugin.

Covers ``ServerDHCP{4,6}EnableView`` and ``ServerDHCP{4,6}DisableView``.

These tests drive the **real** ``KeaClient``; only the HTTP boundary is stubbed
via ``kea_stub.stub_kea``, so the request payloads the views actually send to Kea
are asserted:

* enable  → ``dhcp-enable``  (``service=["dhcp{v}"]``)
* disable → ``dhcp-disable`` (``service=["dhcp{v}"]``; ``{"max-period": N}`` only
  when a period is supplied — omitted otherwise)

Error paths run through the real client: a ``{"result": 1}`` response becomes a
real ``KeaException`` and a boundary ``requests.RequestException`` a transport
error; the view flashes a generic message and must not leak the raw Kea text. The
error tests follow the redirect onto the status tab, which itself issues
``status-get``/``version-get``/``config-get`` — registered benign so the landing
page renders.
"""

import requests
from django.test import override_settings
from django.urls import reverse

from .kea_stub import stub_kea
from .utils import _PLUGINS_CONFIG, _ViewTestBase


def _config_get_empty(body):
    """A benign ``config-get`` payload for the status-tab landing (global options)."""
    svc = (body.get("service") or [""])[0]
    version = 6 if svc == "dhcp6" else 4
    key = f"Dhcp{version}"
    return {"result": 0, "arguments": {key: {"option-data": [], f"subnet{version}": [], "shared-networks": []}}}


def _control_stub(**overrides):
    """Register dhcp-enable/-disable plus the status-tab landing commands (all benign)."""
    base = {
        "dhcp-enable": {"result": 0},
        "dhcp-disable": {"result": 0},
        "status-get": {"result": 0, "arguments": {"pid": 1, "uptime": 1, "reload": 0}},
        "version-get": {"result": 0, "arguments": {"extended": "2.5"}},
        "config-get": _config_get_empty,
    }
    base.update(overrides)
    return stub_kea(base)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDHCP4EnableView(_ViewTestBase):
    """Tests for ServerDHCP4EnableView (GET confirmation + POST enable)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_enable", args=[self.server.pk])

    def test_get_returns_confirmation_page(self):
        """GET must render the enable confirmation page with dhcp_version=4 (no Kea)."""
        with stub_kea({}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "4")
        self.assertEqual(kea.commands(), [])

    def test_post_calls_dhcp_enable_and_redirects(self):
        """POST must issue dhcp-enable to the dhcp4 service and redirect to the status tab."""
        with _control_stub() as kea:
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertEqual(kea.bodies("dhcp-enable")[0]["service"], ["dhcp4"])

    def test_post_on_kea_exception_shows_error_and_redirects(self):
        """POST with KeaException must flash a generic error; raw Kea text must not be leaked."""
        secret = "kea_secret_internal_url=https://kea.internal:8080/api/"
        with _control_stub(**{"dhcp-enable": {"result": 1, "text": secret}}):
            response = self.client.post(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("kea_secret_internal_url", response.content.decode())
        self.assertIn("Failed to enable DHCPv4:", response.content.decode())

    def test_post_on_unexpected_exception_shows_error_and_redirects(self):
        """POST with transport exception must not leak the raw exception text."""
        marker = "kea_secret_transport_marker=https://kea.internal/"
        with _control_stub(**{"dhcp-enable": requests.RequestException(marker)}):
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
    """Tests for ServerDHCP6EnableView — verifies v6 variant uses the dhcp6 service."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp6_enable", args=[self.server.pk])

    def test_post_calls_dhcp_enable_v6(self):
        """POST must issue dhcp-enable to the dhcp6 service."""
        with _control_stub() as kea:
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.bodies("dhcp-enable")[0]["service"], ["dhcp6"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerDHCP4DisableView(_ViewTestBase):
    """Tests for ServerDHCP4DisableView (GET form + POST disable with optional max_period)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp4_disable", args=[self.server.pk])

    def test_get_returns_form_page(self):
        """GET must render the disable form with max_period field (no Kea)."""
        with stub_kea({}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "max_period")
        self.assertEqual(kea.commands(), [])

    def test_post_without_max_period_calls_disable_no_period(self):
        """POST without max_period must issue dhcp-disable with no max-period argument."""
        with _control_stub() as kea:
            response = self.client.post(self._url(), {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        body = kea.bodies("dhcp-disable")[0]
        self.assertEqual(body["service"], ["dhcp4"])
        self.assertNotIn("arguments", body)

    def test_post_with_max_period_passes_value(self):
        """POST with max_period=300 must send {"max-period": 300} to dhcp-disable."""
        with _control_stub() as kea:
            response = self.client.post(self._url(), {"confirm": "1", "max_period": "300"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        body = kea.bodies("dhcp-disable")[0]
        self.assertEqual(body["service"], ["dhcp4"])
        self.assertEqual(body["arguments"], {"max-period": 300})

    def test_post_with_invalid_max_period_rerenders_form(self):
        """POST with non-integer max_period must re-render the form (no Kea call)."""
        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"confirm": "1", "max_period": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("dhcp-disable", kea.commands())

    def test_post_on_kea_exception_shows_error_and_redirects(self):
        """POST with KeaException must flash a generic error; raw Kea text must not be leaked."""
        secret = "kea_secret_internal_url=https://kea.internal:8080/api/"
        with _control_stub(**{"dhcp-disable": {"result": 1, "text": secret}}):
            response = self.client.post(self._url(), {"confirm": "1"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("kea_secret_internal_url", response.content.decode())
        self.assertIn("Failed to disable DHCPv4:", response.content.decode())

    def test_post_on_unexpected_exception_shows_error_and_redirects(self):
        """POST with transport exception must not leak the raw exception text."""
        marker = "kea_secret_transport_marker=https://kea.internal/"
        with _control_stub(**{"dhcp-disable": requests.RequestException(marker)}):
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
    """Tests for ServerDHCP6DisableView — verifies v6 variant uses the dhcp6 service."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_dhcp6_disable", args=[self.server.pk])

    def test_post_calls_dhcp_disable_v6(self):
        """POST must issue dhcp-disable to the dhcp6 service with no max-period argument."""
        with _control_stub() as kea:
            response = self.client.post(self._url(), {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        body = kea.bodies("dhcp-disable")[0]
        self.assertEqual(body["service"], ["dhcp6"])
        self.assertNotIn("arguments", body)
