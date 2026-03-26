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


# ─────────────────────────────────────────────────────────────────────────────
# SubnetEditForm
# ─────────────────────────────────────────────────────────────────────────────


class TestSubnetEditForm(SimpleTestCase):
    """Unit tests for SubnetEditForm — validation of editable subnet fields."""

    def _form(self, **kwargs):
        from netbox_kea.forms import SubnetEditForm

        data = {"subnet_cidr": "10.0.0.0/24", **kwargs}
        return SubnetEditForm(data=data)

    def test_valid_minimal_form_no_optional_fields(self):
        """A form with only subnet_cidr (hidden) and no optional fields is valid."""
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_form_with_all_fields(self):
        """A fully populated form is valid."""
        form = self._form(
            pools="10.0.0.100-10.0.0.200",
            gateway="10.0.0.1",
            dns_servers="8.8.8.8, 1.1.1.1",
            ntp_servers="pool.ntp.org",
            valid_lft="3600",
            min_valid_lft="1800",
            max_valid_lft="7200",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_gateway_ip_raises_validation_error(self):
        """A non-IP gateway value must produce a form error."""
        form = self._form(gateway="not-an-ip")
        self.assertFalse(form.is_valid())
        self.assertIn("gateway", form.errors)

    def test_invalid_dns_server_ip_raises_validation_error(self):
        """A non-IP DNS server value must produce a form error."""
        form = self._form(dns_servers="8.8.8.8, invalid-ip")
        self.assertFalse(form.is_valid())
        self.assertIn("dns_servers", form.errors)

    def test_invalid_pool_format_raises_validation_error(self):
        """A pool entry without '-' or '/' must produce a form error."""
        form = self._form(pools="10.0.0.1")
        self.assertFalse(form.is_valid())
        self.assertIn("pools", form.errors)

    def test_pools_cleaned_as_list(self):
        """clean_pools returns a list of strings, one per non-empty line."""
        form = self._form(pools="10.0.0.100-10.0.0.150\n10.0.0.200-10.0.0.220\n")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["pools"], ["10.0.0.100-10.0.0.150", "10.0.0.200-10.0.0.220"])

    def test_dns_servers_cleaned_as_list(self):
        """clean_dns_servers returns a list of IP strings."""
        form = self._form(dns_servers="8.8.8.8, 1.1.1.1")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["dns_servers"], ["8.8.8.8", "1.1.1.1"])

    def test_ntp_servers_cleaned_as_list(self):
        """clean_ntp_servers returns a list of hostname/IP strings."""
        form = self._form(ntp_servers="pool.ntp.org, time.google.com")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["ntp_servers"], ["pool.ntp.org", "time.google.com"])


# ---------------------------------------------------------------------------
# TestLease4AddForm
# ---------------------------------------------------------------------------


class TestLease4AddForm(SimpleTestCase):
    """Tests for Lease4AddForm validation."""

    def _form(self, data):
        from netbox_kea.forms import Lease4AddForm

        return Lease4AddForm(data=data)

    def _valid_data(self, **overrides):
        base = {"ip_address": "10.0.0.100"}
        base.update(overrides)
        return base

    def test_valid_with_ip_only(self):
        """Form is valid with only ip_address provided (all other fields optional)."""
        form = self._form(self._valid_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_with_all_fields(self):
        """Form is valid when all optional fields are provided."""
        form = self._form(
            self._valid_data(
                hw_address="aa:bb:cc:dd:ee:ff",
                subnet_id=1,
                valid_lft=3600,
                hostname="host.example.com",
                sync_to_netbox=True,
            )
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_ip_address_rejected(self):
        """Non-IP value in ip_address causes form error."""
        form = self._form(self._valid_data(ip_address="not-an-ip"))
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_ipv6_address_rejected(self):
        """IPv6 address in a v4 form is rejected."""
        form = self._form(self._valid_data(ip_address="2001:db8::1"))
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_missing_ip_address_fails(self):
        """ip_address is required."""
        form = self._form({})
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_clean_ip_returns_string(self):
        """clean_ip_address returns a plain IP string (no prefix length)."""
        form = self._form(self._valid_data(ip_address="10.0.0.50"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["ip_address"], "10.0.0.50")

    def test_subnet_id_must_be_positive(self):
        """subnet_id must be >= 1 (min_value=1 on the field)."""
        form = self._form(self._valid_data(subnet_id=0))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_id", form.errors)

    def test_sync_to_netbox_defaults_to_unchecked(self):
        """sync_to_netbox is not required and defaults to False when absent."""
        form = self._form(self._valid_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data.get("sync_to_netbox"))


# ---------------------------------------------------------------------------
# TestLease6AddForm
# ---------------------------------------------------------------------------


class TestLease6AddForm(SimpleTestCase):
    """Tests for Lease6AddForm validation."""

    def _form(self, data):
        from netbox_kea.forms import Lease6AddForm

        return Lease6AddForm(data=data)

    def _valid_data(self, **overrides):
        base = {
            "ip_address": "2001:db8::1",
            "duid": "00:01:02:03:04:05",
            "iaid": 1,
        }
        base.update(overrides)
        return base

    def test_valid_with_required_fields(self):
        """Form is valid with ip_address, duid, and iaid."""
        form = self._form(self._valid_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_ipv4_address_rejected(self):
        """IPv4 address in a v6 form is rejected."""
        form = self._form(self._valid_data(ip_address="10.0.0.1"))
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_invalid_ip_address_rejected(self):
        """Non-IP string in ip_address causes form error."""
        form = self._form(self._valid_data(ip_address="not-an-ip"))
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_missing_duid_fails(self):
        """duid is required for a v6 lease."""
        data = self._valid_data()
        del data["duid"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("duid", form.errors)

    def test_missing_iaid_fails(self):
        """iaid is required for a v6 lease."""
        data = self._valid_data()
        del data["iaid"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("iaid", form.errors)

    def test_clean_ip_returns_string(self):
        """clean_ip_address returns a plain IPv6 string."""
        form = self._form(self._valid_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["ip_address"], "2001:db8::1")

    def test_iaid_cannot_be_negative(self):
        """iaid has min_value=0; negative value is rejected."""
        form = self._form(self._valid_data(iaid=-1))
        self.assertFalse(form.is_valid())
        self.assertIn("iaid", form.errors)


# ---------------------------------------------------------------------------
# TestSharedNetworkForm
# ---------------------------------------------------------------------------


class TestSharedNetworkForm(SimpleTestCase):
    """Tests for SharedNetworkForm validation."""

    def _form(self, data):
        from netbox_kea.forms import SharedNetworkForm

        return SharedNetworkForm(data=data)

    def test_valid_with_name(self):
        """Form is valid when a non-empty name is provided."""
        form = self._form({"name": "prod-network"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_missing_name_fails(self):
        """name is required."""
        form = self._form({})
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_empty_name_fails(self):
        """Empty string for name is rejected."""
        form = self._form({"name": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_name_max_length_128(self):
        """Names up to 128 chars are accepted; 129 chars are rejected."""
        form_ok = self._form({"name": "x" * 128})
        self.assertTrue(form_ok.is_valid(), form_ok.errors)
        form_too_long = self._form({"name": "x" * 129})
        self.assertFalse(form_too_long.is_valid())
        self.assertIn("name", form_too_long.errors)


# ---------------------------------------------------------------------------
# F11: SubnetEditForm renew/rebind timer fields
# ---------------------------------------------------------------------------


class TestSubnetEditFormTimers(SimpleTestCase):
    """F11: SubnetEditForm must expose renew_timer and rebind_timer fields."""

    def _form(self, **kwargs):
        from netbox_kea.forms import SubnetEditForm

        data = {"subnet_cidr": "10.0.0.0/24", **kwargs}
        return SubnetEditForm(data=data)

    def test_form_has_renew_timer_field(self):
        """SubnetEditForm must have a renew_timer field."""
        from netbox_kea.forms import SubnetEditForm

        self.assertIn("renew_timer", SubnetEditForm().fields)

    def test_form_has_rebind_timer_field(self):
        """SubnetEditForm must have a rebind_timer field."""
        from netbox_kea.forms import SubnetEditForm

        self.assertIn("rebind_timer", SubnetEditForm().fields)

    def test_valid_form_with_timer_fields(self):
        """A form with valid renew_timer and rebind_timer values is valid."""
        form = self._form(renew_timer="600", rebind_timer="900")
        self.assertTrue(form.is_valid(), form.errors)

    def test_renew_timer_cleaned_as_int(self):
        """renew_timer cleaned value must be an integer."""
        form = self._form(renew_timer="600", rebind_timer="900")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["renew_timer"], 600)

    def test_rebind_timer_cleaned_as_int(self):
        """rebind_timer cleaned value must be an integer."""
        form = self._form(renew_timer="600", rebind_timer="900")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["rebind_timer"], 900)

    def test_timer_fields_are_optional(self):
        """renew_timer and rebind_timer are optional; form is valid without them."""
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data.get("renew_timer"))
        self.assertIsNone(form.cleaned_data.get("rebind_timer"))


# ---------------------------------------------------------------------------
# SubnetAddForm — shared_network field
# ---------------------------------------------------------------------------


class TestSubnetAddFormSharedNetwork(SimpleTestCase):
    """SubnetAddForm must expose a shared_network ChoiceField."""

    def test_form_has_shared_network_field(self):
        """SubnetAddForm exposes a shared_network field."""
        from netbox_kea.forms import SubnetAddForm

        self.assertIn("shared_network", SubnetAddForm().fields)

    def test_shared_network_field_is_not_required(self):
        """shared_network is optional."""
        from netbox_kea.forms import SubnetAddForm

        field = SubnetAddForm().fields["shared_network"]
        self.assertFalse(field.required)

    def test_form_valid_without_shared_network(self):
        """Form is valid when shared_network is omitted (empty)."""
        from netbox_kea.forms import SubnetAddForm

        form = SubnetAddForm(data={"subnet": "10.0.0.0/24", "shared_network": ""})
        # Should not fail on shared_network validation itself.
        errors = form.errors
        self.assertNotIn("shared_network", errors)


# ---------------------------------------------------------------------------
# SharedNetworkEditForm
# ---------------------------------------------------------------------------


class TestSharedNetworkEditForm(SimpleTestCase):
    """Tests for SharedNetworkEditForm validation, particularly clean_relay_addresses()."""

    def _form(self, **kwargs):
        from netbox_kea.forms import SharedNetworkEditForm

        data = {"name": "prod-net", **kwargs}
        return SharedNetworkEditForm(data=data)

    def test_valid_with_name_only(self):
        """Form is valid when only name is provided (all optional fields empty)."""
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_with_single_relay_address(self):
        """A single valid IPv4 relay address is accepted."""
        form = self._form(relay_addresses="10.0.0.1")
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_with_multiple_relay_addresses(self):
        """Multiple comma-separated valid IPs are accepted."""
        form = self._form(relay_addresses="10.0.0.1, 10.0.0.2")
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_with_ipv6_relay_address(self):
        """An IPv6 relay address is accepted."""
        form = self._form(relay_addresses="2001:db8::1")
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_relay_address_fails_validation(self):
        """A non-IP value in relay_addresses raises a ValidationError."""
        form = self._form(relay_addresses="not-an-ip")
        self.assertFalse(form.is_valid())
        self.assertIn("relay_addresses", form.errors)

    def test_invalid_second_relay_address_fails_validation(self):
        """If the second address in a comma-separated list is bad, validation fails."""
        form = self._form(relay_addresses="10.0.0.1, not-an-ip")
        self.assertFalse(form.is_valid())
        self.assertIn("relay_addresses", form.errors)

    def test_empty_relay_addresses_accepted(self):
        """An empty relay_addresses string is accepted (clears relay)."""
        form = self._form(relay_addresses="")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["relay_addresses"], "")

    def test_form_has_description_field(self):
        """Form exposes a description field."""
        from netbox_kea.forms import SharedNetworkEditForm

        self.assertIn("description", SharedNetworkEditForm().fields)

    def test_form_has_interface_field(self):
        """Form exposes an interface field."""
        from netbox_kea.forms import SharedNetworkEditForm

        self.assertIn("interface", SharedNetworkEditForm().fields)

    def test_name_field_is_hidden_input(self):
        """The name field uses HiddenInput widget."""
        from django.forms import HiddenInput

        from netbox_kea.forms import SharedNetworkEditForm

        self.assertIsInstance(SharedNetworkEditForm().fields["name"].widget, HiddenInput)

    def test_description_accepts_255_chars(self):
        """description accepts strings up to 255 characters."""
        form = self._form(description="x" * 255)
        self.assertTrue(form.is_valid(), form.errors)

    def test_description_rejects_256_chars(self):
        """description rejects strings longer than 255 characters."""
        form = self._form(description="x" * 256)
        self.assertFalse(form.is_valid())
        self.assertIn("description", form.errors)
