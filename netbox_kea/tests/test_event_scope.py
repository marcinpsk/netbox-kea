# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""A plugin transaction and NetBox's events: a unit dispatches after its COMMIT, or nothing when it fails.

On NetBox 4.7 and later, a block of ``event_scope.atomic()`` that runs in a tracked request with no open transaction
is a unit with its own nested ``event_tracking``. Inside a transaction, and on NetBox releases whose
``event_tracking`` cannot nest, it is a plain transaction or savepoint, and the limits that NetBox's design leaves
are pinned here. The module imports no fixture of netbox-branching, so the NetBox 4.3 CI leg can collect it.
"""

from __future__ import annotations

import os
import time
import unittest
import uuid
from contextlib import contextmanager, nullcontext, suppress
from typing import Any

from core.models import ObjectChange, ObjectType
from dcim.models import Site
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connection, transaction
from django.db.models.signals import post_save
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from ipam.models import Prefix
from netbox.context import current_request
from netbox.context_managers import event_tracking
from netbox.settings import VERSION

from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.utils import DISPATCHED_EVENTS, _make_db_server

User: Any = get_user_model()
_RECORDER = "netbox_kea.tests.utils.record_dispatched_events"
# NetBox 4.7 made event_tracking nestable: it restores the caller's context and dispatches nothing on an exception.
NESTED_TRACKING = tuple(int(part) for part in VERSION.split("-", 1)[0].split(".")[:2]) >= (4, 7)
# 2026-10-06, development host: 10^4 units took 151.0 s; the same saves in one transaction took 127.2 s.
MEASURED_UNIT_SECONDS = 151.0


def _request(user) -> Any:
    request: Any = RequestFactory().post("/plugins/kea/")
    request.user, request.id = user, uuid.uuid4()
    return request


def _clear_object_type_cache() -> None:
    """NetBox 4.3: a TransactionTestCase flush recreates the content types, and ObjectType's manager keeps old ids."""
    if hasattr(ObjectType.objects, "clear_cache"):
        ObjectType.objects.clear_cache()


def _key(event: dict) -> tuple[Any, Any]:
    return event["object_type"].model_class(), event["object_id"]


@contextmanager
def _receiver(signal, sender, function):
    signal.connect(function, sender=sender, weak=False)
    try:
        yield
    finally:
        signal.disconnect(function, sender=sender)


class _Recorded(SimpleTestCase):
    """Record what NetBox dispatches, and check that every dispatched event names a row that exists."""

    def setUp(self):
        super().setUp()
        _clear_object_type_cache()
        self.user = User.objects.create_user(f"event-scope-{uuid.uuid4()}")
        self.request = _request(self.user)
        DISPATCHED_EVENTS.clear()
        self.addCleanup(DISPATCHED_EVENTS.clear)

    def dispatched(self) -> list[tuple[Any, Any]]:
        return [_key(event) for event in DISPATCHED_EVENTS]

    def reverted(self) -> list[tuple[Any, Any]]:
        """Return each dispatched event whose row does not exist."""
        return [(model, pk) for model, pk in self.dispatched() if not model.objects.filter(pk=pk).exists()]

    def assert_every_event_names_a_committed_row(self) -> None:
        self.assertEqual(self.reverted(), [], "An event names a row that never committed")


@override_settings(EVENTS_PIPELINE=[_RECORDER])
class UnitEventTest(_Recorded, TransactionTestCase):
    """Top-level plugin transactions in a tracked request, as the IPAM job and the per-row views run them."""

    def test_a_unit_dispatches_after_its_commit_and_a_failed_unit_dispatches_nothing(self):
        from netbox_kea import event_scope

        with event_tracking(self.request):
            with event_scope.atomic():
                kept = Site.objects.create(name="unit-kept", slug="unit-kept")
            self.assertEqual(self.dispatched(), [(Site, kept.pk)] if NESTED_TRACKING else [])
            with suppress(LookupError), event_scope.atomic():
                failed = Site.objects.create(name="unit-failed", slug="unit-failed")
                raise LookupError("a refusal after the write")
        self.assertFalse(Site.objects.filter(pk=failed.pk).exists())
        if NESTED_TRACKING:
            self.assertEqual(self.dispatched(), [(Site, kept.pk)])
        else:
            # Accepted limit: without nestable tracking, the request flush also sends the reverted write's event.
            self.assertEqual(self.dispatched(), [(Site, kept.pk), (Site, failed.pk)])

    def test_an_open_transaction_selects_plain_mode(self):
        from netbox_kea import event_scope

        with event_tracking(self.request):
            with transaction.atomic():
                with event_scope.atomic():
                    site = Site.objects.create(name="caller-owned", slug="caller-owned")
                self.assertEqual(self.dispatched(), [])
        self.assertEqual(self.dispatched(), [(Site, site.pk)])

    def test_an_unopened_alias_without_autocommit_selects_plain_mode_without_a_connection(self):
        from netbox_kea import event_scope

        connection.close()
        settings = connection.settings_dict
        settings["AUTOCOMMIT"] = False
        try:
            self.assertTrue(event_scope.in_transaction())
            self.assertIsNone(connection.connection)
            with event_tracking(self.request):
                with event_scope.atomic():
                    Site.objects.create(name="no-autocommit", slug="no-autocommit")
                # Django made a savepoint, so nothing committed and no unit dispatched.
                self.assertEqual(self.dispatched(), [])
                connection.rollback()
                DISPATCHED_EVENTS.clear()
        finally:
            settings["AUTOCOMMIT"] = True
            connection.close()
        self.assertFalse(Site.objects.filter(slug="no-autocommit").exists())

    def test_a_unit_keeps_the_callers_request_queue_and_change_logging(self):
        from netbox_kea import event_scope

        with event_tracking(self.request):
            caller = Site.objects.create(name="caller", slug="caller")
            with event_scope.atomic():
                inner = Site.objects.create(name="inner", slug="inner")
            self.assertIs(current_request.get(), self.request)
            later = Site.objects.create(name="later", slug="later")
        self.assertEqual(
            set(ObjectChange.objects.filter(request_id=self.request.id).values_list("changed_object_id", flat=True)),
            {caller.pk, inner.pk, later.pk},
        )
        self.assertEqual(sorted(pk for _, pk in self.dispatched()), sorted([caller.pk, inner.pk, later.pk]))

    def test_units_dispatch_per_unit_and_before_the_callers_events(self):
        from netbox_kea import event_scope

        with event_tracking(self.request):
            caller = Site.objects.create(name="first", slug="first")
            with event_scope.atomic():
                site = Site.objects.create(name="twice", slug="twice")
            with event_scope.atomic():
                site.snapshot()
                site.description = "touched by a second unit"
                site.save()
        events = [(_key(event), event["event_type"]) for event in DISPATCHED_EVENTS]
        if NESTED_TRACKING:
            # Accepted consequence: no coalescing across units, and the caller's own events dispatch last.
            expected = [((Site, site.pk), "object_created"), ((Site, site.pk), "object_updated")]
            self.assertEqual(events, [*expected, ((Site, caller.pk), "object_created")])
        else:
            self.assertEqual(events, [((Site, caller.pk), "object_created"), ((Site, site.pk), "object_created")])

    def test_an_on_commit_hook_that_raises_drops_its_units_events(self):
        from netbox_kea import event_scope

        def fail():
            raise LookupError("on_commit hook failure")

        with event_tracking(self.request):
            with self.assertRaises(LookupError), event_scope.atomic():
                site = Site.objects.create(name="hooked", slug="hooked")
                transaction.on_commit(fail)
        self.assertTrue(Site.objects.filter(pk=site.pk).exists())
        # Accepted limit on NetBox 4.7: the committed write's event is dropped. Without units the caller sends it.
        self.assertEqual(self.dispatched(), [] if NESTED_TRACKING else [(Site, site.pk)])


@override_settings(EVENTS_PIPELINE=[_RECORDER])
class DhcpImportEventTest(_Recorded, TransactionTestCase):
    """Each family import is a unit of its own, and an import inside a caller's transaction is plain."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed("netbox_dhcp"):
            raise unittest.SkipTest("netbox_dhcp not installed")
        super().setUpClass()

    def _post_import(self, server, conf):
        from netbox_kea.tests.test_views_dhcp_plugin import _sync_responses

        self.client.force_login(User.objects.create_superuser(f"importer-{uuid.uuid4()}"))
        with stub_kea(_sync_responses(conf)):
            return self.client.post(reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[server.pk]))

    def test_a_failed_family_import_dispatches_nothing_for_that_family(self):
        server = _make_db_server(name=f"kea-families-{uuid.uuid4()}", sync_dhcp_plugin_enabled=True)
        conf = {
            4: {"subnet4": [{"id": 1, "subnet": "10.78.0.0/24"}]},
            6: {"subnet6": [{"id": 1, "subnet": "2001:db8:78::/64"}]},
        }

        def fail_the_v6_prefix(sender, instance, created, **kwargs):
            if str(instance.prefix) == "2001:db8:78::/64":
                raise RuntimeError("the DHCPv6 import fails after NetBox queued the Prefix event")

        with _receiver(post_save, Prefix, fail_the_v6_prefix):
            response = self._post_import(server, conf)
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "An internal error occurred during the DHCP-plugin import.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )
        self.assertFalse(Prefix.objects.filter(prefix="2001:db8:78::/64").exists())
        subnet = apps.get_model("netbox_dhcp", "Subnet").objects.get(prefix__prefix="10.78.0.0/24")
        self.assertIn((type(subnet), subnet.pk), self.dispatched())
        if NESTED_TRACKING:
            self.assert_every_event_names_a_committed_row()
        else:
            self.assertIn(Prefix, [model for model, _ in self.reverted()])

    def test_an_import_inside_a_caller_transaction_is_plain_mode(self):
        from netbox_dhcp.models import DHCPServer

        from netbox_kea.integrations.dhcp_plugin import import_server_config
        from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config

        server = _make_db_server(name=f"kea-nested-{uuid.uuid4()}", dhcp6=False)
        intent = parse_dhcp_config({"subnet4": [{"id": 9, "subnet": "198.19.0.0/24"}]}, 4)
        reverted = []

        def fail_after_the_event_is_queued(sender, instance, created, **kwargs):
            reverted.append(instance.pk)
            raise RuntimeError("nested import failure")

        with event_tracking(self.request), transaction.atomic():
            caller = Site.objects.create(name="nested-import-caller", slug="nested-import-caller")
            with _receiver(post_save, DHCPServer, fail_after_the_event_is_queued):
                with self.assertRaisesMessage(RuntimeError, "nested import failure"):
                    import_server_config(server, intent, None)
            self.assertEqual(self.dispatched(), [])
            summary = import_server_config(server, intent, None)
            self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertIn((Site, caller.pk), self.dispatched())
        self.assertFalse(DHCPServer.objects.filter(pk=reverted[0]).exists())
        # Accepted limit: the caller's transaction owns the events, so the reverted DHCP server's event dispatches.
        self.assertIn((DHCPServer, reverted[0]), self.dispatched())


@override_settings(EVENTS_PIPELINE=["extras.events.process_event_queue"])
@unittest.skipUnless(NESTED_TRACKING, "Units need the nestable event_tracking of NetBox 4.7")
class UnitCostTest(TransactionTestCase):
    """Each unit flush costs one EventRule lookup per distinct event type and object type, so cost is linear."""

    def _units(self, count: int, *, units: bool = True) -> tuple[int, float]:
        """Save one Site *count* times, each save in its own unit; return the EventRule lookups and the seconds."""
        from netbox_kea import event_scope

        lookups = []

        def count_lookups(execute, sql, params, many, context):
            if 'FROM "extras_eventrule"' in sql:
                lookups.append(sql)
            return execute(sql, params, many, context)

        _clear_object_type_cache()
        request = _request(User.objects.create_user(f"unit-cost-{uuid.uuid4()}"))
        site = Site.objects.create(name=f"unit-cost-{uuid.uuid4()}", slug=f"unit-cost-{uuid.uuid4()}")
        with connection.execute_wrapper(count_lookups), event_tracking(request):
            started = time.perf_counter()
            with nullcontext() if units else transaction.atomic():
                for index in range(count):
                    with event_scope.atomic():
                        site.snapshot()
                        site.description = f"unit {index}"
                        site.save()
            elapsed = time.perf_counter() - started
        return len(lookups), elapsed

    def test_each_unit_flush_costs_one_event_rule_lookup(self):
        self.assertEqual(self._units(200)[0], 200)
        self.assertEqual(self._units(200, units=False)[0], 1)

    @unittest.skipUnless(os.environ.get("NETBOX_KEA_BENCHMARK") == "1", "Set NETBOX_KEA_BENCHMARK=1 to measure")
    def test_ten_thousand_units(self):
        lookups, elapsed = self._units(10_000)
        _, plain = self._units(10_000, units=False)
        print(f"10^4 units: {lookups} EventRule lookups, {elapsed:.1f} s; one transaction: {plain:.1f} s")
        self.assertEqual(lookups, 10_000)
        self.assertLess(elapsed, 2 * MEASURED_UNIT_SECONDS)
