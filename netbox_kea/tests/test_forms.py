# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.forms — validation logic for all form classes."""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from netbox_kea.forms import Leases4SearchForm, Leases6SearchForm, MultipleIPField, ServerForm


class TestLeases4SearchFormValidation(SimpleTestCase):
    """Tests for Leases4SearchForm (DHCPv4 lease search validation)."""

    def _form(self, by, q, page=""):
        return Leases4SearchForm(data={"by": by, "q": q, "page": page})

    def test_valid_ip_search(self):
        form = self._form("ip", "192.168.1.1")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["q"], "192.168.1.1")

    def test_valid_hostname_search(self):
        form = self._form("hostname", "myhost.example.com")
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_hw_address_colon(self):
        form = self._form("hw", "aa:bb:cc:dd:ee:ff")
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_hw_address_dash(self):
        form = self._form("hw", "aa-bb-cc-dd-ee-ff")
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_hw_address(self):
        form = self._form("hw", "not-a-mac")
        self.assertFalse(form.is_valid())
        self.assertIn("q", form.errors)

    def test_valid_subnet_with_cidr(self):
        form = self._form("subnet", "192.168.1.0/24")
        self.assertTrue(form.is_valid(), form.errors)

    def test_subnet_without_cidr_fails(self):
        form = self._form("subnet", "192.168.1.0")
        self.assertFalse(form.is_valid())
        self.assertIn("q", form.errors)

    def test_invalid_subnet(self):
        form = self._form("subnet", "notanip/24")
        self.assertFalse(form.is_valid())
        self.assertIn("q", form.errors)

    def test_subnet_not_network_address_fails(self):
        # 192.168.1.5/24 is not a network address (network is 192.168.1.0/24)
        form = self._form("subnet", "192.168.1.5/24")
        self.assertFalse(form.is_valid())
        self.assertIn("q", form.errors)

    def test_valid_subnet_id(self):
        form = self._form("subnet_id", "42")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["q"], 42)

    def test_subnet_id_zero_fails(self):
        form = self._form("subnet_id", "0")
        self.assertFalse(form.is_valid())

    def test_subnet_id_negative_fails(self):
        form = self._form("subnet_id", "-1")
        self.assertFalse(form.is_valid())

    def test_subnet_id_non_integer_fails(self):
        form = self._form("subnet_id", "abc")
        self.assertFalse(form.is_valid())

    def test_valid_client_id(self):
        form = self._form("client_id", "aabb")
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_client_id(self):
        form = self._form("client_id", "gg")
        self.assertFalse(form.is_valid())

    def test_q_without_by_fails(self):
        form = Leases4SearchForm(data={"q": "something"})
        self.assertFalse(form.is_valid())

    def test_by_without_q_fails(self):
        form = Leases4SearchForm(data={"by": "ip", "q": ""})
        self.assertFalse(form.is_valid())

    def test_page_requires_subnet_by(self):
        form = Leases4SearchForm(data={"by": "ip", "q": "192.168.1.1", "page": "192.168.1.2"})
        self.assertFalse(form.is_valid())
        self.assertIn("page", form.errors)

    def test_valid_page_with_subnet(self):
        form = Leases4SearchForm(data={"by": "subnet", "q": "192.168.1.0/24", "page": "192.168.1.5"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["page"], "192.168.1.5")

    def test_page_not_in_subnet_fails(self):
        form = Leases4SearchForm(data={"by": "subnet", "q": "192.168.1.0/24", "page": "10.0.0.1"})
        self.assertFalse(form.is_valid())
        self.assertIn("page", form.errors)

    def test_ipv6_address_fails_for_v4_form(self):
        form = self._form("ip", "2001:db8::1")
        self.assertFalse(form.is_valid())
        self.assertIn("q", form.errors)


class TestLeases6SearchFormValidation(SimpleTestCase):
    """Tests for Leases6SearchForm (DHCPv6 lease search validation)."""

    def _form(self, by, q, page=""):
        return Leases6SearchForm(data={"by": by, "q": q, "page": page})

    def test_valid_ipv6_address(self):
        form = self._form("ip", "2001:db8::1")
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_ipv6_address(self):
        form = self._form("ip", "notanip")
        self.assertFalse(form.is_valid())

    def test_ipv4_address_fails_for_v6_form(self):
        form = self._form("ip", "192.168.1.1")
        self.assertFalse(form.is_valid())

    def test_valid_duid(self):
        form = self._form("duid", "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff")
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_duid(self):
        form = self._form("duid", "gg:hh")
        self.assertFalse(form.is_valid())

    def test_valid_subnet_v6(self):
        form = self._form("subnet", "2001:db8::/32")
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_subnet_id(self):
        form = self._form("subnet_id", "10")
        self.assertTrue(form.is_valid(), form.errors)


class TestMultipleIPField(SimpleTestCase):
    """Tests for MultipleIPField validation."""

    def test_valid_ipv4_list(self):
        field = MultipleIPField(version=4)
        result = field.clean(["192.168.1.1", "10.0.0.2"])
        self.assertEqual(result, ["192.168.1.1", "10.0.0.2"])

    def test_valid_ipv6_list(self):
        field = MultipleIPField(version=6)
        result = field.clean(["2001:db8::1", "::1"])
        self.assertIn("2001:db8::1", result)

    def test_empty_list_fails(self):
        field = MultipleIPField(version=4)
        with self.assertRaises(ValidationError):
            field.clean([])

    def test_non_list_fails(self):
        field = MultipleIPField(version=4)
        with self.assertRaises(ValidationError):
            field.clean("192.168.1.1")

    def test_invalid_ip_fails(self):
        field = MultipleIPField(version=4)
        with self.assertRaises(ValidationError):
            field.clean(["notanip"])


class TestServerFormFields(TestCase):
    """Tests that ServerForm exposes the expected fields (requires DB for NetBox ObjectType lookup)."""

    def test_server_form_has_dual_url_fields(self):
        form = ServerForm()
        self.assertIn("dhcp4_url", form.fields)
        self.assertIn("dhcp6_url", form.fields)

    def test_server_form_has_has_control_agent(self):
        form = ServerForm()
        self.assertIn("has_control_agent", form.fields)

    def test_server_form_has_core_fields(self):
        form = ServerForm()
        for field in ("name", "server_url", "username", "password", "ssl_verify", "dhcp4", "dhcp6"):
            self.assertIn(field, form.fields, f"Missing field: {field}")

    def test_server_form_password_is_password_input(self):
        from django import forms

        form = ServerForm()
        self.assertIsInstance(form.fields["password"].widget, forms.PasswordInput)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Reservation Management — form tests
# These tests will FAIL until Reservation4Form and Reservation6Form are added
# to netbox_kea/forms.py.
# ─────────────────────────────────────────────────────────────────────────────


class TestReservationForm4(SimpleTestCase):
    """Tests for Reservation4Form (IPv4 reservation form validation)."""

    def _form(self, data):
        from netbox_kea.forms import Reservation4Form  # deferred: class not yet defined

        return Reservation4Form(data=data)

    def _valid_data(self, **overrides):
        base = {
            "subnet_id": 1,
            "ip_address": "192.168.1.100",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "testhost.example.com",
        }
        base.update(overrides)
        return base

    def test_valid_form_with_hw_address_identifier(self):
        form = self._form(self._valid_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_form_with_client_id_identifier(self):
        form = self._form(self._valid_data(identifier_type="client-id", identifier="01aabbccddeeff"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_form_hostname_optional(self):
        data = self._valid_data()
        del data["hostname"]
        form = self._form(data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_ipv4_address(self):
        form = self._form(self._valid_data(ip_address="999.999.999.999"))
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_ipv6_address_rejected_in_v4_form(self):
        form = self._form(self._valid_data(ip_address="2001:db8::1"))
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_missing_subnet_id_fails(self):
        data = self._valid_data()
        del data["subnet_id"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_id", form.errors)

    def test_missing_ip_address_fails(self):
        data = self._valid_data()
        del data["ip_address"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_missing_identifier_type_fails(self):
        data = self._valid_data()
        del data["identifier_type"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("identifier_type", form.errors)

    def test_missing_identifier_fails(self):
        data = self._valid_data()
        del data["identifier"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("identifier", form.errors)

    def test_identifier_type_choices_include_hw_address(self):
        from netbox_kea.forms import Reservation4Form

        choices = [c[0] for c in Reservation4Form().fields["identifier_type"].choices]
        self.assertIn("hw-address", choices)

    def test_identifier_type_choices_include_client_id(self):
        from netbox_kea.forms import Reservation4Form

        choices = [c[0] for c in Reservation4Form().fields["identifier_type"].choices]
        self.assertIn("client-id", choices)

    def test_identifier_type_choices_include_circuit_id(self):
        from netbox_kea.forms import Reservation4Form

        choices = [c[0] for c in Reservation4Form().fields["identifier_type"].choices]
        self.assertIn("circuit-id", choices)

    def test_identifier_type_choices_include_flex_id(self):
        from netbox_kea.forms import Reservation4Form

        choices = [c[0] for c in Reservation4Form().fields["identifier_type"].choices]
        self.assertIn("flex-id", choices)

    def test_subnet_id_zero_fails(self):
        form = self._form(self._valid_data(subnet_id=0))
        self.assertFalse(form.is_valid())

    def test_subnet_id_negative_fails(self):
        form = self._form(self._valid_data(subnet_id=-1))
        self.assertFalse(form.is_valid())

    def test_invalid_identifier_type_choice_fails(self):
        form = self._form(self._valid_data(identifier_type="not-a-real-type"))
        self.assertFalse(form.is_valid())
        self.assertIn("identifier_type", form.errors)


class TestReservationForm6(SimpleTestCase):
    """Tests for Reservation6Form (IPv6 reservation form validation)."""

    def _form(self, data):
        from netbox_kea.forms import Reservation6Form  # deferred: class not yet defined

        return Reservation6Form(data=data)

    def _valid_data(self, **overrides):
        base = {
            "subnet_id": 1,
            "ip_addresses": "2001:db8::100",
            "identifier_type": "duid",
            "identifier": "00:01:02:03:04:05:06:07",
            "hostname": "testhost6.example.com",
        }
        base.update(overrides)
        return base

    def test_valid_form_with_duid_identifier(self):
        form = self._form(self._valid_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_form_with_hw_address_identifier(self):
        form = self._form(self._valid_data(identifier_type="hw-address", identifier="aa:bb:cc:dd:ee:ff"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_form_multiple_ip_addresses(self):
        form = self._form(self._valid_data(ip_addresses="2001:db8::100,2001:db8::101"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_form_hostname_optional(self):
        data = self._valid_data()
        del data["hostname"]
        form = self._form(data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_ipv4_address_rejected_in_v6_form(self):
        form = self._form(self._valid_data(ip_addresses="192.168.1.1"))
        self.assertFalse(form.is_valid())
        self.assertIn("ip_addresses", form.errors)

    def test_missing_subnet_id_fails(self):
        data = self._valid_data()
        del data["subnet_id"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_id", form.errors)

    def test_missing_ip_addresses_fails(self):
        data = self._valid_data()
        del data["ip_addresses"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("ip_addresses", form.errors)

    def test_missing_identifier_type_fails(self):
        data = self._valid_data()
        del data["identifier_type"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("identifier_type", form.errors)

    def test_missing_identifier_fails(self):
        data = self._valid_data()
        del data["identifier"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("identifier", form.errors)

    def test_identifier_type_choices_include_duid(self):
        from netbox_kea.forms import Reservation6Form

        choices = [c[0] for c in Reservation6Form().fields["identifier_type"].choices]
        self.assertIn("duid", choices)

    def test_identifier_type_choices_include_hw_address(self):
        from netbox_kea.forms import Reservation6Form

        choices = [c[0] for c in Reservation6Form().fields["identifier_type"].choices]
        self.assertIn("hw-address", choices)

    def test_identifier_type_choices_include_client_id(self):
        from netbox_kea.forms import Reservation6Form

        choices = [c[0] for c in Reservation6Form().fields["identifier_type"].choices]
        self.assertIn("client-id", choices)

    def test_identifier_type_choices_include_flex_id(self):
        from netbox_kea.forms import Reservation6Form

        choices = [c[0] for c in Reservation6Form().fields["identifier_type"].choices]
        self.assertIn("flex-id", choices)
