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


class TestGlobalLeaseTableInheritance(TestCase):
    """GlobalLeaseTable4/6 must stay aligned with their per-server counterparts."""

    def test_global_lease_table4_extends_lease_table4(self):
        """GlobalLeaseTable4 must extend LeaseTable4 to inherit the client_id column."""
        from netbox_kea.tables import GlobalLeaseTable4, LeaseTable4

        self.assertTrue(issubclass(GlobalLeaseTable4, LeaseTable4))

    def test_global_lease_table4_has_client_id_field(self):
        """GlobalLeaseTable4 must expose client_id (inherited from LeaseTable4)."""
        from netbox_kea.tables import GlobalLeaseTable4

        self.assertIn("client_id", GlobalLeaseTable4.Meta.fields)

    def test_global_lease_table4_default_columns_include_state_label(self):
        """GlobalLeaseTable4 default_columns must include state_label like the per-server table."""
        from netbox_kea.tables import GlobalLeaseTable4

        self.assertIn("state_label", GlobalLeaseTable4.Meta.default_columns)

    def test_global_lease_table4_default_columns_include_server(self):
        """GlobalLeaseTable4 must prepend the server column to identify source server."""
        from netbox_kea.tables import GlobalLeaseTable4

        self.assertEqual(GlobalLeaseTable4.Meta.default_columns[0], "server")

    def test_global_lease_table6_default_columns_include_state_label(self):
        """GlobalLeaseTable6 default_columns must include state_label like the per-server table."""
        from netbox_kea.tables import GlobalLeaseTable6

        self.assertIn("state_label", GlobalLeaseTable6.Meta.default_columns)


class TestGlobalSubnetTableInheritance(TestCase):
    """GlobalSubnetTable4/6 must extend SubnetTable to stay in sync."""

    def test_global_subnet_table4_extends_subnet_table(self):
        """GlobalSubnetTable4 must extend SubnetTable to avoid column drift."""
        from netbox_kea.tables import GlobalSubnetTable4, SubnetTable

        self.assertTrue(issubclass(GlobalSubnetTable4, SubnetTable))

    def test_global_subnet_table4_default_columns_prepend_server(self):
        """GlobalSubnetTable4 must prepend server to default_columns."""
        from netbox_kea.tables import GlobalSubnetTable4

        self.assertEqual(GlobalSubnetTable4.Meta.default_columns[0], "server")

    def test_global_subnet_table4_fields_superset_of_subnet_table(self):
        """GlobalSubnetTable4 fields must contain all SubnetTable fields."""
        from netbox_kea.tables import GlobalSubnetTable4, SubnetTable

        for field in SubnetTable.Meta.fields:
            self.assertIn(field, GlobalSubnetTable4.Meta.fields)

    def test_global_subnet_table6_extends_global_subnet_table4(self):
        """GlobalSubnetTable6 must still extend GlobalSubnetTable4."""
        from netbox_kea.tables import GlobalSubnetTable4, GlobalSubnetTable6

        self.assertTrue(issubclass(GlobalSubnetTable6, GlobalSubnetTable4))
