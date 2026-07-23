# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Standard NetBox view + REST API coverage for the Server model.

These use NetBox's own test mixins (``ViewTestCases`` / ``APIViewTestCases``) to
exercise the standard CRUD, list, bulk, and REST-API behaviour of the ``Server``
model against the real database, forms, and serializers — including the coverage
the hand-rolled view tests do not assert (object-permission enforcement, the
changelog view, bulk import/edit/delete, and the REST API, which had no mixin
coverage at all).

``Server.clean()`` runs a live ``version-get`` connectivity check per enabled
service (see models.py), and NetBox's REST serializer calls ``full_clean()`` on
create/update, so any create/edit/bulk path would otherwise try to reach a real
Kea daemon. Each test activates ``stub_kea`` to answer that check at the HTTP
boundary — the real ``clean()`` runs, only the transport is stubbed.
"""

from __future__ import annotations

from django.test import override_settings
from utilities.testing import APIViewTestCases, ViewTestCases, create_tags

from netbox_kea.models import Server

from .kea_stub import stub_kea
from .utils import _PLUGINS_CONFIG

_VERSION_OK = {"result": 0, "arguments": {"extended": "3.2.0"}}


class _ServerGenericTestMixin:
    """Answer ``Server.clean()``'s live ``version-get`` check at the HTTP boundary.

    ``Server.clean()`` runs a live ``version-get`` per enabled service, and NetBox's
    REST serializer calls ``full_clean()`` on create/update, so every create/edit/bulk
    path would otherwise try to reach a real Kea daemon. ``stub_kea`` answers it at the
    transport boundary — the real ``clean()`` runs, only the HTTP call is stubbed.
    ``setUpTestData`` builds fixtures with ``bulk_create`` (which skips ``Model.clean()``),
    so the stub is only exercised by the paths that genuinely run the check.

    The exact SQL-query-count baselines the list-view tests assert against live in
    ``tests/query_counts.json`` (recorded with ``UPDATE_QUERY_COUNTS=1``); the unit-test
    CI pins the same NetBox version as the devcontainer so those counts stay stable and
    catch N+1 drift.
    """

    def setUp(self):
        super().setUp()
        cm = stub_kea({"version-get": _VERSION_OK})
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class ServerViewTestCase(_ServerGenericTestMixin, ViewTestCases.PrimaryObjectViewTestCase):
    """Standard object views for Server: get/list/create/edit/delete, changelog, bulk import/edit/delete."""

    model = Server

    def _get_base_url(self):
        # Plugin views live under the ``plugins:`` namespace.
        return f"plugins:{self.model._meta.app_label}:{self.model._meta.model_name}_{{}}"

    @classmethod
    def setUpTestData(cls):
        servers = (
            Server(name="kea-1", ca_url="https://kea1.example.com"),
            Server(name="kea-2", ca_url="https://kea2.example.com"),
            Server(name="kea-3", ca_url="https://kea3.example.com"),
        )
        Server.objects.bulk_create(servers)

        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.form_data = {
            "name": "kea-new",
            "ca_url": "https://kea-new.example.com",
            "has_control_agent": True,
            "dhcp4": True,
            "dhcp6": True,
            "ssl_verify": True,
            "sync_enabled": True,
            "sync_leases_enabled": True,
            "sync_reservations_enabled": True,
            "sync_prefixes_enabled": True,
            "sync_ip_ranges_enabled": True,
            "persist_config": True,
            "tags": [t.pk for t in tags],
        }

        cls.csv_data = (
            "name,ca_url,dhcp4,dhcp6,has_control_agent",
            "kea-csv-1,https://kea-csv1.example.com,true,true,true",
            "kea-csv-2,https://kea-csv2.example.com,true,true,true",
            "kea-csv-3,https://kea-csv3.example.com,true,true,true",
        )

        cls.csv_update_data = (
            "id,ca_url",
            f"{servers[0].pk},https://updated1.example.com",
            f"{servers[1].pk},https://updated2.example.com",
            f"{servers[2].pk},https://updated3.example.com",
        )

        cls.bulk_edit_data = {
            "has_control_agent": False,
        }


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class ServerAPITestCase(
    _ServerGenericTestMixin,
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.CreateObjectViewTestCase,
    APIViewTestCases.UpdateObjectViewTestCase,
    APIViewTestCases.DeleteObjectViewTestCase,
):
    """Standard REST API for Server: detail/list/create/update/delete.

    GraphQL is intentionally excluded (the plugin ships no GraphQL schema), so the
    individual CRUD mixins are composed rather than the bundled ``APIViewTestCase``.
    """

    model = Server
    # Plugin API views live under the ``plugins-api:`` namespace (→ ``plugins-api:netbox_kea-api``).
    view_namespace = "plugins-api:netbox_kea"
    brief_fields = ["ca_url", "id", "name", "url"]
    bulk_update_data = {"ssl_verify": False}

    @classmethod
    def setUpTestData(cls):
        servers = (
            Server(name="kea-api-1", ca_url="https://kea-api1.example.com"),
            Server(name="kea-api-2", ca_url="https://kea-api2.example.com"),
            Server(name="kea-api-3", ca_url="https://kea-api3.example.com"),
        )
        Server.objects.bulk_create(servers)

        cls.create_data = [
            {"name": "kea-api-4", "ca_url": "https://kea-api4.example.com"},
            {"name": "kea-api-5", "ca_url": "https://kea-api5.example.com"},
            {"name": "kea-api-6", "ca_url": "https://kea-api6.example.com"},
        ]
