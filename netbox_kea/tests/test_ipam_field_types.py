# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IPAM writes give netaddr values to the address fields, so other plugins' save signals can read them."""

from collections.abc import Iterator
from contextlib import contextmanager

from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from ipam.models import IPAddress, IPRange, Prefix
from netaddr import IPNetwork

from netbox_kea.ipam_reconciliation import claim
from netbox_kea.tests.kea_stub import typed_lease
from netbox_kea.tests.test_ipam_reconciliation import _kea, _lease, _server
from netbox_kea.tests.test_prefix_pool_reconciliation import run_job
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
