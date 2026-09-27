# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Server sync fields must be readable and writable over the REST API.

They were settable only in the web edit form: ``ServerSerializer.Meta.fields``
stopped at ``has_control_agent``, so an operator automating NetBox could not turn
IPAM sync on, pick a sync VRF or reveal the DHCP-plugin tab. ``persist_config``
stays off this surface on purpose; it changes what every subsequent write does to
Kea's on-disk configuration.
"""

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from ipam.models import VRF
from rest_framework.test import APIClient

from netbox_kea.api.serializers import ServerSerializer
from netbox_kea.models import Server

from .kea_stub import stub_kea
from .utils import _make_db_server

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}
_VERSION_OK = {"version-get": {"result": 0, "arguments": {"extended": "2.4.0"}}}

#: Every boolean the periodic sync job and the DHCP-plugin tab gate read.
SYNC_BOOLEANS = (
    "sync_enabled",
    "sync_leases_enabled",
    "sync_reservations_enabled",
    "sync_prefixes_enabled",
    "sync_ip_ranges_enabled",
    "sync_dhcp_plugin_enabled",
)


class TestServerSerializerCoversEveryEditableField(SimpleTestCase):
    """A model field the serializer omits cannot be set by any API client.

    custom_field_data is NetBox's own JSON store, which NetBoxModelSerializer
    exposes as ``custom_fields`` instead. persist_config is a deliberate product
    call: it is edit-form and CSV-import only.
    """

    def test_serializer_writes_every_editable_server_field_but_the_allowed_omissions(self):
        editable = {
            field.name
            for field in Server._meta.get_fields()
            if getattr(field, "editable", False) and not field.auto_created
        }
        writable = {name for name, field in ServerSerializer().fields.items() if not field.read_only}

        self.assertEqual(editable - writable, {"custom_field_data", "persist_config"})


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerSyncFieldsOverRest(TestCase):
    """Round-trip the sync fields through the real serializer, view and database."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="sync_api_user",
            email="sync_api@example.com",
            password="sync_api_pass",
        )
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)
        self.vrf = VRF.objects.create(name="kea-sync-vrf")
        self.server = _make_db_server(name="api-sync", dhcp4=True, dhcp6=False)
        self.detail_url = reverse("plugins-api:netbox_kea-api:server-detail", args=[self.server.pk])
        self.list_url = reverse("plugins-api:netbox_kea-api:server-list")

    def test_patch_flips_every_sync_boolean_and_sets_the_vrf(self):
        """The defaults are True/True/True/True/True/False, so each value here is a change."""
        payload = {name: not getattr(self.server, name) for name in SYNC_BOOLEANS}
        payload["sync_vrf"] = self.vrf.pk

        with stub_kea(_VERSION_OK):
            response = self.api_client.patch(self.detail_url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.server.refresh_from_db()
        for name in SYNC_BOOLEANS:
            self.assertEqual(getattr(self.server, name), payload[name], f"{name} did not change")
        self.assertEqual(self.server.sync_vrf, self.vrf)

    def test_patch_clears_the_sync_vrf(self):
        self.server.sync_vrf = self.vrf
        self.server.save()

        with stub_kea(_VERSION_OK):
            response = self.api_client.patch(self.detail_url, {"sync_vrf": None}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.server.refresh_from_db()
        self.assertIsNone(self.server.sync_vrf)

    def test_post_creates_a_server_with_the_sync_fields_set(self):
        payload = {
            "name": "api-created",
            "ca_url": "https://created.example.com",
            "sync_enabled": False,
            "sync_leases_enabled": False,
            "sync_reservations_enabled": False,
            "sync_prefixes_enabled": False,
            "sync_ip_ranges_enabled": False,
            "sync_dhcp_plugin_enabled": True,
            "sync_vrf": self.vrf.pk,
        }

        with stub_kea(_VERSION_OK):
            response = self.api_client.post(self.list_url, payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        created = Server.objects.get(name="api-created")
        for name in SYNC_BOOLEANS:
            self.assertEqual(getattr(created, name), payload[name], f"{name} was not applied")
        self.assertEqual(created.sync_vrf, self.vrf)

    def test_get_reports_the_sync_fields_and_a_nested_vrf(self):
        self.server.sync_dhcp_plugin_enabled = True
        self.server.sync_vrf = self.vrf
        self.server.save()

        response = self.api_client.get(self.detail_url)

        self.assertEqual(response.status_code, 200, response.data)
        for name in SYNC_BOOLEANS:
            self.assertEqual(response.data[name], getattr(self.server, name), f"{name} missing from GET")
        self.assertEqual(response.data["sync_vrf"]["id"], self.vrf.pk)
        self.assertEqual(response.data["sync_vrf"]["name"], self.vrf.name)

    def test_persist_config_is_neither_read_nor_written_over_rest(self):
        """Exposing it would let an API client silently stop Kea persisting its config."""
        with stub_kea(_VERSION_OK):
            response = self.api_client.patch(self.detail_url, {"persist_config": False}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("persist_config", response.data)
        self.server.refresh_from_db()
        self.assertTrue(self.server.persist_config)
