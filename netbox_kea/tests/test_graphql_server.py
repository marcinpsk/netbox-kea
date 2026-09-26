# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Server GraphQL type must report every field an operator can set.

``ServerType`` names its fields one by one, so a field added to the model is
absent from GraphQL until someone remembers to list it. The sync fields shipped
that way. The credentials stay out on purpose: GraphQL has no write-only
equivalent of the serializer's ``write_only``, so listing them would publish
them.
"""

import json

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from ipam.models import VRF

from netbox_kea.graphql import ServerType
from netbox_kea.models import Server

from .utils import _make_db_server

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}

#: Never publish a credential over GraphQL. custom_field_data is NetBox's own JSON
#: store, which CustomFieldsMixin exposes as ``custom_fields``.
GRAPHQL_OMISSIONS = {
    "ca_password",
    "dhcp4_password",
    "dhcp6_password",
    "custom_field_data",
}

SYNC_BOOLEANS = (
    "sync_enabled",
    "sync_leases_enabled",
    "sync_reservations_enabled",
    "sync_prefixes_enabled",
    "sync_ip_ranges_enabled",
    "sync_dhcp_plugin_enabled",
)


class TestServerTypeCoversEveryEditableField(SimpleTestCase):
    """A model field ServerType omits is invisible to every GraphQL client."""

    def test_type_reports_every_editable_server_field_but_the_allowed_omissions(self):
        editable = {
            field.name
            for field in Server._meta.get_fields()
            if getattr(field, "editable", False) and not field.auto_created
        }
        exposed = {field.python_name for field in ServerType.__strawberry_definition__.fields}

        self.assertEqual(editable - exposed, GRAPHQL_OMISSIONS)

    def test_no_credential_reaches_graphql(self):
        """The guard above passes just as well if a password is added to both sets."""
        exposed = {field.python_name for field in ServerType.__strawberry_definition__.fields}

        self.assertEqual(exposed & {"ca_password", "dhcp4_password", "dhcp6_password"}, set())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerGraphQLQuery(TestCase):
    """Query the real schema over HTTP, the way a GraphQL client does."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="graphql_user",
            email="graphql@example.com",
            password="graphql_pass",
        )
        self.client.force_login(self.user)
        self.vrf = VRF.objects.create(name="graphql-sync-vrf")
        self.server = _make_db_server(
            name="graphql-sync",
            sync_enabled=False,
            sync_leases_enabled=False,
            sync_reservations_enabled=True,
            sync_prefixes_enabled=False,
            sync_ip_ranges_enabled=True,
            sync_dhcp_plugin_enabled=True,
            persist_config=False,
            sync_vrf=self.vrf,
        )

    def _query(self, document):
        response = self.client.post(
            reverse("graphql"),
            data=json.dumps({"query": document}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertNotIn("errors", payload, payload)
        return payload["data"]

    def test_query_returns_every_sync_field_and_the_vrf(self):
        fields = "\n".join((*SYNC_BOOLEANS, "persist_config"))
        data = self._query(
            f"""
            query {{
              server(id: {self.server.pk}) {{
                name
                {fields}
                sync_vrf {{ id name }}
              }}
            }}
            """
        )

        server = data["server"]
        self.assertEqual(server["name"], "graphql-sync")
        for name in SYNC_BOOLEANS:
            self.assertEqual(server[name], getattr(self.server, name), f"{name} is wrong")
        self.assertFalse(server["persist_config"])
        self.assertEqual(server["sync_vrf"]["name"], "graphql-sync-vrf")

    def test_sync_vrf_is_null_when_unset(self):
        self.server.sync_vrf = None
        self.server.save()

        data = self._query(f"query {{ server(id: {self.server.pk}) {{ sync_vrf {{ id }} }} }}")

        self.assertIsNone(data["server"]["sync_vrf"])
