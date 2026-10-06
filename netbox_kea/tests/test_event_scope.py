# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""A plugin transaction and NetBox's events: a unit dispatches after its COMMIT, or nothing when it fails.

On NetBox 4.6.9 and later, a block of ``event_scope.atomic()`` that runs in a tracked request with no open transaction
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
from types import SimpleNamespace
from typing import Any

from core.models import ObjectChange, ObjectType
from dcim.models import MACAddress, Site
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import OperationalError, connection, transaction
from django.db.models.signals import post_save, pre_save
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress, Prefix
from netbox.context import current_request
from netbox.context_managers import event_tracking
from netbox.settings import VERSION

from netbox_kea import event_scope
from netbox_kea.ipam_reconciliation import claim
from netbox_kea.sync import sync_mac_address
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot
from netbox_kea.tests.utils import DISPATCHED_EVENTS, _make_db_server

User: Any = get_user_model()
_RECORDER = "netbox_kea.tests.utils.record_dispatched_events"
_FAILING_PIPELINE = "netbox_kea.tests.test_event_scope.fail_dispatch"
# NetBox 4.6.9 (#22923) made event_tracking nestable. Derived here, not read from event_scope, so a wrong gate fails.
NESTED_TRACKING = tuple(int(part) for part in VERSION.split("-", 1)[0].split(".")[:3]) >= (4, 6, 9)
# 2026-10-06, development host: 10^4 units took 151.0 s; the same saves in one transaction took 127.2 s.
MEASURED_UNIT_SECONDS = 151.0
_SUBNET = {"subnet4": [{"id": 1, "subnet": "10.77.0.0/24"}]}


def fail_dispatch(events: list) -> None:
    """Stand in for an events pipeline that fails, such as a Redis outage while the RQ jobs are queued."""
    raise RuntimeError("The events pipeline is down")


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


def _host(index: int) -> dict:
    return {
        "subnet-id": 1,
        "hw-address": f"02:00:00:00:77:{index:02x}",
        "ip-address": f"10.77.0.{index}",
        "hostname": f"host-{index}",
    }


def _reservations(*hosts: dict) -> list:
    return list(_reservation_snapshot(_SUBNET, 4, list(hosts)).snapshot.records)


@contextmanager
def _receiver(signal, sender, function):
    signal.connect(function, sender=sender, weak=False)
    try:
        yield
    finally:
        signal.disconnect(function, sender=sender)


def _mac_write_fails(hardware: str):
    """Fail each MACAddress write of *hardware* with a database error, before NetBox queues its event."""

    def refuse(sender, instance, **kwargs):
        if str(instance.mac_address).lower() == hardware:
            raise OperationalError("simulated database failure of the MAC address write")

    return _receiver(pre_save, MACAddress, refuse)


class UnitGateTest(SimpleTestCase):
    """Units need the nestable event_tracking, which arrived in NetBox 4.6.9."""

    def test_the_gate_matches_this_netbox_release(self):
        self.assertIs(event_scope._NESTED_TRACKING, NESTED_TRACKING)

    def test_the_gate_opens_at_netbox_4_6_9(self):
        for version, nested in (
            ("4.3.7", False),
            ("4.6.8", False),
            ("4.6.9", True),
            ("4.7.0", True),
            ("4.8.0-dev", True),
        ):
            with self.subTest(version=version), override_settings(RELEASE=SimpleNamespace(version=version)):
                self.assertIs(event_scope._nested_tracking(), nested)


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

    def test_a_refused_reservation_row_dispatches_nothing_while_its_sibling_dispatches(self):
        failed, healthy = _host(11), _host(12)
        server = _make_db_server(dhcp6=False)
        with _mac_write_fails(failed["hw-address"]), event_tracking(self.request):
            result = claim(server, 4, _reservations(failed, healthy), force=False)
        self.assertEqual(result.addresses[failed["ip-address"]].outcome, "error")
        self.assertFalse(IPAddress.objects.filter(address__net_host=failed["ip-address"]).exists())
        sibling = IPAddress.objects.get(address__net_host=healthy["ip-address"])
        self.assertIn((IPAddress, sibling.pk), self.dispatched())
        if NESTED_TRACKING:
            self.assert_every_event_names_a_committed_row()
        else:
            self.assertEqual(sum(model is IPAddress for model, _ in self.dispatched()), 2)

    @unittest.skipUnless(NESTED_TRACKING, "Units need the nestable event_tracking of NetBox 4.6.9")
    def test_a_flush_failure_leaves_the_row_loop_and_keeps_the_row_committed(self):
        from netbox_kea.event_scope import EventDispatchError

        host = _host(21)
        server = _make_db_server(dhcp6=False)
        with override_settings(EVENTS_PIPELINE=[_FAILING_PIPELINE]), event_tracking(self.request):
            with self.assertRaises(RuntimeError) as raised:
                claim(server, 4, _reservations(host), force=False)
        self.assertIsInstance(raised.exception, EventDispatchError)
        self.assertEqual(str(raised.exception.__cause__), "The events pipeline is down")
        self.assertTrue(IPAddress.objects.filter(address__net_host=host["ip-address"]).exists())

    @unittest.skipUnless(NESTED_TRACKING, "Units need the nestable event_tracking of NetBox 4.6.9")
    def test_a_flush_failure_leaves_the_mac_sync(self):
        from netbox_kea.event_scope import EventDispatchError

        with override_settings(EVENTS_PIPELINE=[_FAILING_PIPELINE]), event_tracking(self.request):
            with self.assertRaises(RuntimeError) as raised:
                sync_mac_address("02:00:00:00:77:31", "flush-failure")
        self.assertIsInstance(raised.exception, EventDispatchError)
        self.assertTrue(MACAddress.objects.filter(mac_address="02:00:00:00:77:31").exists())

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
        # Accepted limit with units: the committed write's event is dropped. Without units the caller sends it.
        self.assertEqual(self.dispatched(), [] if NESTED_TRACKING else [(Site, site.pk)])


@override_settings(EVENTS_PIPELINE=[_RECORDER])
class SavepointLimitTest(_Recorded, TestCase):
    """Inside a caller's transaction a plugin block is a plain savepoint. These pin the limits that remain there."""

    def test_a_failed_mac_description_write_queues_no_mac_event(self):
        hardware = "02:00:00:00:77:41"

        def refuse_the_description(sender, instance, **kwargs):
            if instance.description:
                raise OperationalError("simulated database failure of the description write")

        with _receiver(pre_save, MACAddress, refuse_the_description), event_tracking(self.request):
            with transaction.atomic():
                self.assertIsNone(sync_mac_address(hardware, "described"))
        self.assertFalse(MACAddress.objects.filter(mac_address=hardware).exists())
        self.assertEqual(self.dispatched(), [])

    def test_limit_a_database_failure_after_the_ip_write_dispatches_the_reverted_ip(self):
        failed, healthy = _host(51), _host(52)
        server = _make_db_server(dhcp6=False)
        with _mac_write_fails(failed["hw-address"]), event_tracking(self.request), transaction.atomic():
            result = claim(server, 4, _reservations(failed, healthy), force=False)
        self.assertEqual(result.addresses[failed["ip-address"]].outcome, "error")
        self.assertFalse(IPAddress.objects.filter(address__net_host=failed["ip-address"]).exists())
        reverted = [pk for model, pk in self.reverted() if model is IPAddress]
        self.assertEqual(len(reverted), 1, "NetBox now discards a savepoint's events: remove this accepted limit")

    def test_limit_b_a_duplicate_mac_made_after_the_pre_check_dispatches_the_reverted_ip(self):
        host = _host(61)

        def duplicate_the_mac(sender, instance, created, **kwargs):
            if created and str(instance.address).split("/")[0] == host["ip-address"]:
                MACAddress.objects.create(mac_address=host["hw-address"])
                MACAddress.objects.create(mac_address=host["hw-address"])

        server = _make_db_server(dhcp6=False)
        with _receiver(post_save, IPAddress, duplicate_the_mac), event_tracking(self.request), transaction.atomic():
            result = claim(server, 4, _reservations(host), force=False)
        self.assertEqual(result.addresses[host["ip-address"]].outcome, "error")
        self.assertFalse(IPAddress.objects.filter(address__net_host=host["ip-address"]).exists())
        self.assertIn(IPAddress, [model for model, _ in self.reverted()])

    def test_a_persistent_duplicate_mac_refuses_the_row_before_its_ip_write(self):
        host, healthy = _host(71), _host(72)
        MACAddress.objects.create(mac_address=host["hw-address"])
        MACAddress.objects.create(mac_address=host["hw-address"])
        server = _make_db_server(dhcp6=False)
        with event_tracking(self.request), transaction.atomic():
            result = claim(server, 4, _reservations(host, healthy), force=False)
        self.assertEqual(result.addresses[host["ip-address"]].outcome, "error")
        self.assertEqual(
            [model for model, _ in self.dispatched() if model is IPAddress],
            [IPAddress],
            "Only the healthy row's address dispatches",
        )
        self.assert_every_event_names_a_committed_row()

    def test_an_unparseable_reservation_mac_refuses_the_row_before_its_ip_write(self):
        # Kea accepts a hardware address of up to 20 octets; NetBox stores only EUI-48 and EUI-64.
        host = {**_host(81), "hw-address": "02:00:00:00:00:00:00:00:00:51"}
        server = _make_db_server(dhcp6=False)
        with event_tracking(self.request), transaction.atomic():
            result = claim(server, 4, _reservations(host), force=False)
        self.assertEqual(result.addresses[host["ip-address"]].outcome, "error")
        self.assertEqual(self.dispatched(), [])

    def _claim_curated(self, host: dict) -> str:
        server = _make_db_server(dhcp6=False)
        IPAddress.objects.create(address=f"{host['ip-address']}/24", vrf_id=server.sync_vrf_id, description="curated")
        with event_tracking(self.request), transaction.atomic():
            result = claim(server, 4, _reservations(host), force=False)
        return result.addresses[host["ip-address"]].outcome

    def test_a_conflict_with_a_duplicate_mac_stays_a_conflict(self):
        host = _host(91)
        MACAddress.objects.create(mac_address=host["hw-address"])
        MACAddress.objects.create(mac_address=host["hw-address"])
        self.assertEqual(self._claim_curated(host), "conflict")
        self.assertEqual(self.dispatched(), [])

    def test_a_conflict_with_an_unparseable_mac_stays_a_conflict(self):
        host = {**_host(92), "hw-address": "02:00:00:00:00:00:00:00:00:52"}
        self.assertEqual(self._claim_curated(host), "conflict")
        self.assertEqual(self.dispatched(), [])


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

    def test_a_refused_import_receipt_dispatches_no_reverted_server_event(self):
        from netbox_kea.models import Server

        server = _make_db_server(name=f"kea-receipt-{uuid.uuid4()}", sync_dhcp_plugin_enabled=True, dhcp6=False)
        refused = []

        def refuse_the_receipt(sender, instance, update_fields=None, **kwargs):
            if update_fields and "ipam_initial_observations" in update_fields:
                refused.append(instance.pk)
                raise RuntimeError("refused after NetBox queued the event")

        conf = {4: {"subnet4": [{"id": 1, "subnet": "10.88.0.0/24", "pools": [{"pool": "10.88.0.10-10.88.0.99"}]}]}}
        with _receiver(post_save, Server, refuse_the_receipt):
            response = self._post_import(server, conf)
        self.assertEqual((response.status_code, refused), (302, [server.pk]))
        server.refresh_from_db()
        self.assertEqual((server.ipam_initial_observations, server.ipam_first_complete_at), ({}, None))
        subnet = apps.get_model("netbox_dhcp", "Subnet").objects.get(prefix__prefix="10.88.0.0/24")
        self.assertIn((type(subnet), subnet.pk), self.dispatched())
        receipts = [
            event["snapshots"]["postchange"]["ipam_initial_observations"]
            for event in DISPATCHED_EVENTS
            if _key(event) == (Server, server.pk)
        ]
        if NESTED_TRACKING:
            self.assertEqual([receipt for receipt in receipts if receipt], [])
        else:
            self.assertTrue(any(receipts), "NetBox now discards a savepoint's events: remove this accepted limit")

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


@override_settings(EVENTS_PIPELINE=[_RECORDER])
class DhcpImportMacTest(_Recorded, TestCase):
    @classmethod
    def setUpClass(cls):
        if not apps.is_installed("netbox_dhcp"):
            raise unittest.SkipTest("netbox_dhcp not installed")
        super().setUpClass()

    def test_a_persistent_duplicate_mac_inside_an_import_dispatches_only_the_healthy_ip(self):
        from netbox_kea.integrations.dhcp_plugin import import_server_config
        from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config

        config = {"subnet6": [{"id": 1, "subnet": "2001:db8:1::/64"}]}
        hardware = "aa:bb:cc:00:00:01"
        MACAddress.objects.create(mac_address=hardware)
        MACAddress.objects.create(mac_address=hardware)
        failed = {"subnet-id": 1, "hw-address": hardware, "ip-addresses": ["2001:db8:1::10"]}
        healthy = {"subnet-id": 1, "duid": "01:02:03:04", "ip-addresses": ["2001:db8:1::11"]}
        server = _make_db_server(name="duplicate-mac-import", dhcp4=False, dhcp6=True)
        observation = _reservation_snapshot(config, 6, [failed, healthy])
        with event_tracking(self.request):
            summary = import_server_config(server, parse_dhcp_config(config, 6), observation)
        self.assertEqual((summary.errors, summary.reservations_created), (1, 1))
        self.assertFalse(IPAddress.objects.filter(address__net_host="2001:db8:1::10").exists())
        healthy_ip = IPAddress.objects.get(address__net_host="2001:db8:1::11")
        self.assertEqual([pk for model, pk in self.dispatched() if model is IPAddress], [healthy_ip.pk])
        self.assert_every_event_names_a_committed_row()


@override_settings(EVENTS_PIPELINE=["extras.events.process_event_queue"])
@unittest.skipUnless(NESTED_TRACKING, "Units need the nestable event_tracking of NetBox 4.6.9")
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
