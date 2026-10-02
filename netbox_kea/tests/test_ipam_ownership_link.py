# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The IPAM Ownership link (ADR 0006): its constraints, its CASCADE keys and the confirmation sequence."""

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from ipam.models import IPAddress, IPRange, Prefix
from netaddr import IPNetwork

from netbox_kea.models import CONFIRMATION_SEQUENCE, IPAMOwnershipLink, next_confirmation_number
from netbox_kea.tests.utils import _make_db_server


class IPAMOwnershipLinkTest(TestCase):
    """A link names exactly one IP address, Prefix or IP Range, once per Server, family and source."""

    def setUp(self):
        self.server = _make_db_server(name="owner")
        self.objects = {
            "ip_address": IPAddress.objects.create(address="192.0.2.10/24"),
            "prefix": Prefix.objects.create(prefix="192.0.2.0/24"),
            "ip_range": IPRange.objects.create(
                start_address=IPNetwork("192.0.2.100/24"), end_address=IPNetwork("192.0.2.199/24")
            ),
        }

    def _link(self, **fields) -> IPAMOwnershipLink:
        values = {"server": self.server, "family": 4, "source": "lease", "confirmation": 1, **fields}
        return IPAMOwnershipLink.objects.create(**values)

    def _assert_refused(self, **fields) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._link(**fields)

    def test_a_link_to_each_object_type_is_stored_with_its_defaults(self):
        for key, obj in self.objects.items():
            with self.subTest(key):
                link = IPAMOwnershipLink.objects.get(pk=self._link(**{key: obj}).pk)

                self.assertEqual(link.owned_object, obj)
                self.assertIsNone(link.facts)
                self.assertIsNone(link.stale_mark)

    def test_a_link_without_an_object_is_refused(self):
        self._assert_refused()

    def test_a_link_to_two_objects_is_refused(self):
        self._assert_refused(ip_address=self.objects["ip_address"], prefix=self.objects["prefix"])
        self._assert_refused(prefix=self.objects["prefix"], ip_range=self.objects["ip_range"])

    def test_a_second_link_of_one_owner_to_one_object_is_refused(self):
        for key, obj in self.objects.items():
            with self.subTest(key):
                self._link(**{key: obj})

                self._assert_refused(**{key: obj})

    def test_another_source_family_or_server_links_the_same_object(self):
        other = _make_db_server(name="other owner")
        ip = self.objects["ip_address"]

        self._link(ip_address=ip)
        self._link(ip_address=ip, source="reservation")
        self._link(ip_address=ip, family=6)
        self._link(ip_address=ip, server=other)

        self.assertEqual(IPAMOwnershipLink.objects.filter(ip_address=ip).count(), 4)

    def test_an_unknown_family_or_source_is_refused(self):
        self._assert_refused(ip_address=self.objects["ip_address"], family=5)
        self._assert_refused(ip_address=self.objects["ip_address"], source="dhcp")

    def test_deleting_the_object_deletes_its_links(self):
        for key, obj in self.objects.items():
            with self.subTest(key):
                link = self._link(**{key: obj})

                obj.delete()

                self.assertFalse(IPAMOwnershipLink.objects.filter(pk=link.pk).exists())

    def test_deleting_the_server_deletes_its_links(self):
        link = self._link(ip_address=self.objects["ip_address"])

        self.server.delete()

        self.assertFalse(IPAMOwnershipLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(IPAddress.objects.filter(pk=self.objects["ip_address"].pk).exists())


class ConfirmationSequenceTest(TestCase):
    """Confirmation and cutoff numbers come from one PostgreSQL sequence with a cache of 1."""

    def test_the_migration_creates_the_sequence_with_a_cache_of_one(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT cache_size FROM pg_sequences WHERE sequencename = %s", [CONFIRMATION_SEQUENCE])
            rows = cursor.fetchall()

        self.assertEqual(rows, [(1,)])

    def test_each_number_is_larger_than_the_one_before(self):
        numbers = [next_confirmation_number() for _ in range(3)]

        self.assertEqual(numbers, sorted(set(numbers)))
        self.assertTrue(all(isinstance(number, int) for number in numbers))
