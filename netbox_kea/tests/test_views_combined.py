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

from django.test import override_settings
from django.urls import reverse

from .utils import _PLUGINS_CONFIG, _ViewTestBase


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchSharedNetworksFromServer(_ViewTestBase):
    """Line 3939: _fetch_shared_networks_from_server with null config raises RuntimeError."""

    def test_null_config_raises_runtime_error(self):
        from netbox_kea.views import _fetch_shared_networks_from_server

        with patch("netbox_kea.models.KeaClient") as MockKea:
            MockKea.return_value.command.return_value = [{"result": 0, "arguments": None}]
            with self.assertRaises(RuntimeError):
                _fetch_shared_networks_from_server(self.server, version=4)


# ---------------------------------------------------------------------------
# Combined reservations — multi-page pagination
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedReservationsMultiPage(_ViewTestBase):
    """Lines 4065-4066: combined reservations multi-page pagination."""

    def _url(self):
        return reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.server.pk}"

    @patch("netbox_kea.models.KeaClient")
    def test_multi_page_pagination_followed(self, MockKeaClient):
        """Lines 4065-4066: from_index/source_index advance across pages."""
        page1 = [{"subnet-id": 1, "ip-address": "10.0.0.1", "hw-address": "aa:bb:cc:dd:ee:01"}]
        page2 = [{"subnet-id": 1, "ip-address": "10.0.0.2", "hw-address": "aa:bb:cc:dd:ee:02"}]
        MockKeaClient.return_value.reservation_get_page.side_effect = [
            (page1, 1, 1),
            (page2, 0, 0),
        ]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MockKeaClient.return_value.reservation_get_page.call_count, 2)
        self.assertIn(b"10.0.0.1", response.content)
        self.assertIn(b"10.0.0.2", response.content)
