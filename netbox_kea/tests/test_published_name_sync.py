# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Reservation and lease sources report the same published name, so a repeated sync changes nothing (#304)."""

import ipaddress
import uuid
from typing import Any
from urllib.parse import urlencode

from core.models import Job, ObjectChange
from dcim.models import MACAddress
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress

from netbox_kea.jobs import KeaIpamSyncJob
from netbox_kea.published_name import reservation_published_name
from netbox_kea.reservations import (
    GlobalReservationScope,
    InSubnetReservationScope,
    IPv4Reservation,
    ReservationIdentity,
)
from netbox_kea.subnet_catalogue import CatalogueUnavailable, display
from netbox_kea.tests.kea_stub import _catalogue_responses_for_subnets, _res_get, complete_lease, stub_kea
from netbox_kea.tests.test_jobs import _patch_kea
from netbox_kea.tests.test_kea_recordings import _recording
from netbox_kea.tests.utils import _make_db_server, plugins_config

_HW = "aa:bb:cc:dd:ee:42"
_SUFFIX = "dhcp.example.com"
_PUBLISHED = "host.dhcp.example.com"
_CASES: dict[int, dict[str, Any]] = {
    4: {
        "cidr": "198.18.0.0/24",
        "address": "198.18.0.42",
        "reservation": {"subnet-id": 1, "hw-address": _HW, "ip-address": "198.18.0.42", "hostname": "host"},
        # Kea gives a DHCPv4 client the qualified name, lower case and without a trailing dot.
        "lease_hostname": _PUBLISHED,
    },
    6: {
        "cidr": "2001:db8:42::/64",
        "address": "2001:db8:42::42",
        "reservation": {"subnet-id": 1, "hw-address": _HW, "ip-addresses": ["2001:db8:42::42"], "hostname": "host"},
        # A DHCPv6 lease keeps the trailing dot of the FQDN.
        "lease_hostname": f"{_PUBLISHED}.",
    },
}


def _lease(family: int, hostname: str | None = None) -> dict:
    case = _CASES[family]
    return complete_lease(
        {
            "ip-address": case["address"],
            "hw-address": _HW,
            "hostname": case["lease_hostname"] if hostname is None else hostname,
            "subnet-id": 1,
            "valid-lft": 3600,
            "state": 0,
        }
    )


def _catalogue(family: int) -> dict:
    """The catalogue replies of one Subnet that takes the global DDNS qualifying suffix."""
    responses = _catalogue_responses_for_subnets(family, [{"id": 1, "subnet": _CASES[family]["cidr"]}])
    responses["config-get"]["arguments"][f"Dhcp{family}"]["ddns-qualifying-suffix"] = _SUFFIX
    return responses


class _ChangeRecords(TestCase):
    def _changes(self, *objects) -> list[list[int]]:
        return [
            list(
                ObjectChange.objects.filter(
                    changed_object_type=ContentType.objects.get_for_model(obj), changed_object_id=obj.pk
                )
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            for obj in objects
        ]

    def _records(self, family: int) -> tuple[IPAddress, MACAddress]:
        ip = IPAddress.objects.get(address__net_host=_CASES[family]["address"])
        mac = MACAddress.objects.get(mac_address=_HW)
        return ip, mac

    def _assert_keeps_the_name(self, change_pks) -> None:
        for change in ObjectChange.objects.filter(pk__in=change_pks):
            self.assertEqual(change.action, "update")
            self.assertEqual(change.prechange_data["dns_name"], change.postchange_data["dns_name"])

    def _assert_published(self, family: int) -> tuple[IPAddress, MACAddress]:
        ip, mac = self._records(family)
        self.assertEqual(ip.dns_name, _PUBLISHED)
        self.assertEqual(mac.description, f"dhcp_hostname: {_PUBLISHED}")
        return ip, mac


@override_settings(PLUGINS_CONFIG=plugins_config())
class TestSyncJobPublishedName(_ChangeRecords):
    def _run(self) -> Job:
        job = Job.objects.create(name=KeaIpamSyncJob.name, job_id=uuid.uuid4(), status="pending")
        with _patch_kea(leases4=[_lease(4)], reservations=[_CASES[4]["reservation"]], responses=_catalogue(4)):
            KeaIpamSyncJob.handle(job)
        job.refresh_from_db()
        return job

    def test_the_lease_and_reservation_phases_write_one_name(self):
        _make_db_server(dhcp6=False)
        self.assertEqual(self._run().status, "completed")
        ip, mac = self._assert_published(4)
        # The second phase of the run sets the status of the IP address, but it does not change the name again.
        self._assert_keeps_the_name(self._changes(ip)[0][1:])
        self.assertEqual(len(self._changes(mac)[0]), 1)
        before = self._changes(ip, mac)

        self.assertEqual(self._run().status, "completed")
        self._assert_published(4)
        self.assertEqual(self._changes(ip, mac), before)


@override_settings(PLUGINS_CONFIG=plugins_config())
class TestPerRowSyncPublishedName(_ChangeRecords):
    def setUp(self):
        self.server = _make_db_server()
        user = get_user_model().objects.create_superuser(username="published-name", password="example-password")
        self.client.force_login(user)

    def _sync_reservation(self, family: int) -> None:
        url = reverse(f"plugins:netbox_kea:server_reservation{family}_sync", args=[self.server.pk, 1])
        query = urlencode({"identifier_type": "hw-address", "identifier": _HW})
        responses = {**_catalogue(family), "reservation-get": _res_get(_CASES[family]["reservation"])}
        with stub_kea(responses):
            response = self.client.post(f"{url}?{query}")
        self.assertEqual(response.status_code, 200, response.content)

    def _sync_lease(self, family: int, hostname: str | None = None) -> None:
        url = reverse(f"plugins:netbox_kea:server_lease{family}_sync", args=[self.server.pk])
        responses = {**_catalogue(family), f"lease{family}-get": {"result": 0, "arguments": _lease(family, hostname)}}
        with stub_kea(responses):
            response = self.client.post(url, {"ip_address": _CASES[family]["address"]})
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_reservation_sync_then_a_lease_sync_records_no_name_change(self):
        for family in (4, 6):
            with self.subTest(family=family):
                self._sync_reservation(family)
                ip, mac = self._assert_published(family)
                before = self._changes(ip, mac)
                self._sync_lease(family)
                self._assert_published(family)
                after_lease = self._changes(ip, mac)
                # The lease sync adds its ownership to the IP address, but it changes neither name nor the MAC.
                self.assertEqual(after_lease[1], before[1])
                self._assert_keeps_the_name(set(after_lease[0]) - set(before[0]))
                self._sync_reservation(family)
                self._sync_lease(family)
                self._assert_published(family)
                self.assertEqual(self._changes(ip, mac), after_lease)

    def test_a_dhcpv6_lease_without_an_fqdn_does_not_rename_a_reserved_host(self):
        # Without a Client FQDN option, Kea stores the raw reserved hostname in the lease (dhcp6_srv.cc).
        self._sync_reservation(6)
        ip, mac = self._assert_published(6)
        before = self._changes(ip, mac)
        self._sync_lease(6, "host")
        self._assert_published(6)
        after_lease = self._changes(ip, mac)
        self.assertEqual(after_lease[1], before[1])
        self._assert_keeps_the_name(set(after_lease[0]) - set(before[0]))
        self._sync_reservation(6)
        self._sync_lease(6, "host")
        self._assert_published(6)
        self.assertEqual(self._changes(ip, mac), after_lease)


class TestReservationPublishedName(TestCase):
    """The effective suffix of a Reservation scope, read from a configuration recorded from Kea 3.2.0."""

    def setUp(self):
        self.server = _make_db_server()

    def _catalogue(self, config_get=None):
        recording = _recording(4)
        with stub_kea({"config-get": config_get or recording["config-get"], "subnet4-list": recording["subnet4-list"]}):
            return display(self.server, 4)

    def _reservation(self, scope, *addresses, hostname="host"):
        return IPv4Reservation(
            scope=scope,
            identity=ReservationIdentity("hw-address", _HW),
            addresses=tuple(ipaddress.IPv4Address(address) for address in addresses),
            hostname=hostname,
        )

    def test_the_scope_selects_the_suffix(self):
        catalogue = self._catalogue()
        in_21 = InSubnetReservationScope(catalogue.find_by_id(21).identity)
        for reservation, expected in (
            (self._reservation(in_21, "198.51.100.130"), "host.office.example.org"),
            (self._reservation(in_21, "198.51.100.210"), "host.pool.example.org"),
            (self._reservation(GlobalReservationScope(), "198.51.100.210"), "host.pool.example.org"),
            (self._reservation(GlobalReservationScope(), "198.51.100.20"), "host.office.example.net"),
            (self._reservation(GlobalReservationScope(), "192.0.2.30"), "host"),
            (self._reservation(GlobalReservationScope(), "203.0.113.5"), "host.dhcp.example.com"),
            (self._reservation(GlobalReservationScope()), "host.dhcp.example.com"),
        ):
            with self.subTest(reservation=reservation):
                self.assertEqual(reservation_published_name(reservation, catalogue), expected)

    def test_an_unknown_suffix_fails_instead_of_using_the_stored_name(self):
        catalogue = self._catalogue(RuntimeError("config-get failed"))
        in_21 = InSubnetReservationScope(catalogue.find_by_id(21).identity)
        for reservation in (self._reservation(in_21, "198.51.100.130"), self._reservation(GlobalReservationScope())):
            with self.subTest(reservation=reservation), self.assertRaises(CatalogueUnavailable):
                reservation_published_name(reservation, catalogue)
        self.assertEqual(reservation_published_name(self._reservation(in_21, hostname=""), catalogue), "")

    def test_a_reservation_without_an_address_in_a_subnet_with_a_pool_suffix_is_unknown(self):
        catalogue = self._catalogue()
        in_21 = InSubnetReservationScope(catalogue.find_by_id(21).identity)
        with self.assertRaises(CatalogueUnavailable):
            reservation_published_name(self._reservation(in_21), catalogue)
