# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the conftest URL-resolver bootstrap hook."""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from netbox_kea.tests.conftest import _prepopulate_url_resolver


class TestPrepopulateUrlResolver(SimpleTestCase):
    """The bootstrap hook must log (not silently swallow) setup failures."""

    def test_failure_is_logged_not_swallowed(self):
        """A resolver-populate error is logged for diagnosability and never propagates."""
        with patch("django.urls.get_resolver", side_effect=RuntimeError("boom")):
            with self.assertLogs("netbox_kea.tests.conftest", level="ERROR") as cm:
                _prepopulate_url_resolver()  # must not raise
        self.assertTrue(
            any("pre-populate" in line.lower() for line in cm.output),
            cm.output,
        )
