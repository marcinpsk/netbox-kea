# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Shared test utilities for netbox_kea view tests.

Import from this module instead of duplicating scaffold code in each test file.
"""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from netbox_kea.models import Server

# Minimal PLUGINS_CONFIG so server.get_client() can read kea_timeout.
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_INT_PK_RE = re.compile(r"/servers/(\d+)/")


def _make_db_server(**kwargs) -> Server:
    """Create and persist a Server without live connectivity checks.

    ``Server.objects.create()`` skips ``Model.clean()``, so no Kea connectivity
    check is triggered.  The ``PLUGINS_CONFIG`` override is applied by the calling
    test class.
    """
    defaults = {
        "name": "test-kea",
        "server_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


def _kea_command_side_effect(cmd, service=None, arguments=None, check=None):
    """Return a plausible Kea API response for each command type."""
    if cmd == "status-get":
        return [{"result": 0, "arguments": {"pid": 1234, "uptime": 3600, "reload": 0}}]
    if cmd == "version-get":
        return [{"result": 0, "arguments": {"extended": "2.4.1-stable"}}]
    if cmd == "config-get":
        # Return minimal Dhcp4/Dhcp6 config so subnet views can parse it.
        if service and service[0] == "dhcp6":
            return [{"result": 0, "arguments": {"Dhcp6": {"subnet6": [], "shared-networks": []}}}]
        return [{"result": 0, "arguments": {"Dhcp4": {"subnet4": [], "shared-networks": []}}}]
    return [{"result": 0, "arguments": {}}]


# ─────────────────────────────────────────────────────────────────────────────
# Shared base class
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _ViewTestBase(TestCase):
    """Creates a superuser and a single Server for use in all view tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="kea_testuser",
            email="kea_test@example.com",
            password="kea_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def _assert_no_none_pk_redirect(self, response):
        """Assert that a redirect URL never contains the string ``None`` as a pk."""
        if hasattr(response, "url"):
            self.assertNotIn(
                "servers/None",
                response.url,
                f"Redirect went to bad URL: {response.url}",
            )

    def _assert_redirect_to_integer_pk(self, response):
        """Assert that a redirect URL contains an integer server pk."""
        self._assert_no_none_pk_redirect(response)
        self.assertIsNotNone(
            _INT_PK_RE.search(response.url),
            f"Expected /servers/<int>/ in redirect URL, got: {response.url}",
        )

    def _make_request(self):
        """Return a RequestFactory request pre-loaded with a FallbackStorage message backend."""
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        setattr(request, "session", "session")
        storage = FallbackStorage(request)
        setattr(request, "_messages", storage)
        return request

    @staticmethod
    def _call_version(call_args):
        """Extract the 'version' argument from a mock call_args (kwargs or positional)."""
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        return kwargs.get("version") or (args[0] if args else None)
