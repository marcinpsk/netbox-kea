# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.tables — column classes and table definitions."""

from unittest import TestCase


class TestExpiryDurationColumn(TestCase):
    """Tests for ExpiryDurationColumn — DurationColumn that applies expiry CSS class."""

    def _make_col(self):
        from netbox_kea.tables import ExpiryDurationColumn

        return ExpiryDurationColumn()

    def test_render_expired_wraps_in_danger_span(self):
        """When expiry_class is 'text-danger', rendered output contains that class."""
        col = self._make_col()
        record = {"expiry_class": "text-danger"}
        result = str(col.render(value=0, record=record))
        self.assertIn("text-danger", result)

    def test_render_warning_wraps_in_warning_span(self):
        """When expiry_class is 'text-warning', rendered output contains that class."""
        col = self._make_col()
        record = {"expiry_class": "text-warning"}
        result = str(col.render(value=200, record=record))
        self.assertIn("text-warning", result)

    def test_render_normal_no_extra_class(self):
        """When expiry_class is empty, rendered output has no danger/warning class."""
        col = self._make_col()
        record = {"expiry_class": ""}
        result = str(col.render(value=3600, record=record))
        self.assertNotIn("text-danger", result)
        self.assertNotIn("text-warning", result)

    def test_render_still_shows_duration(self):
        """Rendered value still includes the human-readable duration text."""
        col = self._make_col()
        record = {"expiry_class": ""}
        result = str(col.render(value=3600, record=record))
        self.assertIn("01:00:00", result)

    def test_render_missing_expiry_class_does_not_crash(self):
        """Missing expiry_class key falls back gracefully (no class, no exception)."""
        col = self._make_col()
        record = {}
        result = str(col.render(value=120, record=record))
        self.assertNotIn("text-danger", result)
        self.assertNotIn("text-warning", result)
