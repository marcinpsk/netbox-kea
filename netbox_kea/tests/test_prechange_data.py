# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Plugin writes preserve the old values in NetBox change records."""

import copy
import unittest
import uuid

from core.models import ObjectChange
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase, override_settings
from ipam.models import VRF, IPAddress, IPRange, Prefix
from netbox.context_managers import event_tracking

from netbox_kea.ipam_reconciliation import LeasePhase, PoolPhase, SubnetPhase, read_catalogue, reconcile
from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config
from netbox_kea.tests.kea_stub import _catalogue_responses_for_subnets, stub_kea
from netbox_kea.tests.test_integration_dhcp_plugin import _reservation_snapshot
from netbox_kea.tests.test_ipam_reconciliation import _kea, _lease, _reconcile, _reservation, _run_job
from netbox_kea.tests.utils import _make_db_server, plugins_config


@override_settings(PLUGINS_CONFIG=plugins_config())
class IPAMChangeRecordTest(TestCase):
    def setUp(self):
        self.server = _make_db_server(ca_url="https://kea.example.invalid", dhcp6=False)
        self.request = RequestFactory().post("/plugins/kea/sync/")
        self.request.user = get_user_model().objects.create_user("sync-operator")
        self.request.id = uuid.uuid4()

    def test_lease_update_keeps_previous_ip_address_fields(self):
        ip = IPAddress.objects.create(
            address="198.18.0.42/32",
            status="reserved",
            dns_name="old.example.invalid",
            description="[kea-sync: reservation] operator note",
        )
        with (
            event_tracking(self.request),
            stub_kea(
                {
                    "lease4-get-page": {
                        "result": 0,
                        "arguments": {
                            "count": 1,
                            "leases": [
                                {
                                    "ip-address": "198.18.0.42",
                                    "hostname": "new.example.invalid",
                                    "subnet-id": 1,
                                    "valid-lft": 3600,
                                    "state": 0,
                                }
                            ],
                        },
                    }
                }
            ),
        ):
            report = reconcile(self.server, 4, [LeasePhase(max_leases=None, subnet_prefix_lengths={1: 24})])
        self.assertEqual(report.errors, 0)
        self.assertEqual(report.updated, 1)
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(ip),
            changed_object_id=ip.pk,
            request_id=self.request.id,
            action="update",
        )
        self.assertIsNotNone(change.prechange_data)
        self.assertEqual(change.prechange_data["address"], "198.18.0.42/32")
        self.assertEqual(change.prechange_data["status"], "reserved")
        self.assertEqual(change.prechange_data["dns_name"], "old.example.invalid")
        self.assertEqual(change.prechange_data["description"], "[kea-sync: reservation] operator note")
        self.assertEqual(change.postchange_data["address"], "198.18.0.42/24")
        self.assertEqual(change.postchange_data["dns_name"], "new.example.invalid")
        self.assertEqual(change.postchange_data["status"], "dhcp")

    def test_lease_hostname_update_keeps_previous_mac_description(self):
        from dcim.models import MACAddress

        mac = MACAddress.objects.create(
            mac_address="02:00:00:00:00:42", description="dhcp_hostname: old.example.invalid"
        )
        with (
            event_tracking(self.request),
            stub_kea(
                {
                    "lease4-get-page": {
                        "result": 0,
                        "arguments": {
                            "count": 1,
                            "leases": [
                                {
                                    "ip-address": "198.18.0.42",
                                    "hostname": "new.example.invalid",
                                    "hw-address": "02:00:00:00:00:42",
                                    "subnet-id": 1,
                                    "valid-lft": 3600,
                                    "state": 0,
                                }
                            ],
                        },
                    }
                }
            ),
        ):
            report = reconcile(self.server, 4, [LeasePhase(max_leases=None, subnet_prefix_lengths={1: 24})])
        self.assertEqual(report.errors, 0)
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(mac),
            changed_object_id=mac.pk,
            request_id=self.request.id,
            action="update",
        )
        self.assertIsNotNone(change.prechange_data)
        self.assertEqual(change.prechange_data["description"], "dhcp_hostname: old.example.invalid")
        self.assertEqual(change.postchange_data["description"], "dhcp_hostname: new.example.invalid")

    def test_legacy_vrf_move_and_lease_update_keep_original_values(self):
        vrf = VRF.objects.create(name="sync-vrf")
        type(self.server).objects.filter(pk=self.server.pk).update(sync_vrf=vrf)
        self.server.refresh_from_db()
        ip = IPAddress.objects.create(
            address="198.18.0.42/32", status="reserved", description="Synced from Kea DHCP reservation"
        )
        with event_tracking(self.request), _kea([_lease("198.18.0.42", "new.example.invalid")]):
            report = reconcile(self.server, 4, [LeasePhase(max_leases=None, subnet_prefix_lengths={1: 24})])
        self.assertEqual(report.errors, 0)
        changes = list(
            ObjectChange.objects.filter(
                changed_object_type=ContentType.objects.get_for_model(ip),
                changed_object_id=ip.pk,
                request_id=self.request.id,
                action="update",
            ).order_by("pk")
        )
        self.assertEqual(len(changes), 2)
        move, update = changes
        self.assertIsNone(move.prechange_data["vrf"])
        self.assertEqual(move.prechange_data["address"], "198.18.0.42/32")
        self.assertEqual(move.postchange_data["vrf"], vrf.pk)
        self.assertEqual(update.prechange_data["vrf"], vrf.pk)
        self.assertEqual(update.prechange_data["address"], "198.18.0.42/32")
        self.assertEqual(update.prechange_data["status"], "reserved")
        self.assertEqual(update.postchange_data["address"], "198.18.0.42/24")

    def test_prefix_and_range_reactivation_and_deprecation_keep_previous_status(self):
        type(self.server).objects.filter(pk=self.server.pk).update(sync_deprecate_prefixes_and_ranges=True)
        self.server.refresh_from_db()
        subnets = [{"id": 1, "subnet": "198.18.0.0/24", "pools": [{"pool": "198.18.0.10 - 198.18.0.20"}]}]
        with stub_kea(_catalogue_responses_for_subnets(4, subnets)):
            observation = read_catalogue(self.server, 4)
            seeded = reconcile(self.server, 4, [SubnetPhase(observation), PoolPhase(observation)])
        self.assertEqual(seeded.prefix_errors, 0)
        Prefix.objects.update(status="deprecated")
        IPRange.objects.update(status="deprecated")
        for reported, old, new in ((subnets, "deprecated", "active"), ([], "active", "deprecated")):
            self.request.id = uuid.uuid4()
            with event_tracking(self.request), stub_kea(_catalogue_responses_for_subnets(4, reported)):
                observation = read_catalogue(self.server, 4)
                report = reconcile(self.server, 4, [SubnetPhase(observation), PoolPhase(observation)])
            self.assertEqual(report.prefix_errors, 0)
            for model in (Prefix, IPRange):
                with self.subTest(model=model, old=old):
                    change = ObjectChange.objects.get(
                        changed_object_type=ContentType.objects.get_for_model(model),
                        request_id=self.request.id,
                        action="update",
                    )
                    self.assertEqual(change.prechange_data["status"], old)
                    self.assertEqual(change.postchange_data["status"], new)

    def test_complete_job_keeps_previous_server_observation_receipt(self):
        with event_tracking(self.request):
            _run_job(self.server, [], [])
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(self.server),
            changed_object_id=self.server.pk,
            request_id=self.request.id,
            action="update",
        )
        self.assertEqual(change.prechange_data["ipam_initial_observations"], {})
        self.assertIsNone(change.prechange_data["ipam_first_complete_at"])
        self.assertIn("job", change.postchange_data["ipam_initial_observations"])
        self.assertIsNotNone(change.postchange_data["ipam_first_complete_at"])

    @override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="deprecate"))
    def test_stale_lease_and_reservation_cleanup_keep_previous_ip_status(self):
        address = "198.18.0.42"
        reservation = _reservation(address, "phone.example.invalid")
        subnets = [{"id": 1, "subnet": "198.18.0.0/24"}]
        seeded = _reconcile(self.server, [_lease(address)], [reservation], subnets=subnets)
        self.assertEqual(seeded.errors, 0)
        ip = IPAddress.objects.get(address__net_host=address)
        self.assertEqual(ip.status, "active")
        for reservations, old, new in (([reservation], "active", "reserved"), ([], "reserved", "deprecated")):
            self.request.id = uuid.uuid4()
            with event_tracking(self.request):
                report = _reconcile(self.server, reservations=reservations, subnets=subnets)
            self.assertEqual(report.errors, 0)
            change = ObjectChange.objects.get(
                changed_object_type=ContentType.objects.get_for_model(ip),
                changed_object_id=ip.pk,
                request_id=self.request.id,
                action="update",
            )
            self.assertEqual(change.prechange_data["status"], old)
            self.assertEqual(change.postchange_data["status"], new)


@override_settings(PLUGINS_CONFIG=plugins_config())
class DHCPImportChangeRecordTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not apps.is_installed("netbox_dhcp"):
            raise unittest.SkipTest("netbox_dhcp not installed")
        super().setUpClass()

    def test_reimport_keeps_previous_server_class_subnet_option_and_reservation_fields(self):
        from netbox_kea.integrations.dhcp_plugin import import_server_config

        server = _make_db_server(name="change-record-server", ca_url="https://kea.example.invalid", dhcp6=False)
        before = {
            "valid-lifetime": 3600,
            "option-def": [{"code": 224, "name": "gateway-label", "space": "dhcp4", "type": "string"}],
            "client-classes": [{"name": "phones", "test": "option[60].text == 'old'"}],
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "198.18.0.0/24",
                    "valid-lifetime": 7200,
                    "option-data": [{"code": 224, "space": "dhcp4", "data": "198.18.0.1"}],
                    "reservations": [
                        {"flex-id": "phone", "ip-address": "198.18.0.42", "hostname": "old.example.invalid"}
                    ],
                }
            ],
        }
        first = import_server_config(server, parse_dhcp_config(before, 4), _reservation_snapshot(before, 4))
        self.assertEqual(first.errors, 0, first.warnings)
        self.assertEqual(first.options_created, 1, first.warnings)
        after = copy.deepcopy(before)
        after["valid-lifetime"] = 1800
        after["client-classes"][0]["test"] = "option[60].text == 'new'"
        after["subnet4"][0]["valid-lifetime"] = 5400
        after["subnet4"][0]["option-data"][0]["data"] = "198.18.0.254"
        after["subnet4"][0]["reservations"][0]["hostname"] = "new.example.invalid"
        request = RequestFactory().post("/plugins/kea/import/")
        request.user = get_user_model().objects.create_user("import-operator")
        request.id = uuid.uuid4()
        with event_tracking(request):
            second = import_server_config(server, parse_dhcp_config(after, 4), _reservation_snapshot(after, 4))
        self.assertEqual(second.errors, 0, second.warnings)
        self.assertEqual(second.options_updated, 1, second.warnings)
        for model_name, field, old, new in (
            ("DHCPServer", "valid_lifetime", 3600, 1800),
            ("ClientClass", "test", "option[60].text == 'old'", "option[60].text == 'new'"),
            ("Subnet", "valid_lifetime", 7200, 5400),
            ("Option", "data", "198.18.0.1", "198.18.0.254"),
            ("HostReservation", "hostname", "old.example.invalid", "new.example.invalid"),
        ):
            with self.subTest(model=model_name):
                change = ObjectChange.objects.get(
                    changed_object_type=ContentType.objects.get_for_model(apps.get_model("netbox_dhcp", model_name)),
                    request_id=request.id,
                    action="update",
                )
                self.assertIsNotNone(change.prechange_data)
                self.assertEqual(change.prechange_data[field], old)
                self.assertEqual(change.postchange_data[field], new)
