# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Server submissions validate changed connections without blocking metadata edits."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import requests
from core.models import Job, ObjectChange, ObjectType
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from extras.models import Tag
from ipam.models import VRF
from rest_framework.test import APIClient
from users.models import ObjectPermission

from netbox_kea.models import Server

from .kea_stub import queued, stub_kea
from .utils import _PLUGINS_CONFIG, DISPATCHED_EVENTS, User, _make_db_server, _ViewTestBase

_VERSION_OK = {"result": 0, "arguments": {"extended": "3.2.0"}}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class ServerLocalValidationTest(TestCase):
    def test_full_clean_never_contacts_kea_for_new_or_existing_servers(self):
        servers = (
            Server(name="new-local", ca_url="https://new.example.invalid"),
            _make_db_server(name="existing-local", ca_url="https://existing.example.invalid"),
        )
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            for server in servers:
                with self.subTest(existing=server.pk is not None):
                    server.full_clean()
            self.assertEqual(kea.requests, [])

    def test_invalid_local_configuration_sends_no_http(self):
        cases = (
            ({"dhcp4": False, "dhcp6": False}, "dhcp6"),
            ({"client_cert_path": "/missing-cert.pem"}, "client_cert_path"),
            ({"client_key_path": "/missing-key.pem"}, "client_cert_path"),
            ({"client_cert_path": "/missing-cert.pem", "client_key_path": "/missing-key.pem"}, "client_cert_path"),
            ({"ca_file_path": "/ca.pem", "ssl_verify": False}, "ca_file_path"),
        )
        with stub_kea({}) as kea:
            for values, field in cases:
                with self.subTest(field=field, values=values), self.assertRaises(ValidationError) as error:
                    Server(name="invalid-local", ca_url="https://local.example.invalid", **values).full_clean()
                self.assertIn(field, error.exception.message_dict)
            self.assertEqual(kea.requests, [])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class ServerSubmissionConnectivityTest(_ViewTestBase):
    @staticmethod
    def _payload(**values):
        return {
            "name": "submitted-server",
            "ca_url": "https://shared.example.invalid",
            "dhcp4": True,
            "dhcp6": False,
            "ssl_verify": True,
            "has_control_agent": True,
            **values,
        }

    def test_metadata_edit_succeeds_while_kea_is_unreachable(self):
        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        payload = {
            "name": "renamed-server",
            "ca_url": self.server.ca_url,
            "dhcp4": True,
            "dhcp6": True,
            "ssl_verify": True,
            "has_control_agent": True,
            "sync_dhcp_plugin_enabled": True,
        }
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.requests, [])
        self.server.refresh_from_db()
        self.assertEqual(self.server.name, "renamed-server")
        self.assertTrue(self.server.sync_dhcp_plugin_enabled)

    def test_blank_password_edit_keeps_stored_passwords(self):
        passwords = {"ca_password": "ca-secret", "dhcp4_password": "v4-secret", "dhcp6_password": "v6-secret"}
        Server.objects.filter(pk=self.server.pk).update(**passwords)
        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        payload = self._payload(name="renamed-server", ca_url=self.server.ca_url, dhcp6=True)
        payload.update(dict.fromkeys(passwords, ""))
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.requests, [])
        self.server.refresh_from_db()
        self.assertEqual(self.server.name, "renamed-server")
        self.assertEqual({name: getattr(self.server, name) for name in passwords}, passwords)

        with stub_kea({"version-get": _VERSION_OK}) as kea:
            response = self.client.post(url, {**payload, "dhcp4_password": "new-secret"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(kea.requests), 2)
        self.server.refresh_from_db()
        self.assertEqual(self.server.dhcp4_password, "new-secret")
        self.assertEqual(self.server.ca_password, "ca-secret")

    def test_create_with_blank_passwords_stores_blank_passwords(self):
        payload = self._payload(ca_password="", dhcp4_password="", dhcp6_password="")
        with stub_kea({"version-get": _VERSION_OK}):
            response = self.client.post(reverse("plugins:netbox_kea:server_add"), payload)
        self.assertEqual(response.status_code, 302)
        server = Server.objects.get(name="submitted-server")
        self.assertEqual((server.ca_password, server.dhcp4_password, server.dhcp6_password), ("", "", ""))

    def test_create_preserves_family_errors_and_exception_routing(self):
        cases = (
            (requests.ConnectionError("private diagnostic"), "Unable to reach"),
            (requests.Timeout("private diagnostic"), "Unable to reach"),
            (requests.exceptions.SSLError("private diagnostic"), "Unable to reach"),
            (ValueError("private diagnostic"), "Unable to reach"),
            ({"result": 1, "text": "private diagnostic"}, "Unable to reach"),
            (requests.exceptions.JSONDecodeError("private diagnostic", "", 0), "An internal error occurred."),
        )
        url = reverse("plugins:netbox_kea:server_add")
        for family in (4, 6):
            for reply, message in cases:
                with self.subTest(family=family, reply=type(reply).__name__), stub_kea({"version-get": reply}) as kea:
                    response = self.client.post(url, self._payload(dhcp4=family == 4, dhcp6=family == 6))
                    self.assertEqual(response.status_code, 200)
                    errors = response.context["form"].errors[f"dhcp{family}"]
                    self.assertIn(message, errors[0])
                    self.assertNotIn("private diagnostic", errors[0])
                    self.assertEqual(len(kea.requests), 1)
        self.assertFalse(Server.objects.filter(name="submitted-server").exists())

    def test_create_with_a_missing_ca_bundle_reports_a_field_error(self):
        payload = self._payload(ca_url="https://127.0.0.1:9/", ca_file_path="/nonexistent/kea-ca.pem")
        # Requests prefers these variables over the session CA bundle.
        with patch.dict(os.environ), self.assertLogs("netbox_kea.server_connection", "ERROR") as logs:
            os.environ.pop("REQUESTS_CA_BUNDLE", None)
            os.environ.pop("CURL_CA_BUNDLE", None)
            response = self.client.post(reverse("plugins:netbox_kea:server_add"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["dhcp4"], ["Unable to reach the Kea DHCPv4 service."])
        self.assertIn("suitable TLS CA certificate bundle", "\n".join(logs.output))
        self.assertFalse(Server.objects.filter(name="submitted-server").exists())

    def test_create_preserves_enabled_families_and_direct_or_control_agent_routing(self):
        url = reverse("plugins:netbox_kea:server_add")
        for dhcp4, dhcp6 in ((True, False), (False, True), (True, True)):
            for control_agent in (True, False):
                with self.subTest(dhcp4=dhcp4, dhcp6=dhcp6, control_agent=control_agent):
                    name = f"submitted-{dhcp4}-{dhcp6}-{control_agent}"
                    payload = self._payload(
                        name=name,
                        dhcp4=dhcp4,
                        dhcp6=dhcp6,
                        has_control_agent=control_agent,
                        dhcp4_url="https://v4.example.invalid",
                        dhcp6_url="https://v6.example.invalid",
                    )
                    with stub_kea({"version-get": _VERSION_OK}) as kea:
                        response = self.client.post(url, payload)
                    self.assertEqual(response.status_code, 302)
                    families = ([6] if dhcp6 else []) + ([4] if dhcp4 else [])
                    self.assertEqual(kea.urls(), [f"https://v{family}.example.invalid" for family in families])
                    self.assertEqual(
                        kea.requests,
                        [
                            {"command": "version-get", **({"service": [f"dhcp{family}"]} if control_agent else {})}
                            for family in families
                        ],
                    )

    def test_connection_edit_rejects_an_unreachable_service(self):
        url = reverse("plugins:netbox_kea:server_edit", args=[self.server.pk])
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            response = self.client.post(url, self._payload(name=self.server.name, dhcp6=True))
        self.assertEqual(response.status_code, 200)
        self.assertIn("dhcp6", response.context["form"].errors)
        self.assertEqual(len(kea.requests), 1)
        self.server.refresh_from_db()
        self.assertEqual(self.server.ca_url, "https://kea.example.com")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class ServerAPIConnectivityTest(_ViewTestBase):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.detail = reverse("plugins-api:netbox_kea-api:server-detail", args=[self.server.pk])
        self.collection = reverse("plugins-api:netbox_kea-api:server-list")

    def test_create_rejects_an_unreachable_enabled_service(self):
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            response = self.api.post(
                self.collection,
                {"name": "api-new", "ca_url": "https://new.example.invalid", "dhcp4": True, "dhcp6": False},
                format="json",
            )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["dhcp4"], ["Unable to reach the Kea DHCPv4 service."])
        self.assertEqual(len(kea.requests), 1)
        self.assertFalse(Server.objects.filter(name="api-new").exists())

    def test_patch_and_put_metadata_with_unchanged_connection_values_send_no_http(self):
        vrf = VRF.objects.create(name="metadata-vrf")
        tag = Tag.objects.create(name="Metadata", slug="metadata")
        for method in ("patch", "put"):
            with self.subTest(method=method):
                payload = {
                    "name": f"metadata-{method}",
                    "ca_url": self.server.ca_url,
                    "ca_username": None,
                    "dhcp4": True,
                    "dhcp6": True,
                    "ssl_verify": True,
                    "has_control_agent": True,
                    "sync_enabled": False,
                    "sync_vrf": vrf.pk,
                    "tags": [{"id": tag.pk}],
                }
                with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
                    response = getattr(self.api, method)(self.detail, payload, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(kea.requests, [])
                self.assertEqual(response.data["ca_username"], "")
                self.assertEqual(response.data["sync_vrf"]["id"], vrf.pk)
                self.assertEqual(response.data["tags"][0]["id"], tag.pk)
                self.server.refresh_from_db()
                self.assertEqual(self.server.name, f"metadata-{method}")
                self.assertFalse(self.server.sync_enabled)

    def test_patch_and_put_connection_edits_reject_unreachable_services(self):
        for method in ("patch", "put"):
            with self.subTest(method=method), stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
                response = getattr(self.api, method)(
                    self.detail,
                    {"name": self.server.name, "ca_url": "https://changed.example.invalid"},
                    format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["dhcp6"], ["Unable to reach the Kea DHCPv6 service."])
                self.assertEqual(len(kea.requests), 1)
                self.server.refresh_from_db()
                self.assertEqual(self.server.ca_url, "https://kea.example.com")

    def test_connection_fields_trigger_probes_of_currently_enabled_services(self):
        self.server.dhcp6 = False
        self.server.save()
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            cert.write_text("")
            key.write_text("")
            self.server.client_cert_path = str(cert)
            self.server.client_key_path = str(key)
            self.server.save()
            other_cert, other_key = Path(directory) / "other-cert.pem", Path(directory) / "other-key.pem"
            other_cert.write_text("")
            other_key.write_text("")
            changes = (
                {"ca_url": "https://changed.example.invalid"},
                {"dhcp4_url": "https://v4.example.invalid"},
                {"dhcp6_url": "https://unused.example.invalid"},
                {"ca_username": "placeholder"},
                {"ca_password": "placeholder"},
                {"dhcp4_username": "placeholder"},
                {"dhcp4_password": "placeholder"},
                {"dhcp6_username": "placeholder"},
                {"dhcp6_password": "placeholder"},
                {"ssl_verify": False},
                {"ca_file_path": str(cert)},
                {"client_cert_path": str(other_cert)},
                {"client_key_path": str(other_key)},
                {"has_control_agent": False},
                {"dhcp6": True},
            )
            for payload in changes:
                with (
                    self.subTest(payload=payload),
                    stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea,
                ):
                    response = self.api.patch(self.detail, payload, format="json")
                    family = 6 if payload.get("dhcp6") else 4
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertIn(f"dhcp{family}", response.data)
                    self.assertEqual(len(kea.requests), 1)

    def test_create_keeps_native_related_validation_and_connection_defaults(self):
        vrf = VRF.objects.create(name="create-vrf")
        tag = Tag.objects.create(name="Created", slug="created")
        payload = {
            "name": "api-with-related",
            "ca_url": "https://related.example.invalid",
            "sync_vrf": {"id": vrf.pk},
            "tags": [{"id": tag.pk}],
        }
        with stub_kea({"version-get": _VERSION_OK}) as kea:
            response = self.api.post(self.collection, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            kea.requests,
            [
                {"command": "version-get", "service": ["dhcp6"]},
                {"command": "version-get", "service": ["dhcp4"]},
            ],
        )
        self.assertEqual(response.data["sync_vrf"]["id"], vrf.pk)
        self.assertEqual(response.data["tags"][0]["id"], tag.pk)
        with stub_kea({}) as kea:
            response = self.api.post(
                self.collection, {**payload, "name": "invalid-related", "tags": [{"id": 999999}]}, format="json"
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.requests, [])


@override_settings(
    PLUGINS_CONFIG=_PLUGINS_CONFIG,
    EVENTS_PIPELINE=["netbox_kea.tests.utils.record_dispatched_events"],
)
class ServerBulkConnectivityTest(_ViewTestBase):
    def test_only_selected_permitted_servers_are_updated_or_probed(self):
        self.server.ca_url = "https://selected.example.invalid"
        self.server.save()
        denied = _make_db_server(name="denied-server", ca_url="https://denied.example.invalid", dhcp4=False)
        unselected = _make_db_server(name="unselected-server", ca_url="https://unselected.example.invalid")
        user = User.objects.create_user(username="restricted-operator")
        permission = ObjectPermission.objects.create(
            name="DHCPv4 selected Servers",
            actions=["view", "change"],
            constraints={"dhcp4": True},
        )
        permission.object_types.add(ObjectType.objects.get_for_model(Server))
        permission.users.add(user)
        self.client.force_login(user)
        with stub_kea({"version-get": _VERSION_OK}) as kea:
            response = self.client.post(
                reverse("plugins:netbox_kea:server_bulk_edit"),
                {
                    "pk": [self.server.pk, denied.pk],
                    "has_control_agent": "false",
                    "_apply": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(kea.requests, [])
        self.server.refresh_from_db()
        self.assertTrue(self.server.has_control_agent)
        with stub_kea({"version-get": _VERSION_OK}) as kea:
            response = self.client.post(
                reverse("plugins:netbox_kea:server_bulk_edit"),
                {"pk": [self.server.pk], "has_control_agent": "false", "_apply": True},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.urls(), ["https://selected.example.invalid", "https://selected.example.invalid"])
        self.server.refresh_from_db()
        denied.refresh_from_db()
        unselected.refresh_from_db()
        self.assertFalse(self.server.has_control_agent)
        self.assertTrue(denied.has_control_agent)
        self.assertTrue(unselected.has_control_agent)

    def test_tag_changes_and_explicit_unchanged_connection_values_send_no_http(self):
        tag = Tag.objects.create(name="Bulk metadata", slug="bulk-metadata")
        url = reverse("plugins:netbox_kea:server_bulk_edit")
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            response = self.client.post(
                url,
                {
                    "pk": [self.server.pk],
                    "add_tags": [tag.pk],
                    "dhcp4": "true",
                    "dhcp6": "true",
                    "has_control_agent": "true",
                    "_apply": True,
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.requests, [])
        self.assertEqual(list(self.server.tags.all()), [tag])

    def test_candidate_outside_permission_constraints_is_rolled_back_before_any_probe(self):
        user = User.objects.create_user(username="constrained-operator")
        permission = ObjectPermission.objects.create(
            name="DHCPv4 Servers",
            actions=["view", "change"],
            constraints={"dhcp4": True},
        )
        permission.object_types.add(ObjectType.objects.get_for_model(Server))
        permission.users.add(user)
        self.client.force_login(user)
        with stub_kea({}) as kea:
            response = self.client.post(
                reverse("plugins:netbox_kea:server_bulk_edit"),
                {
                    "pk": [self.server.pk],
                    "dhcp4": "false",
                    "_apply": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertEqual(kea.requests, [])
        self.server.refresh_from_db()
        self.assertTrue(self.server.dhcp4)

    def test_second_server_failure_rolls_back_the_batch_and_dispatches_no_events(self):
        second = _make_db_server(name="second-server")
        tag = Tag.objects.create(name="Connection change", slug="connection-change")
        payload = {"pk": [self.server.pk, second.pk], "dhcp4": "false", "add_tags": [tag.pk], "_apply": True}
        url = reverse("plugins:netbox_kea:server_bulk_edit")
        changes_before = ObjectChange.objects.count()
        jobs_before = Job.objects.count()
        DISPATCHED_EVENTS.clear()
        with stub_kea({"version-get": queued(_VERSION_OK, requests.ConnectionError("Unavailable"))}) as kea:
            response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(kea.requests), 2)
        for server in (self.server, second):
            server.refresh_from_db()
            self.assertTrue(server.dhcp4)
            self.assertFalse(server.tags.exists())
        self.assertEqual(ObjectChange.objects.count(), changes_before)
        self.assertEqual(Job.objects.count(), jobs_before)
        self.assertEqual(DISPATCHED_EVENTS, [])

        with stub_kea({"version-get": _VERSION_OK}) as kea:
            response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(kea.requests), 2)
        self.assertGreater(ObjectChange.objects.count(), changes_before)
        self.assertEqual({event["object_id"] for event in DISPATCHED_EVENTS}, {self.server.pk, second.pk})
        for server in (self.server, second):
            server.refresh_from_db()
            self.assertFalse(server.dhcp4)
            self.assertEqual(list(server.tags.all()), [tag])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class ServerImportConnectivityTest(_ViewTestBase):
    def test_csv_metadata_and_unchanged_connection_values_send_no_http(self):
        url = reverse("plugins:netbox_kea:server_bulk_import")
        csv = f"id,name,ca_url,sync_enabled\r\n{self.server.pk},csv-renamed,{self.server.ca_url},false\r\n"
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            response = self.client.post(url, {"data": csv, "format": "csv", "csv_delimiter": ","})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.requests, [])
        self.server.refresh_from_db()
        self.assertEqual(self.server.name, "csv-renamed")
        self.assertFalse(self.server.sync_enabled)
        self.assertTrue(self.server.dhcp4)
        self.assertTrue(self.server.dhcp6)

    def test_csv_creation_keeps_omitted_connection_defaults_and_checks_both_families(self):
        csv = "name,ca_url\r\ncsv-created,https://csv.example.invalid\r\n"
        with stub_kea({"version-get": _VERSION_OK}) as kea:
            response = self.client.post(
                reverse("plugins:netbox_kea:server_bulk_import"),
                {
                    "data": csv,
                    "format": "csv",
                    "csv_delimiter": ",",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            kea.requests,
            [
                {"command": "version-get", "service": ["dhcp6"]},
                {"command": "version-get", "service": ["dhcp4"]},
            ],
        )
        created = Server.objects.get(name="csv-created")
        self.assertTrue(created.dhcp4)
        self.assertTrue(created.dhcp6)
        self.assertTrue(created.ssl_verify)
        self.assertTrue(created.has_control_agent)

    def test_csv_creation_rejects_unreachable_enabled_service_with_present_field_error(self):
        csv = "name,ca_url,dhcp4,dhcp6\r\ncsv-unreachable,https://csv.example.invalid,true,false\r\n"
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            response = self.client.post(
                reverse("plugins:netbox_kea:server_bulk_import"),
                {
                    "data": csv,
                    "format": "csv",
                    "csv_delimiter": ",",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dhcp4")
        self.assertContains(response, "Unable to reach the Kea DHCPv4 service.")
        self.assertEqual(len(kea.requests), 1)
        self.assertFalse(Server.objects.filter(name="csv-unreachable").exists())

    def test_csv_connection_update_reports_an_omitted_family_as_a_row_error(self):
        self.server.dhcp6 = False
        self.server.save()
        url = reverse("plugins:netbox_kea:server_bulk_import")
        csv = f"id,ca_url\r\n{self.server.pk},https://changed.example.invalid\r\n"
        with stub_kea({"version-get": requests.ConnectionError("Unavailable")}) as kea:
            response = self.client.post(url, {"data": csv, "format": "csv", "csv_delimiter": ","})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to reach the Kea DHCPv4 service.")
        self.assertContains(response, "DHCPv4:")
        self.assertEqual(len(kea.requests), 1)
        self.server.refresh_from_db()
        self.assertEqual(self.server.ca_url, "https://kea.example.com")

    def test_csv_malformed_json_error_keeps_the_omitted_family_label(self):
        self.server.dhcp6 = False
        self.server.save()
        csv = f"id,ca_url\r\n{self.server.pk},https://changed.example.invalid\r\n"
        with stub_kea({"version-get": requests.exceptions.JSONDecodeError("private diagnostic", "", 0)}) as kea:
            response = self.client.post(
                reverse("plugins:netbox_kea:server_bulk_import"),
                {
                    "data": csv,
                    "format": "csv",
                    "csv_delimiter": ",",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DHCPv4: An internal error occurred.")
        self.assertNotContains(response, "private diagnostic")
        self.assertEqual(len(kea.requests), 1)
        self.server.refresh_from_db()
        self.assertEqual(self.server.ca_url, "https://kea.example.com")
