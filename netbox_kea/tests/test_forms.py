# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2026 Andrew Backeby <andrew@backeby.eu>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.forms — validation logic for all form classes."""

import re

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from netbox_kea import constants
from netbox_kea.forms import (
    Leases4SearchForm,
    Leases6SearchForm,
    MultipleIPField,
    PoolAddForm,
    ServerForm,
    ServerImportForm,
    SubnetConfirmForm,
)
from netbox_kea.models import Server
from netbox_kea.reservations import ReservationCapabilities, reservation_identifier_types


def _reservation_capabilities(family, identifiers=None):
    """Live capabilities for *family*; pass *identifiers* to restrict what Kea enables."""
    enabled = reservation_identifier_types(family) if identifiers is None else tuple(identifiers)
    return ReservationCapabilities(
        family=family,
        identifiers=enabled,
        mutation_available=True,
        explanation="",
        unavailable_identifiers=tuple(
            (identifier, "Not enabled in the live Kea configuration.")
            for identifier in reservation_identifier_types(family)
            if identifier not in enabled
        ),
    )


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

    def test_valid_numeric_page_with_hostname_search(self):
        form = Leases4SearchForm(data={"by": "hostname", "q": "search-host", "page": "2"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["page"], 2)

    def test_hostname_page_must_be_a_positive_integer(self):
        for page in ("198.18.0.2", "0", "-1", "1.5"):
            with self.subTest(page=page):
                form = self._form("hostname", "search-host", page=page)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["page"], ["Page must be a positive integer."])

    def test_valid_numeric_page_with_subnet_search(self):
        form = self._form("subnet", "192.168.1.0/24", page="2")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["page"], 2)

    def test_subnet_page_must_be_a_positive_integer(self):
        form = self._form("subnet_id", "42", page="192.168.1.2")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["page"], ["Page must be a positive integer."])

    def test_subnet_page_rejects_zero(self):
        form = self._form("subnet_id", "42", page="0")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["page"], ["Page must be a positive integer."])

    def test_valid_page_with_all_leases_search(self):
        form = Leases4SearchForm(data={"by": "", "q": "", "page": "192.168.1.5"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["page"], "192.168.1.5")

    def test_invalid_page_address_fails(self):
        form = Leases4SearchForm(data={"by": "", "q": "", "page": "not-an-address"})
        self.assertFalse(form.is_valid())
        self.assertIn("page", form.errors)

    def test_page_address_with_prefix_fails(self):
        form = Leases4SearchForm(data={"by": "", "q": "", "page": "192.0.2.1/24"})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["page"], ["Invalid IP."])

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

    def test_valid_numeric_page_with_subnet_id_search(self):
        form = self._form("subnet_id", "10", page="3")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["page"], 3)


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
        for field in ("name", "ca_url", "ca_username", "ca_password", "ssl_verify", "dhcp4", "dhcp6"):
            self.assertIn(field, form.fields, f"Missing field: {field}")

    def test_server_form_password_is_password_input(self):
        from django import forms

        form = ServerForm()
        self.assertIsInstance(form.fields["ca_password"].widget, forms.PasswordInput)

    def test_server_form_dhcp4_password_is_password_input(self):
        from django import forms

        form = ServerForm()
        self.assertIsInstance(form.fields["dhcp4_password"].widget, forms.PasswordInput)

    def test_server_form_dhcp6_password_is_password_input(self):
        from django import forms

        form = ServerForm()
        self.assertIsInstance(form.fields["dhcp6_password"].widget, forms.PasswordInput)

    def test_server_form_offers_every_editable_server_field(self):
        """A model field no form lists can only be set through the ORM.

        sync_dhcp_plugin_enabled shipped that way: the model, the migration and the
        DHCP-plugin tab gate all read it, but nothing rendered it, so no operator could
        turn the tab on. custom_field_data is NetBox's own JSON store, which
        NetBoxModelForm renders from the CustomField rows rather than as a model field.
        """
        editable = {
            field.name
            for field in Server._meta.get_fields()
            if getattr(field, "editable", False) and not field.auto_created
        }

        self.assertEqual(editable - set(ServerForm.Meta.fields), {"custom_field_data"})

    def test_server_form_sync_vrf_offers_every_vrf(self):
        """The field is generated from the FK, so its queryset must need no fixing up."""
        from ipam.models import VRF

        vrf = VRF.objects.create(name="edit-form-vrf")

        self.assertIn(vrf, ServerForm().fields["sync_vrf"].queryset)

    def test_server_form_has_the_dhcp_plugin_sync_toggle(self):
        form = ServerForm()
        self.assertIn("sync_dhcp_plugin_enabled", form.fields)

    def test_server_form_has_per_protocol_credential_fields(self):
        form = ServerForm()
        for field in ("dhcp4_username", "dhcp4_password", "dhcp6_username", "dhcp6_password"):
            self.assertIn(field, form.fields, f"Missing field: {field}")


class TestServerImportFormFields(TestCase):
    """ServerImportForm must reach every field an operator can set in the edit form."""

    def test_import_form_offers_every_editable_server_field(self):
        """A field the import form omits cannot be set by CSV, only row by row in the UI.

        The sync fields shipped that way: the import form stopped at
        has_control_agent, so a bulk-imported server always landed on the model
        defaults. custom_field_data is NetBox's own JSON store, which
        NetBoxModelImportForm renders from the CustomField rows.
        """
        editable = {
            field.name
            for field in Server._meta.get_fields()
            if getattr(field, "editable", False) and not field.auto_created
        }

        self.assertEqual(editable - set(ServerImportForm().fields), {"custom_field_data"})

    def test_import_form_matches_the_sync_vrf_by_name(self):
        """CSV carries names, not primary keys."""
        from ipam.models import VRF

        vrf = VRF.objects.create(name="import-form-vrf")
        field = ServerImportForm().fields["sync_vrf"]

        self.assertEqual(field.to_python("import-form-vrf"), vrf)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Reservation Management — form tests
# These tests will FAIL until Reservation4Form and Reservation6Form are added
# to netbox_kea/forms.py.
# ─────────────────────────────────────────────────────────────────────────────


class TestReservationForm4(SimpleTestCase):
    """Tests for Reservation4Form (IPv4 reservation form validation)."""

    def _form(self, data):
        from netbox_kea.forms import Reservation4Form  # deferred: class not yet defined

        return Reservation4Form(data=data, capabilities=_reservation_capabilities(4))

    def _valid_data(self, **overrides):
        base = {
            "subnet_cidr": "192.168.1.0/24",
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

    def test_mutation_fails_closed_without_live_capabilities(self):
        from netbox_kea.forms import Reservation4Form

        form = Reservation4Form(data=self._valid_data())
        self.assertFalse(form.is_valid())
        self.assertIn("Reservation mutation capabilities are unavailable", form.non_field_errors()[0])

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

    def test_missing_subnet_cidr_fails(self):
        data = self._valid_data()
        del data["subnet_cidr"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_missing_ip_address_is_allowed(self):
        """A DHCPv4 host may reserve only a hostname, options or client classes.

        Kea accepts such an identifier-only reservation and omits ``ip-address`` when
        reporting it, so the form must not demand one.
        """
        data = self._valid_data()
        del data["ip_address"]
        form = self._form(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["ip_address"], "")

    def test_invalid_ip_address_still_fails(self):
        data = self._valid_data()
        data["ip_address"] = "not-an-ip"
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("ip_address", form.errors)

    def test_missing_identifier_type_fails(self):
        data = self._valid_data()
        del data["identifier_type"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("identifier_type", form.errors)
        self.assertNotIn(
            "This identifier is not enabled in the live Kea configuration.",
            form.errors["identifier_type"],
        )

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

    def test_invalid_subnet_cidr_fails(self):
        form = self._form(self._valid_data(subnet_cidr="not-a-cidr"))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_host_bits_set_in_subnet_cidr_fails(self):
        """A CIDR with host bits set (e.g. 192.168.1.5/24) is rejected (strict=True)."""
        form = self._form(self._valid_data(subnet_cidr="192.168.1.5/24"))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_ipv6_subnet_cidr_rejected_in_v4_form(self):
        """A well-formed IPv6 CIDR must still be rejected by the IPv4 reservation form."""
        form = self._form(self._valid_data(subnet_cidr="2001:db8::/48"))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)
        # Must not leak the raw ipaddress parser text (e.g. "Expected 4 octets in ...").
        self.assertEqual(form.errors["subnet_cidr"], ["Enter a valid IPv4 subnet CIDR (e.g. 10.0.0.0/24)."])

    def test_bare_address_without_prefix_fails(self):
        """A bare IP address (no /prefix) must be rejected, not silently treated as /32."""
        form = self._form(self._valid_data(subnet_cidr="192.168.1.5"))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_netmask_form_is_canonicalized_to_prefix_length(self):
        """A dotted-decimal netmask CIDR is canonicalized so it matches Kea's own reporting.

        configured_subnet_id_from_cidr() matches running configuration entries, and
        Kea always reports subnets with a prefix-length suffix, never a netmask.
        """
        form = self._form(self._valid_data(subnet_cidr="10.0.0.0/255.255.255.0"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["subnet_cidr"], "10.0.0.0/24")

    def test_disabled_subnet_cidr_skips_cidr_validation(self):
        """A disabled subnet_cidr (edit views) is not re-validated as a CIDR.

        The edit views seed it from a server-side lookup that falls back to the
        raw Kea subnet ID (e.g. "1") when that lookup fails — not a valid CIDR,
        but not user input either, so it must not fail validation.
        """
        from netbox_kea.forms import Reservation4Form

        form = Reservation4Form(
            data=self._valid_data(),
            initial={"subnet_cidr": "1"},
            capabilities=_reservation_capabilities(4),
        )
        form.fields["subnet_cidr"].disabled = True
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["subnet_cidr"], "1")

    def test_invalid_identifier_type_choice_fails(self):
        form = self._form(self._valid_data(identifier_type="not-a-real-type"))
        self.assertFalse(form.is_valid())
        self.assertIn("identifier_type", form.errors)
        self.assertNotIn(
            "This identifier is not enabled in the live Kea configuration.",
            form.errors["identifier_type"],
        )

    def test_identifier_errors_are_curated_validation_copy(self):
        """The domain ValueError *is* the field's validation text, so pin what reaches the page.

        Change this list only with a fresh check that the new message carries no internal
        URL, TLS, or Kea configuration detail.
        """
        malformed = self._form(self._valid_data(identifier="zz:zz:zz:zz:zz:zz"))
        self.assertFalse(malformed.is_valid())
        self.assertEqual(malformed.errors["identifier"], ["The Reservation identifier is invalid."])

        too_long = self._form(self._valid_data(identifier_type="circuit-id", identifier="a" * 256))
        self.assertFalse(too_long.is_valid())
        self.assertEqual(
            too_long.errors["identifier"],
            ["Reservation identifier value must not exceed 255 characters."],
        )

    def test_max_octet_client_id_is_accepted(self):
        """A 128-octet client-id is 383 characters delimited — past the opaque 255-char cap."""
        identifier = ":".join(["ab"] * constants.CLIENT_ID_MAX_OCTETS)
        form = self._form(self._valid_data(identifier_type="client-id", identifier=identifier))
        self.assertTrue(form.is_valid(), form.errors)

    def test_client_id_past_its_octet_limit_is_rejected(self):
        identifier = ":".join(["ab"] * (constants.CLIENT_ID_MAX_OCTETS + 1))
        form = self._form(self._valid_data(identifier_type="client-id", identifier=identifier))
        self.assertFalse(form.is_valid())
        self.assertIn("identifier", form.errors)

    def test_opaque_identifier_is_preserved_exactly(self):
        form = self._form(self._valid_data(identifier_type="flex-id", identifier="a" * 255))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["identifier"], "a" * 255)

    def test_opaque_identifier_past_the_length_limit_is_rejected(self):
        form = self._form(self._valid_data(identifier_type="flex-id", identifier="a" * 256))
        self.assertFalse(form.is_valid())
        self.assertIn("identifier", form.errors)


class TestReservationForm6(SimpleTestCase):
    """Tests for Reservation6Form (IPv6 reservation form validation)."""

    def _form(self, data):
        from netbox_kea.forms import Reservation6Form  # deferred: class not yet defined

        return Reservation6Form(data=data, capabilities=_reservation_capabilities(6))

    def _valid_data(self, **overrides):
        base = {
            "subnet_cidr": "2001:db8::/48",
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

    def test_mutation_fails_closed_without_live_capabilities(self):
        from netbox_kea.forms import Reservation6Form

        form = Reservation6Form(data=self._valid_data())
        self.assertFalse(form.is_valid())
        self.assertIn("Reservation mutation capabilities are unavailable", form.non_field_errors()[0])

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

    def test_missing_subnet_cidr_fails(self):
        data = self._valid_data()
        del data["subnet_cidr"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_invalid_subnet_cidr_fails(self):
        form = self._form(self._valid_data(subnet_cidr="not-a-cidr"))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_host_bits_set_in_subnet_cidr_fails(self):
        """A CIDR with host bits set (e.g. 2001:db8::1/48) is rejected (strict=True)."""
        form = self._form(self._valid_data(subnet_cidr="2001:db8::1/48"))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_ipv4_subnet_cidr_rejected_in_v6_form(self):
        """A well-formed IPv4 CIDR must still be rejected by the IPv6 reservation form."""
        form = self._form(self._valid_data(subnet_cidr="192.168.1.0/24"))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)
        # Must not leak the raw ipaddress parser text (e.g. "At least 3 parts expected...").
        self.assertEqual(form.errors["subnet_cidr"], ["Enter a valid IPv6 subnet CIDR (e.g. 2001:db8::/48)."])

    def test_bare_address_without_prefix_fails(self):
        """A bare IPv6 address (no /prefix) must be rejected, not silently treated as /128."""
        form = self._form(self._valid_data(subnet_cidr="2001:db8::5"))
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_expanded_notation_is_canonicalized_to_compressed_form(self):
        """A fully-expanded IPv6 CIDR is canonicalized so it matches Kea's own reporting.

        configured_subnet_id_from_cidr() matches running configuration entries, and
        Kea always reports subnets in compressed form.
        """
        form = self._form(self._valid_data(subnet_cidr="2001:0db8:0000:0000:0000:0000:0000:0000/32"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["subnet_cidr"], "2001:db8::/32")

    def test_disabled_subnet_cidr_skips_cidr_validation(self):
        """A disabled subnet_cidr (edit views) is not re-validated as a CIDR.

        The edit views seed it from a server-side lookup that falls back to the
        raw Kea subnet ID (e.g. "1") when that lookup fails — not a valid CIDR,
        but not user input either, so it must not fail validation.
        """
        from netbox_kea.forms import Reservation6Form

        form = Reservation6Form(
            data=self._valid_data(),
            initial={"subnet_cidr": "1"},
            capabilities=_reservation_capabilities(6),
        )
        form.fields["subnet_cidr"].disabled = True
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["subnet_cidr"], "1")

    def test_missing_ip_addresses_is_allowed(self):
        """A DHCPv6 host may delegate only prefixes, or reserve only a hostname."""
        data = self._valid_data()
        del data["ip_addresses"]
        form = self._form(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["ip_addresses"], "")

    def test_invalid_ip_addresses_still_fails(self):
        data = self._valid_data()
        data["ip_addresses"] = "2001:db8::1,not-an-ip"
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("ip_addresses", form.errors)

    def test_delegated_prefixes_are_canonicalised_and_deduplicated(self):
        data = self._valid_data()
        data["prefixes"] = "2001:db8:1::/64, 2001:0db8:1::/64 ,2001:db8:2::/64"
        form = self._form(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["prefixes"], "2001:db8:1::/64,2001:db8:2::/64")

    def test_prefix_with_host_bits_set_fails(self):
        """``2001:db8::1/64`` names a host inside a prefix, not the prefix itself."""
        data = self._valid_data()
        data["prefixes"] = "2001:db8::1/64"
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("prefixes", form.errors)

    def test_ipv4_prefix_is_rejected(self):
        data = self._valid_data()
        data["prefixes"] = "10.0.0.0/24"
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("prefixes", form.errors)

    def test_bare_address_without_length_is_rejected(self):
        data = self._valid_data()
        data["prefixes"] = "2001:db8:1::"
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("prefixes", form.errors)

    def test_zero_length_prefix_is_rejected(self):
        """``::/0`` is the whole address space, not a delegable prefix (length is 1–128)."""
        data = self._valid_data()
        data["prefixes"] = "::/0"
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("prefixes", form.errors)

    def test_delegable_prefix_length_is_still_accepted(self):
        data = self._valid_data()
        data["prefixes"] = "2001:db8::/48"
        form = self._form(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["prefixes"], "2001:db8::/48")

    def test_max_octet_duid_is_accepted(self):
        """A 128-octet DUID is 383 characters delimited — past the opaque 255-char cap."""
        identifier = ":".join(["ab"] * constants.DUID_MAX_OCTETS)
        form = self._form(self._valid_data(identifier=identifier))
        self.assertTrue(form.is_valid(), form.errors)

    def test_duid_past_its_octet_limit_is_rejected(self):
        identifier = ":".join(["ab"] * (constants.DUID_MAX_OCTETS + 1))
        form = self._form(self._valid_data(identifier=identifier))
        self.assertFalse(form.is_valid())
        self.assertIn("identifier", form.errors)

    def test_missing_identifier_type_fails(self):
        data = self._valid_data()
        del data["identifier_type"]
        form = self._form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("identifier_type", form.errors)
        self.assertNotIn(
            "This identifier is not enabled in the live Kea configuration.",
            form.errors["identifier_type"],
        )

    def test_invalid_identifier_type_choice_fails(self):
        form = self._form(self._valid_data(identifier_type="not-a-real-type"))
        self.assertFalse(form.is_valid())
        self.assertIn("identifier_type", form.errors)
        self.assertNotIn(
            "This identifier is not enabled in the live Kea configuration.",
            form.errors["identifier_type"],
        )

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

    def test_identifier_type_choices_exclude_client_id(self):
        from netbox_kea.forms import Reservation6Form

        choices = [c[0] for c in Reservation6Form().fields["identifier_type"].choices]
        self.assertNotIn("client-id", choices)

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

        data = {
            "subnet_cidr": "10.0.0.0/24",
            "original_network_confirmed": "True",
            "shared_networks_complete": "True",
            **kwargs,
        }
        return SubnetEditForm(data=data)

    def test_valid_minimal_form_no_optional_fields(self):
        """A form with only subnet_cidr (hidden) and no optional fields is valid."""
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_page_that_could_not_confirm_the_shared_network_cannot_be_saved(self):
        """A missing or false confirmation fails closed, so a save never guesses the membership."""
        unconfirmed = (
            "NetBox could not confirm the Shared Network of this Subnet when it showed the page. "
            "Reload the page and try again."
        )
        for value in ("False", ""):
            with self.subTest(confirmed=value):
                form = self._form(original_network_confirmed=value)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.non_field_errors(), [unconfirmed])

    def test_shown_cleans_each_hidden_copy_the_same_way_as_its_field(self):
        from netbox_kea.kea import SubnetEdit, SubnetFields

        values = {
            "pools": " 10.0.0.16/28 \r\n\r\n10.0.0.100 - 10.0.0.110",
            "gateway": " 10.0.0.1 ",
            "dns_servers": "10.0.0.53 , 10.0.0.54",
            "ntp_servers": "",
            "ddns_qualifying_suffix": " example.org. ",
            "valid_lft": "3600",
            "rebind_timer": "",
        }
        form = self._form(**values, **{f"shown_{name}": value for name, value in values.items()})
        self.assertTrue(form.is_valid(), form.errors)
        expected = SubnetEdit(
            fields=SubnetFields(
                pools=("10.0.0.16-10.0.0.31", "10.0.0.100-10.0.0.110"),
                gateway="10.0.0.1",
                dns_servers=("10.0.0.53", "10.0.0.54"),
                ntp_servers=(),
                ddns_qualifying_suffix="example.org.",
            ),
            valid_lifetime=3600,
            min_valid_lifetime=None,
            max_valid_lifetime=None,
            renew_timer=None,
            rebind_timer=None,
        )
        self.assertEqual((form.to_edit(), form.shown()), (expected, expected))

    def test_each_hidden_copy_is_its_field_with_a_hidden_widget_and_no_input_limits(self):
        from django.forms import HiddenInput

        from netbox_kea.forms import SharedNetworkEditForm, SubnetEditForm

        for form in (SubnetEditForm(), SharedNetworkEditForm()):
            for name in form.shown_names:
                with self.subTest(form=type(form).__name__, field=name):
                    hidden = form.fields[f"shown_{name}"]
                    self.assertIs(type(hidden), type(form.fields[name]))
                    self.assertIsInstance(hidden.widget, HiddenInput)
                    self.assertEqual((hidden.required, hidden.validators), (False, []))
        # A live 0 and a long value are what Kea holds, so the hidden copies accept them.
        form = self._form(shown_valid_lft="0", shown_ddns_qualifying_suffix="x" * 300)
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_shown_value_that_does_not_clean_refuses_the_form(self):
        for field, value in (("shown_pools", "10.1.0.0/28"), ("shown_gateway", "gw"), ("shown_valid_lft", "x")):
            with self.subTest(field=field):
                form = self._form(**{field: value})
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.non_field_errors(),
                    ["The values that the page showed are not valid. Reload the page and try again."],
                )

    def test_the_original_network_keeps_the_name_as_kea_declares_it(self):
        form = self._form(original_network=" office ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["original_network"], " office ")

    def test_valid_form_with_all_fields(self):
        """A fully populated form is valid."""
        form = self._form(
            pools="10.0.0.100-10.0.0.200",
            gateway="10.0.0.1",
            dns_servers="8.8.8.8, 1.1.1.1",
            ntp_servers="192.0.2.123",
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

    def test_pools_cleaned_as_parsed_pools(self):
        """Each non-empty line becomes one parsed Pool; a CIDR Pool becomes its range."""
        form = self._form(pools="10.0.0.100-10.0.0.150\n\n 10.0.0.192/27 \n")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            [pool.range for pool in form.cleaned_data["pools"]], ["10.0.0.100-10.0.0.150", "10.0.0.192-10.0.0.223"]
        )

    def test_pools_are_parsed_inside_a_subnet_cidr_with_host_bits(self):
        form = self._form(subnet_cidr="10.0.0.5/24", pools="10.0.0.100-10.0.0.150")
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_pool_outside_the_subnet_is_a_pools_error(self):
        form = self._form(pools="10.0.0.100-10.0.0.150\n10.0.1.0/28")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["pools"], ["Pool 10.0.1.0/28 is outside Subnet 10.0.0.0/24."])

    def test_pools_that_overlap_each_other_are_a_pools_error(self):
        form = self._form(pools="10.0.0.100-10.0.0.150\n10.0.0.128/27")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["pools"], ["Pool 10.0.0.128-10.0.0.159 overlaps Pool 10.0.0.100-10.0.0.150."])

    def test_dns_servers_cleaned_as_list(self):
        """clean_dns_servers returns a list of IP strings."""
        form = self._form(dns_servers="8.8.8.8, 1.1.1.1")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["dns_servers"], ["8.8.8.8", "1.1.1.1"])

    def test_ntp_servers_validated_as_addresses(self):
        """Option 42 and its v6 counterpart are address arrays: Kea rejects a hostname."""
        for subnet, value, expected in (
            ("192.0.2.0/24", " 192.0.2.123, , 192.0.2.124, ", ["192.0.2.123", "192.0.2.124"]),
            ("2001:db8::/64", " 2001:db8::123, , 2001:db8::124, ", ["2001:db8::123", "2001:db8::124"]),
        ):
            with self.subTest(subnet=subnet):
                form = self._form(subnet_cidr=subnet, ntp_servers=value)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["ntp_servers"], expected)

                form = self._form(subnet_cidr=subnet, ntp_servers="ntp.example.com")
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["ntp_servers"], ["Invalid NTP server IP address: 'ntp.example.com'"])


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

        data = {
            "subnet_cidr": "10.0.0.0/24",
            "original_network_confirmed": "True",
            "shared_networks_complete": "True",
            **kwargs,
        }
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
    """SubnetAddForm takes the Shared Network as a free name; config_write checks that it exists."""

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

        form = SubnetAddForm(data={"subnet": "10.0.0.0/24", "shared_network": "", "shared_networks_complete": "True"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("shared_network", form.errors)

    def test_a_name_that_no_list_offers_is_valid_as_posted(self):
        from netbox_kea.forms import SubnetAddForm

        data = {"subnet": "10.0.0.0/24", "shared_network": " clients", "shared_networks_complete": "True"}
        form = SubnetAddForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["shared_network"], " clients")

    def test_a_name_of_only_white_space_is_refused(self):
        from netbox_kea.forms import SubnetAddForm

        data = {"subnet": "10.0.0.0/24", "shared_network": " ", "shared_networks_complete": "True"}
        form = SubnetAddForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["shared_network"], ["Enter a Shared Network name, or choose none."])

    def test_a_page_without_the_shared_network_list_cannot_be_saved(self):
        """A disabled select posts no name, so the form must not read the missing name as no Shared Network."""
        from netbox_kea.forms import SubnetAddForm, SubnetEditForm

        forms = (
            SubnetAddForm(data={"subnet": "10.0.0.0/24"}),
            SubnetEditForm(data={"subnet_cidr": "10.0.0.0/24", "original_network_confirmed": "True"}),
        )
        for form in forms:
            with self.subTest(form=type(form).__name__):
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.non_field_errors(),
                    [
                        (
                            "NetBox could not load the Shared Networks from Kea when it showed the page. "
                            "Reload the page and try again."
                        )
                    ],
                )


class TestSubnetAddFormAddressFamily(SimpleTestCase):
    """Option 6 and option 42 are arrays of the subnet's own family; Kea rejects a mismatch."""

    def _form(self, **overrides):
        from netbox_kea.forms import SubnetAddForm

        data = {"subnet": "192.0.2.0/24", "shared_network": "", "shared_networks_complete": "True"}
        data.update(overrides)
        return SubnetAddForm(data=data)

    def test_rejects_mismatched_dns_and_ntp_addresses(self):
        for field, label in (("dns_servers", "DNS server"), ("ntp_servers", "NTP server")):
            with self.subTest(field=field):
                form = self._form(**{field: "2001:db8::123"})
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors[field],
                    [f"{label} '2001:db8::123' must be an IPv4 address to match the subnet family."],
                )

    def test_accepts_matching_addresses(self):
        form = self._form(dns_servers="192.0.2.53", ntp_servers="192.0.2.123")
        self.assertTrue(form.is_valid(), form.errors)


class TestSubnetEditFormAddressFamily(SimpleTestCase):
    """The edit form carries the subnet CIDR in a hidden field, so it can check the family too."""

    def _form(self, **overrides):
        from netbox_kea.forms import SubnetEditForm

        data = {"subnet_cidr": "192.0.2.0/24", "original_network_confirmed": "True", "shared_networks_complete": "True"}
        data.update(overrides)
        return SubnetEditForm(data=data)

    def test_rejects_mismatched_dns_and_ntp_addresses(self):
        for field, label in (("dns_servers", "DNS server"), ("ntp_servers", "NTP server")):
            with self.subTest(field=field):
                form = self._form(**{field: "2001:db8::123"})
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors[field],
                    [f"{label} '2001:db8::123' must be an IPv4 address to match the subnet family."],
                )

    def test_rejects_mismatched_dns_and_ntp_addresses_on_v6(self):
        for field, label in (("dns_servers", "DNS server"), ("ntp_servers", "NTP server")):
            with self.subTest(field=field):
                form = self._form(subnet_cidr="2001:db8::/64", **{field: "192.0.2.53"})
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors[field],
                    [f"{label} '192.0.2.53' must be an IPv6 address to match the subnet family."],
                )

    def test_rejects_mismatched_gateway(self):
        form = self._form(gateway="2001:db8::1")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["gateway"], ["Gateway must be an IPv4 address to match the subnet family."])

    def test_rejects_gateway_on_v6_subnet(self):
        form = self._form(subnet_cidr="2001:db8::/64", gateway="2001:db8::1")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["gateway"], ["Gateway is not allowed for IPv6 subnets."])

    def test_rejects_subnet_cidr_that_is_not_a_network(self):
        """The hidden field is client-supplied and goes straight to subnet{v}-update."""
        form = self._form(subnet_cidr="not-a-subnet")
        self.assertFalse(form.is_valid())
        self.assertIn("subnet_cidr", form.errors)

    def test_accepts_a_prefix_with_host_bits_set(self):
        """Kea allows it and reports it back; the add form still rejects it as a typo."""
        from netbox_kea.forms import SubnetAddForm

        form = self._form(subnet_cidr="10.0.0.5/24", dns_servers="10.0.0.53")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["subnet_cidr"], "10.0.0.5/24")

        add = SubnetAddForm(data={"subnet": "10.0.0.5/24", "shared_network": "", "shared_networks_complete": "True"})
        self.assertFalse(add.is_valid())
        self.assertIn("subnet", add.errors)

    def test_accepts_matching_addresses(self):
        for subnet, gateway, dns, ntp in (
            ("192.0.2.0/24", "192.0.2.1", "192.0.2.53", "192.0.2.123"),
            ("2001:db8::/64", "", "2001:db8::53", "2001:db8::123"),
        ):
            with self.subTest(subnet=subnet):
                form = self._form(subnet_cidr=subnet, gateway=gateway, dns_servers=dns, ntp_servers=ntp)
                self.assertTrue(form.is_valid(), form.errors)


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
        self.assertEqual(form.cleaned_data["relay_addresses"], [])

    def test_to_edit_applies_one_strip_rule_to_every_address_list(self):
        from netbox_kea.kea import SharedNetworkEdit

        form = self._form(
            description=" Office ",
            interface=" eth1 ",
            relay_addresses=" 10.0.0.1 , ,10.0.0.2 ",
            dns_servers=" 8.8.8.8 , , 1.1.1.1",
            ntp_servers=" , ",
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.to_edit(),
            SharedNetworkEdit("Office", "eth1", ("10.0.0.1", "10.0.0.2"), ("8.8.8.8", "1.1.1.1"), ()),
        )

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

    def test_description_accepts_a_comment_of_any_length(self):
        """Kea puts no length limit on a comment, so an existing long comment must not block an edit."""
        form = self._form(description="x" * 1000)
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_single_dns_server(self):
        """A single valid DNS server IP is accepted and normalized."""
        form = self._form(dns_servers="  8.8.8.8  ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["dns_servers"], ["8.8.8.8"])

    def test_valid_multiple_dns_servers(self):
        """Multiple comma-separated DNS server IPs are accepted and normalized."""
        form = self._form(dns_servers="8.8.8.8 , 1.1.1.1")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["dns_servers"], ["8.8.8.8", "1.1.1.1"])

    def test_invalid_dns_server_fails_validation(self):
        """A non-IP value in dns_servers raises a ValidationError."""
        form = self._form(dns_servers="not-an-ip")
        self.assertFalse(form.is_valid())
        self.assertIn("dns_servers", form.errors)

    def test_valid_single_ntp_server(self):
        """A single valid NTP server IP is accepted and normalized."""
        form = self._form(ntp_servers="  10.0.0.1  ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["ntp_servers"], ["10.0.0.1"])

    def test_valid_multiple_ntp_servers(self):
        """Multiple comma-separated NTP server IPs are accepted and normalized."""
        form = self._form(ntp_servers="10.0.0.1 , 10.0.0.2")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["ntp_servers"], ["10.0.0.1", "10.0.0.2"])

    def test_invalid_ntp_server_fails_validation(self):
        """A non-IP value in ntp_servers raises a ValidationError."""
        form = self._form(ntp_servers="not-valid")
        self.assertFalse(form.is_valid())
        self.assertIn("ntp_servers", form.errors)

    def test_relay_addresses_normalized(self):
        """Relay addresses with extra whitespace are normalized to canonical addresses."""
        form = self._form(relay_addresses="  10.0.0.1 , 2001:DB8::0001  ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["relay_addresses"], ["10.0.0.1", "2001:db8::1"])

    def test_shown_cleans_each_hidden_copy_the_same_way_as_its_field(self):
        from netbox_kea.kea import SharedNetworkEdit

        values = {
            "description": " Office\r\nFloor 2 ",
            "interface": " eth1 ",
            "relay_addresses": " 10.0.0.1 , ,2001:DB8::0001 ",
            "dns_servers": "8.8.8.8,1.1.1.1",
            "ntp_servers": " , ",
        }
        form = self._form(**values, **{f"shown_{name}": value for name, value in values.items()})
        self.assertTrue(form.is_valid(), form.errors)
        expected = SharedNetworkEdit("OfficeFloor 2", "eth1", ("10.0.0.1", "2001:db8::1"), ("8.8.8.8", "1.1.1.1"), ())
        self.assertEqual((form.to_edit(), form.shown()), (expected, expected))

    def test_a_shown_value_that_does_not_clean_refuses_the_form(self):
        form = self._form(shown_dns_servers="not-an-ip")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.non_field_errors(), ["The values that the page showed are not valid. Reload the page and try again."]
        )


class TestLeasesSearchFormSubnetCombobox(SimpleTestCase):
    """The lease search form has no standalone subnet field; choices feed the template combobox."""

    def test_no_subnet_field_even_when_choices_supplied(self):
        # Previously a separate ``subnet`` quick-select field was added; it's gone now.
        form = Leases4SearchForm(subnet_choices=(("198.18.0.0/24", 1),))
        self.assertNotIn("subnet", form.fields)

    def test_subnet_choices_exposed_for_template(self):
        form = Leases6SearchForm(subnet_choices=(("2001:db8::/64", 5),))
        self.assertEqual(form.subnet_choices, (("2001:db8::/64", 5),))

    def test_subnet_choices_defaults_to_empty(self):
        self.assertEqual(Leases4SearchForm().subnet_choices, ())

    def test_subnet_search_still_validates(self):
        form = Leases4SearchForm(data={"by": "subnet", "q": "192.168.1.0/24"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_subnet_id_search_still_validates(self):
        form = Leases4SearchForm(data={"by": "subnet_id", "q": "3"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["q"], 3)

    def test_subnet_search_rejects_a_page_cursor(self):
        form = Leases4SearchForm(data={"by": "subnet", "q": "192.168.1.0/24", "page": "192.168.1.10"})
        self.assertFalse(form.is_valid())
        self.assertIn("page", form.errors)


class TestReservationIdentifierCapabilities(SimpleTestCase):
    """The identifier the operator picked must be one the live Kea config enables."""

    def test_v4_rejects_an_identifier_the_live_configuration_disables(self):
        from netbox_kea.forms import Reservation4Form

        form = Reservation4Form(
            data={
                "subnet_cidr": "192.168.1.0/24",
                "ip_address": "192.168.1.100",
                "identifier_type": "client-id",
                "identifier": "01aabbccddeeff",
            },
            capabilities=_reservation_capabilities(4, identifiers=("hw-address",)),
        )

        self.assertFalse(form.is_valid())
        self.assertIn(
            "This identifier is not enabled in the live Kea configuration.",
            form.errors["identifier_type"],
        )

    def test_v4_accepts_an_identifier_the_live_configuration_enables(self):
        from netbox_kea.forms import Reservation4Form

        form = Reservation4Form(
            data={
                "subnet_cidr": "192.168.1.0/24",
                "ip_address": "192.168.1.100",
                "identifier_type": "hw-address",
                "identifier": "aa:bb:cc:dd:ee:ff",
            },
            capabilities=_reservation_capabilities(4, identifiers=("hw-address",)),
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_v6_rejects_an_identifier_the_live_configuration_disables(self):
        from netbox_kea.forms import Reservation6Form

        form = Reservation6Form(
            data={
                "subnet_cidr": "2001:db8::/64",
                "ip_addresses": "2001:db8::100",
                "identifier_type": "hw-address",
                "identifier": "aa:bb:cc:dd:ee:ff",
            },
            capabilities=_reservation_capabilities(6, identifiers=("duid",)),
        )

        self.assertFalse(form.is_valid())
        self.assertIn(
            "This identifier is not enabled in the live Kea configuration.",
            form.errors["identifier_type"],
        )

    def test_disabled_identifier_option_is_rendered_disabled_with_its_reason(self):
        """Render the unavailable choice as disabled, not only reject it on submit.

        Server-side rejection is covered above. Nothing exercised
        `ReservationIdentifierSelect.create_option`, so a regression that stopped
        disabling the option would offer a choice Kea cannot accept.
        """
        from netbox_kea.forms import Reservation4Form

        form = Reservation4Form(capabilities=_reservation_capabilities(4, identifiers=("hw-address",)))
        markup = str(form["identifier_type"])

        disabled = re.findall(r'<option value="([^"]+)"[^>]*\bdisabled\b', markup)

        self.assertNotIn("hw-address", disabled)
        self.assertIn("client-id", disabled)
        self.assertIn('title="Not enabled in the live Kea configuration."', markup)


class TestBulkReservationImportForm(SimpleTestCase):
    """Exactly one document source, with a message that names the actual problem."""

    def _form(self, **kwargs):
        from netbox_kea.forms import Reservation4ImportForm

        return Reservation4ImportForm(**kwargs)

    def test_rejects_both_sources_at_once(self):
        upload = SimpleUploadedFile("r.yaml", b"reservations: []", content_type="application/yaml")
        form = self._form(data={"document": "reservations: []", "format": "yaml"}, files={"document_file": upload})

        self.assertFalse(form.is_valid())
        self.assertIn(
            "Paste a document or upload a document file, but do not use both.",
            form.non_field_errors(),
        )

    def test_rejects_neither_source_without_claiming_both_were_used(self):
        form = self._form(data={"format": "yaml"})

        self.assertFalse(form.is_valid())
        # The empty form is a distinct problem: "do not use both" described it wrongly.
        self.assertIn("Paste a document or upload a document file.", form.non_field_errors())
        self.assertNotIn(
            "Paste a document or upload a document file, but do not use both.",
            form.non_field_errors(),
        )

    def test_accepts_a_pasted_document(self):
        form = self._form(data={"document": "reservations: []", "format": "yaml"})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["document"], "reservations: []")

    def test_decodes_an_uploaded_document_as_utf8(self):
        upload = SimpleUploadedFile("r.yaml", "reservations: []\n# café\n".encode(), content_type="application/yaml")
        form = self._form(data={"format": "yaml"}, files={"document_file": upload})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("café", form.cleaned_data["document"])

    def test_rejects_an_upload_that_is_not_utf8(self):
        upload = SimpleUploadedFile("r.yaml", b"reservations: []\n# \xff\xfe", content_type="application/yaml")
        form = self._form(data={"format": "yaml"}, files={"document_file": upload})

        self.assertFalse(form.is_valid())
        self.assertIn("The document file must use UTF-8 encoding.", form.non_field_errors())

    def test_rejects_an_oversized_upload_before_reading_it(self):
        """Reject the upload on its declared size, so `clean` never buffers the content.

        Django's DATA_UPLOAD_MAX_MEMORY_SIZE does not apply to file uploads, so without
        this limit one request can allocate the whole file.
        """
        from netbox_kea.forms import _BaseBulkReservationImportForm

        limit = _BaseBulkReservationImportForm.MAX_DOCUMENT_BYTES
        oversized = SimpleUploadedFile("r.yaml", b"a" * (limit + 1), content_type="application/yaml")
        form = self._form(data={"format": "yaml"}, files={"document_file": oversized})

        self.assertFalse(form.is_valid())
        self.assertIn(f"The document file must not exceed {limit // (1024 * 1024)} MB.", form.non_field_errors())

    def test_accepts_an_upload_at_the_size_limit(self):
        from netbox_kea.forms import _BaseBulkReservationImportForm

        limit = _BaseBulkReservationImportForm.MAX_DOCUMENT_BYTES
        document = b"reservations: []\n" + b"#" * (limit - len(b"reservations: []\n"))
        upload = SimpleUploadedFile("r.yaml", document, content_type="application/yaml")
        form = self._form(data={"format": "yaml"}, files={"document_file": upload})

        self.assertTrue(form.is_valid(), form.errors)


def _verified_subnet(cidr="10.0.0.0/24", pools=("10.0.0.10-10.0.0.20",), *, configuration=True):
    """A Verified Subnet 1 with its declared Pools; ``configuration=False`` drops the configuration facts."""
    import ipaddress

    from netbox_kea.pools import parse_pool
    from netbox_kea.server_configuration import SubnetConfiguration, SubnetSettings
    from netbox_kea.subnet_catalogue import SubnetIdentity, VerifiedSubnet

    network = ipaddress.ip_network(cidr)
    facts = SubnetConfiguration(
        pools=tuple(parse_pool(pool, network) for pool in pools), options=(), settings=SubnetSettings()
    )
    return VerifiedSubnet(
        identity=SubnetIdentity(subnet_id=1, network=network),
        declared_cidr=cidr,
        configuration=facts if configuration else None,
        shared_network=None,
        membership_known=True,
    )


def _pool_add_form(*, data, subnet, absence_confirmed=True):
    return PoolAddForm(data=data, subnet=subnet, absence_confirmed=absence_confirmed)


class TestPoolAddForm(SimpleTestCase):
    """The Pool is parsed inside the Verified Subnet and must not overlap an existing Pool."""

    def test_a_cidr_pool_parses_to_its_range(self):
        form = _pool_add_form(data={"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.64/28"}, subnet=_verified_subnet())

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["pool"].range, "10.0.0.64-10.0.0.79")

    def test_a_range_pool_is_normalized(self):
        form = _pool_add_form(
            data={"subnet_cidr": "10.0.0.0/24", "pool": " 10.0.0.50 - 10.0.0.99 "}, subnet=_verified_subnet()
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["pool"].range, "10.0.0.50-10.0.0.99")

    def test_invalid_text_is_a_pool_error(self):
        for value, message in (
            ("10.0.0.0/99", "Pool 10.0.0.0/99 is not a valid prefix"),
            ("nonsense", "Pool nonsense must be a range (start-end) or a prefix (CIDR)."),
            ("10.0.0.50-10.0.0.x", "Pool 10.0.0.50-10.0.0.x has an invalid address"),
            ("10.0.0.90-10.0.0.80", "Pool 10.0.0.90-10.0.0.80 starts after it ends."),
        ):
            with self.subTest(value=value):
                form = _pool_add_form(data={"subnet_cidr": "10.0.0.0/24", "pool": value}, subnet=_verified_subnet())

                self.assertFalse(form.is_valid())
                self.assertIn(message, form.errors["pool"][0])

    def test_a_pool_outside_the_subnet_is_a_pool_error(self):
        form = _pool_add_form(data={"subnet_cidr": "10.0.0.0/24", "pool": "10.0.1.0/28"}, subnet=_verified_subnet())

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["pool"], ["Pool 10.0.1.0/28 is outside Subnet 10.0.0.0/24."])

    def test_an_overlap_with_an_existing_pool_is_a_pool_error(self):
        form = _pool_add_form(
            data={"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.20-10.0.0.30"}, subnet=_verified_subnet()
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["pool"], ["Pool 10.0.0.20-10.0.0.30 overlaps existing Pool 10.0.0.10-10.0.0.20."])

    def test_missing_configuration_facts_skip_the_overlap_check(self):
        form = _pool_add_form(
            data={"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.20-10.0.0.30"},
            subnet=_verified_subnet(configuration=False),
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_subnet_that_is_absent_from_a_complete_observation_is_a_form_error(self):
        form = _pool_add_form(data={"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.50-10.0.0.60"}, subnet=None)

        self.assertFalse(form.is_valid())
        self.assertNotIn("pool", form.errors)
        self.assertEqual(
            form.non_field_errors(),
            ["This Subnet is not in the current Subnet Catalogue. Reload the Subnets page and try again."],
        )

    def test_a_subnet_missing_from_an_incomplete_observation_never_reads_as_absent(self):
        form = _pool_add_form(
            data={"subnet_cidr": "10.0.0.0/24", "pool": "10.0.0.50-10.0.0.60"}, subnet=None, absence_confirmed=False
        )

        self.assertFalse(form.is_valid())
        self.assertNotIn("pool", form.errors)
        self.assertEqual(
            form.non_field_errors(),
            ["NetBox could not confirm Kea's Subnet list, so it did not send the change. Try again later."],
        )

    def test_a_cidr_that_is_not_the_subnet_says_that_the_subnet_changed(self):
        form = _pool_add_form(
            data={"subnet_cidr": "10.0.1.0/24", "pool": "10.0.1.50-10.0.1.60"}, subnet=_verified_subnet()
        )

        self.assertFalse(form.is_valid())
        self.assertNotIn("pool", form.errors)
        self.assertEqual(
            form.non_field_errors(), ["Subnet 1 (10.0.1.0/24) changed in Kea. Reload the page and try again."]
        )

    def test_a_missing_invalid_or_other_family_cidr_is_a_form_error(self):
        for cidr in ("", "not-a-cidr", "2001:db8::/64"):
            with self.subTest(cidr=cidr):
                form = _pool_add_form(
                    data={"subnet_cidr": cidr, "pool": "10.0.0.50-10.0.0.60"}, subnet=_verified_subnet()
                )

                self.assertFalse(form.is_valid())
                self.assertNotIn("pool", form.errors)
                self.assertIn("Invalid subnet CIDR", form.non_field_errors()[0])

    def test_the_cidr_that_kea_declares_with_host_bits_names_the_subnet(self):
        form = _pool_add_form(
            data={"subnet_cidr": "10.0.0.5/24", "pool": "10.0.0.50-10.0.0.60"}, subnet=_verified_subnet()
        )

        self.assertTrue(form.is_valid(), form.errors)


class TestSubnetConfirmForm(SimpleTestCase):
    """The hidden CIDR of a Subnet change page must be a CIDR of the family."""

    def test_a_cidr_of_the_family_is_valid_with_host_bits(self):
        for family, cidr in ((4, " 10.0.0.5/24 "), (6, "2001:db8:1::1/64")):
            with self.subTest(family=family):
                form = SubnetConfirmForm(data={"subnet_cidr": cidr}, family=family)

                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["subnet_cidr"], cidr.strip())

    def test_a_missing_invalid_or_other_family_cidr_is_invalid(self):
        for cidr in ("", "nonsense", "2001:db8::/64"):
            with self.subTest(cidr=cidr):
                self.assertFalse(SubnetConfirmForm(data={"subnet_cidr": cidr}, family=4).is_valid())
