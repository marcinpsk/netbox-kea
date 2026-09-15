# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Server API must keep accepting null for the optional text fields.

Those columns were nullable before 0016, so every released version answered GET
with null for an unset credential or path. A client that reads a Server, edits one
field and writes the whole object back therefore sends null for the rest. Making
the columns NOT NULL turns that round trip into a 400 unless the serializer keeps
taking null and storing it as "".
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .kea_stub import stub_kea
from .utils import _make_db_server

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

#: The columns 0016 moved from NULL-or-blank to blank only.
OPTIONAL_TEXT_FIELDS = (
    "ca_username",
    "ca_password",
    "dhcp4_username",
    "dhcp4_password",
    "dhcp6_username",
    "dhcp6_password",
    "client_cert_path",
    "client_key_path",
    "ca_file_path",
    "dhcp4_url",
    "dhcp6_url",
)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerOptionalTextFieldsAcceptNull(TestCase):
    """A null for any optional text field must be stored as "", not rejected."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="server_api_user",
            email="server_api@example.com",
            password="server_api_pass",
        )
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)
        self.server = _make_db_server(name="api-null", dhcp4=True, dhcp6=False)
        self.url = reverse("plugins-api:netbox_kea-api:server-detail", args=[self.server.pk])

    def test_patching_every_optional_field_to_null_is_accepted(self):
        """This is the read-modify-write shape a pre-0016 client produces."""
        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "2.4.0"}}}):
            response = self.api_client.patch(self.url, dict.fromkeys(OPTIONAL_TEXT_FIELDS), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.server.refresh_from_db()
        for field in OPTIONAL_TEXT_FIELDS:
            self.assertEqual(getattr(self.server, field), "", f"{field} did not become blank")

    def test_a_null_does_not_erase_a_neighbouring_value(self):
        """Only the fields the client actually sent may change."""
        self.server.ca_username = "kea-operator"
        self.server.save()

        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "2.4.0"}}}):
            response = self.api_client.patch(self.url, {"dhcp4_url": None}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.server.refresh_from_db()
        self.assertEqual(self.server.dhcp4_url, "")
        self.assertEqual(self.server.ca_username, "kea-operator")

    def test_null_clears_a_populated_field(self):
        """The fields must start populated, or dropping null keys would also pass."""
        for field in OPTIONAL_TEXT_FIELDS:
            setattr(self.server, field, "set-before" if "path" not in field and "url" not in field else "/set/before")
        self.server.save()

        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "2.4.0"}}}):
            response = self.api_client.patch(self.url, dict.fromkeys(OPTIONAL_TEXT_FIELDS), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.server.refresh_from_db()
        for field in OPTIONAL_TEXT_FIELDS:
            self.assertEqual(getattr(self.server, field), "", f"{field} kept its old value instead of clearing")

    def test_a_real_value_still_round_trips(self):
        """Accepting null must not turn every write into a blank."""
        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "2.4.0"}}}):
            response = self.api_client.patch(self.url, {"ca_username": "kea-operator"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.server.refresh_from_db()
        self.assertEqual(self.server.ca_username, "kea-operator")
