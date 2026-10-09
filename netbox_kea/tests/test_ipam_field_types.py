# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IPAM writes give netaddr values to the address fields, so other plugins' save signals can read them."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from core.exceptions import JobFailed
from core.models import Job
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save
from django.test import TestCase, override_settings
from ipam.models import IPAddress, IPRange, Prefix
from netaddr import IPNetwork

from netbox_kea.ipam_reconciliation import claim
from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.tests.kea_stub import _catalogue_responses_for_subnets, stub_kea, typed_lease
from netbox_kea.tests.test_ipam_reconciliation import _kea, _lease, _server
from netbox_kea.tests.test_prefix_pool_reconciliation import SUBNET, run_job
from netbox_kea.tests.utils import _make_db_server, plugins_config

_FIELDS = {IPAddress: ("address",), Prefix: ("prefix",), IPRange: ("start_address", "end_address")}


@contextmanager
def _saved_field_types() -> Iterator[list[tuple[str, str, type]]]:
    """Record the type of each IPAM address field that a post_save receiver sees."""
    seen: list[tuple[str, str, type]] = []

    def receiver(sender, instance, **kwargs):
        seen.extend((sender.__name__, name, type(getattr(instance, name))) for name in _FIELDS[sender])

    for model in _FIELDS:
        post_save.connect(receiver, sender=model, weak=False)
    try:
        yield seen
    finally:
        for model in _FIELDS:
            post_save.disconnect(receiver, sender=model)


def _dns_view_guard(sender, instance, **kwargs):
    """Refuse a change of prefix or VRF, as NetBox DNS does for a Prefix that a DNS view uses."""
    if instance.pk is None:
        return
    saved = Prefix.objects.get(pk=instance.pk)
    if saved.prefix != instance.prefix or saved.vrf_id != instance.vrf_id:
        raise ValidationError("Prefix is assigned to DNS views _default_. Prefix and VRF must not be changed")


def _refuse_every_prefix_update(sender, instance, **kwargs):
    if instance.pk is not None:
        raise ValidationError("Prefix is assigned to DNS views _default_. Prefix and VRF must not be changed")


@override_settings(PLUGINS_CONFIG=plugins_config())
class IPAMFieldTypesTest(TestCase):
    def assert_netaddr(self, seen, expected_models):
        self.assertEqual({model for model, _name, _type in seen}, expected_models)
        self.assertEqual([entry for entry in seen if entry[2] is not IPNetwork], [])

    def test_a_claimed_ip_address_is_created_and_updated_with_an_ip_network(self):
        server = _server("field-types")
        with _saved_field_types() as seen, _kea():
            claim(server, 4, [typed_lease(_lease(hostname="one"))], force=False)
            claim(server, 4, [typed_lease(_lease(hostname="two"))], force=False)
        self.assertEqual(IPAddress.objects.get().dns_name, "two")
        self.assertEqual(len(seen), 2)
        self.assert_netaddr(seen, {"IPAddress"})

    def test_a_synchronized_prefix_and_range_are_created_and_updated_with_ip_networks(self):
        server = _make_db_server(dhcp6=False, sync_leases_enabled=False, sync_reservations_enabled=False)
        with _saved_field_types() as seen:
            run_job(server)
            Prefix.objects.update(status="reserved")
            IPRange.objects.update(status="reserved")
            run_job(server)
        self.assertEqual(set(Prefix.objects.values_list("status", flat=True)), {"active"})
        self.assertEqual(len([entry for entry in seen if entry[0] == "Prefix"]), 2)
        self.assert_netaddr(seen, {"Prefix", "IPRange"})

    def test_a_legacy_marked_prefix_in_a_dns_view_gets_the_new_marker_once(self):
        server = _make_db_server(dhcp6=False, sync_leases_enabled=False, sync_reservations_enabled=False)
        prefix = Prefix.objects.create(
            prefix=IPNetwork("198.18.0.0/24"), status="active", description="Synced from Kea DHCP subnet"
        )
        pre_save.connect(_dns_view_guard, sender=Prefix, weak=False)
        self.addCleanup(pre_save.disconnect, _dns_view_guard, sender=Prefix)
        with _saved_field_types() as seen:
            first = run_job(server)
            second = run_job(server)
        prefix.refresh_from_db()
        self.assertEqual(prefix.description, "[kea-sync: subnet]")
        self.assertEqual((first["errors"], first["prefix_errors"]), (0, 0))
        self.assertEqual((second["created"], second["updated"], second["errors"]), (0, 0, 0))
        self.assertEqual(len([entry for entry in seen if entry[0] == "Prefix"]), 1)

    def test_an_unhandled_server_error_names_its_exception_and_writes_its_traceback(self):
        server = _make_db_server(dhcp6=False, sync_leases_enabled=False, sync_reservations_enabled=False)
        Prefix.objects.create(
            prefix=IPNetwork("198.18.0.0/24"), status="active", description="Synced from Kea DHCP subnet"
        )
        pre_save.connect(_refuse_every_prefix_update, sender=Prefix, weak=False)
        self.addCleanup(pre_save.disconnect, _refuse_every_prefix_update, sender=Prefix)
        job = Job.objects.create(name="Kea IPAM Sync", job_id=uuid.uuid4(), data={})
        with (
            stub_kea(_catalogue_responses_for_subnets(4, [SUBNET])),
            self.assertLogs("netbox_kea.jobs", level="ERROR") as module_log,
            suppress(JobFailed),
        ):
            KeaIpamSyncJob(job).run(server_pk=server.pk)
        # NetBox's JobLogHandler on the job logger blocks Python's last-resort stderr handler; the module logger does not.
        self.assertEqual([record.exc_info[0] for record in module_log.records], [ValidationError])
        failures = [
            entry["message"] for entry in job.log_entries if "Unhandled error syncing server" in entry["message"]
        ]
        self.assertEqual(len(failures), 1, job.log_entries)
        self.assertIn("ValidationError: ", failures[0])
        self.assertNotIn("%s", failures[0])
