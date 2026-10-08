# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for Phase 3: NetBox IPAM sync endpoints.

URL names (all registered in netbox_kea/urls.py):
  server_lease4_sync       — POST /servers/<pk>/leases4/sync/
  server_lease6_sync       — POST /servers/<pk>/leases6/sync/
  server_reservation4_sync: POST /servers/<pk>/reservations4/<subnet-id>/sync/
  server_reservation6_sync: POST /servers/<pk>/reservations6/<subnet-id>/sync/

Lease endpoints accept POST with:
  ip_address   — host IP to sync
  hostname     — (optional) hostname / dns_name
  status       — "active" (leases) or "reserved" (reservations)

Returns an HTMX HTML fragment (<td> content) with a link to the new/updated
NetBox IPAddress, or an error message if something went wrong.

These tests drive the real ``KeaClient`` and IPAM synchronization operations.
They assert the NetBox ``IPAddress`` rows and ownership links. Only the HTTP boundary to Kea is stubbed
via ``kea_stub.stub_kea``:

* single lease sync       → ``lease{v}-get`` (echoes the posted IP back)
* single reservation sync: Subnet Catalogue plus an exact ``reservation-get``
* bulk reservation sync: Subnet Catalogue plus ``reservation-get-page``
"""

from __future__ import annotations

import requests
from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.signals import pre_save
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress as NbIP

from netbox_kea.models import IPAMOwnershipLink, Server, next_confirmation_number
from netbox_kea.views.reservations import _RESERVATION_PAGE_SIZE

from .kea_stub import (
    _catalogue_responses,
    _catalogue_responses_for_subnets,
    _res_page,
    _reservation_mutation_commands,
    complete_lease,
    lease_record,
    queued,
    stub_kea,
)
from .utils import _PLUGINS_CONFIG

User = get_user_model()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — echo the queried IP back so the real sync creates the matching NbIP
# ─────────────────────────────────────────────────────────────────────────────


def _lease_get(hostname, **extra):
    """Build a ``lease{v}-get`` callable that echoes the queried ip-address."""

    def _resp(body):
        ip = body["arguments"]["ip-address"]
        return {
            "result": 0,
            "arguments": complete_lease({"ip-address": ip, "hostname": hostname, "subnet-id": 1, **extra}),
        }

    return _resp


def _reservation_get(hostname, address, *, version=4, **extra):
    """Build an exact typed ``reservation-get`` response."""
    address_fields = {"ip-address": address} if version == 4 else {"ip-addresses": [address]}
    return {
        "result": 0,
        "arguments": {**address_fields, "hostname": hostname, "subnet-id": 1, **extra},
    }


def _make_server(**kwargs) -> Server:
    defaults = {
        "name": "sync-test-kea",
        "ca_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _SyncViewBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="sync_testuser",
            email="sync_test@example.com",
            password="sync_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_server()

    def _start_stub(self, responses):
        """Enter a ``stub_kea`` context for the whole test and return the stub."""
        cm = stub_kea(responses)
        stub = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return stub


# ─────────────────────────────────────────────────────────────────────────────
# TestLease4SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestMalformedLeaseSyncView(_SyncViewBase):
    def test_nonstring_hostname_returns_a_generic_error_without_claiming(self):
        for family, address, network in ((4, "198.18.0.10", "198.18.0.0/24"), (6, "2001:db8::10", "2001:db8::/64")):
            for hostname in (42, True, [], {"private diagnostic": "hostname"}):
                with (
                    self.subTest(family=family, hostname=hostname),
                    stub_kea(
                        {
                            **_catalogue_responses(family, 1, network),
                            f"lease{family}-get": _lease_get(hostname),
                        }
                    ),
                ):
                    response = self.client.post(
                        reverse(f"plugins:netbox_kea:server_lease{family}_sync", args=[self.server.pk]),
                        {"ip_address": address},
                    )
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.content, b"Sync error: see server logs for details.")
                    self.assertFalse(NbIP.objects.exists())
                    self.assertFalse(IPAMOwnershipLink.objects.exists())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease4SyncView(_SyncViewBase):
    """POST to server_lease4_sync creates/updates a NetBox IPAddress."""

    def test_failed_mac_update_keeps_the_lease_claim_and_rolls_back_only_the_mac(self):
        from dcim.models import MACAddress
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE dcim_macaddress ADD CONSTRAINT reject_sync_hostname "
                "CHECK (description <> 'dhcp_hostname: rejected.example.invalid')"
            )
        for index, existing in enumerate((False, True)):
            hardware = f"02:00:00:00:00:{index + 1:02x}"
            address = f"198.18.0.{10 + index}"
            original = "Operator description"
            if existing:
                MACAddress.objects.create(mac_address=hardware, description=original)
            with (
                self.subTest(existing=existing),
                stub_kea(
                    {
                        **_catalogue_responses(4, 1, "198.18.0.0/24"),
                        "lease4-get": _lease_get("rejected.example.invalid", **{"hw-address": hardware}),
                    }
                ),
            ):
                response = self.client.post(self._url(), {"ip_address": address})
                self.assertContains(response, f"{address}/24")
                ip = NbIP.objects.get(address__net_host=address)
                self.assertEqual((ip.status, ip.dns_name), ("dhcp", "rejected.example.invalid"))
                self.assertEqual(IPAMOwnershipLink.objects.get(ip_address=ip).server_id, self.server.pk)
                if existing:
                    self.assertEqual(MACAddress.objects.get(mac_address=hardware).description, original)
                else:
                    self.assertFalse(MACAddress.objects.filter(mac_address=hardware).exists())
            with stub_kea(
                {
                    **_catalogue_responses(4, 1, "198.18.0.0/24"),
                    "lease4-get": _lease_get("accepted.example.invalid", **{"hw-address": hardware}),
                }
            ):
                response = self.client.post(self._url(), {"ip_address": address})
            self.assertContains(response, f"{address}/24")
            self.assertEqual(
                MACAddress.objects.get(mac_address=hardware).description, "dhcp_hostname: accepted.example.invalid"
            )

    def test_malformed_lease_subnet_id_returns_a_generic_error_without_claiming(self):
        from netbox_kea.models import IPAMOwnershipLink

        for index, subnet_id in enumerate(([], {}, True, False, 1.0, "1", None, 0, -1, 4_294_967_295)):
            address = f"198.18.0.{20 + index}"
            with (
                self.subTest(subnet_id=subnet_id),
                stub_kea(
                    {
                        **_catalogue_responses(4, 1, "198.18.0.0/24"),
                        "lease4-get": _lease_get("host.example.com", **{"subnet-id": subnet_id}),
                    }
                ),
            ):
                response = self.client.post(self._url(), {"ip_address": address})
                self.assertContains(response, "Sync error: see server logs", status_code=500)
                self.assertFalse(NbIP.objects.filter(address__net_host=address).exists())
                self.assertFalse(IPAMOwnershipLink.objects.filter(ip_address__address__net_host=address).exists())

    def test_highest_valid_kea_subnet_id_can_be_claimed(self):
        from netbox_kea.models import IPAMOwnershipLink

        with stub_kea(
            {
                **_catalogue_responses(4, 4_294_967_294, "198.18.0.0/24"),
                "lease4-get": _lease_get("host.example.com", **{"subnet-id": 4_294_967_294}),
            }
        ):
            response = self.client.post(self._url(), {"ip_address": "198.18.0.10"})
        self.assertContains(response, "198.18.0.10/24")
        self.assertEqual(IPAMOwnershipLink.objects.get(server=self.server).facts["prefix_length"], 24)

    def test_unavailable_catalogue_returns_a_generic_error_without_claiming(self):
        from netbox_kea.models import IPAMOwnershipLink

        with stub_kea(
            {
                "lease4-get": _lease_get("host.example.com"),
                "subnet4-list": {"result": 1, "text": "private diagnostic"},
                "config-get": {"result": 1, "text": "private diagnostic"},
            }
        ):
            response = self.client.post(self._url(), {"ip_address": "198.18.0.10"})
        self.assertContains(response, "Sync error: see server logs", status_code=500)
        self.assertNotContains(response, "private diagnostic", status_code=500)
        self.assertFalse(NbIP.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_duplicate_ipam_rows_report_a_sync_error_without_changing_either(self):
        from netbox_kea.models import IPAMOwnershipLink

        NbIP.objects.create(address="198.18.0.10/24", description="[kea-sync: lease]")
        NbIP.objects.create(address="198.18.0.10/32", description="[kea-sync: lease]")
        before = list(NbIP.objects.order_by("pk").values())
        response = self.client.post(
            reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk]),
            {"ip_address": "198.18.0.10"},
        )
        self.assertContains(response, "Sync error: see server logs", status_code=500)
        self.assertEqual(list(NbIP.objects.order_by("pk").values()), before)
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def setUp(self):
        super().setUp()
        self._start_stub(
            {
                **_catalogue_responses(4, 1, "192.168.0.0/16"),
                "lease4-get": _lease_get("mock-host.local", **{"hw-address": "aa:bb:cc:00:00:01", "valid-lft": 86400}),
            }
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(self._url(), {"ip_address": "192.168.10.5", "hostname": "host-a"})
        self.assertEqual(response.status_code, 200)

    def test_creates_netbox_ip_on_post(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.6", "hostname": "host-b"})
        self.assertTrue(NbIP.objects.filter(address__net_host="192.168.10.6").exists())

    def test_created_ip_has_dhcp_status(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.7", "hostname": "host-c"})
        ip = NbIP.objects.filter(address__net_host="192.168.10.7").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "dhcp")

    def test_created_ip_has_correct_dns_name(self):
        # hostname in POST is ignored; dns_name comes from Kea lease data (mock returns "mock-host.local")
        self.client.post(self._url(), {"ip_address": "192.168.10.8", "hostname": "dns-test.local"})
        ip = NbIP.objects.filter(address__net_host="192.168.10.8").first()
        self.assertEqual(ip.dns_name, "mock-host.local")

    def test_response_contains_ip_link(self):
        response = self.client.post(self._url(), {"ip_address": "192.168.10.9", "hostname": "link-host"})
        self.assertContains(response, "192.168.10.9")
        # Response must contain a link to the NetBox IP detail page
        self.assertContains(response, "/ipam/ip-addresses/")

    def test_returns_400_when_ip_address_missing(self):
        response = self.client.post(self._url(), {"hostname": "no-ip"})
        self.assertEqual(response.status_code, 400)

    def test_malformed_lease_response_returns_400(self):
        """A malformed Kea response does not escape the live-data boundary."""
        with stub_kea({"lease4-get": {"result": 0, "arguments": None}}):
            response = self.client.post(self._url(), {"ip_address": "192.168.10.5"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Could not fetch live data", status_code=400)

    def test_idempotent_second_post_does_not_create_duplicate(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.20", "hostname": "idem-host"})
        self.client.post(self._url(), {"ip_address": "192.168.10.20", "hostname": "idem-host"})
        self.assertEqual(NbIP.objects.filter(address__net_host="192.168.10.20").count(), 1)

    def test_returns_404_for_nonexistent_server(self):
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[99999])
        response = self.client.post(url, {"ip_address": "192.168.10.30", "hostname": "ghost"})
        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self._url(), {"ip_address": "192.168.10.31", "hostname": "anon"})
        # Should redirect to login (3xx) or return 403
        self.assertIn(response.status_code, [302, 403])


# ─────────────────────────────────────────────────────────────────────────────
# TestLease6SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease6SyncView(_SyncViewBase):
    """POST to server_lease6_sync creates/updates a NetBox IPAddress for IPv6."""

    def setUp(self):
        super().setUp()
        self._start_stub(
            {
                **_catalogue_responses(6, 1, "2001:db8::/64"),
                "lease6-get": _lease_get("mock-v6.local", duid="01:02:03:04", **{"valid-lft": 86400}),
            }
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease6_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(
            self._url(),
            {"ip_address": "2001:db8::1", "hostname": "v6host"},
        )
        self.assertEqual(response.status_code, 200)

    def test_creates_netbox_ip_with_kea_subnet_mask_for_ipv6(self):

        self.client.post(
            self._url(),
            {"ip_address": "2001:db8::2", "hostname": "v6host2"},
        )
        ip = NbIP.objects.filter(address__net_host="2001:db8::2").first()
        self.assertIsNotNone(ip)
        self.assertTrue(str(ip.address).endswith("/64"))

    def test_created_ip_has_dhcp_status(self):

        self.client.post(
            self._url(),
            {"ip_address": "2001:db8::3", "hostname": "v6host3"},
        )
        ip = NbIP.objects.filter(address__net_host="2001:db8::3").first()
        self.assertEqual(ip.status, "dhcp")

    def test_malformed_lease_response_returns_400(self):
        """A malformed DHCPv6 lease response does not escape the live-data boundary."""
        with stub_kea({"lease6-get": {"result": 0, "arguments": None}}):
            response = self.client.post(self._url(), {"ip_address": "2001:db8::4"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Could not fetch live data", status_code=400)


# ─────────────────────────────────────────────────────────────────────────────
# Per-kind lease Sync: delegated prefixes, permissions and current use
# ─────────────────────────────────────────────────────────────────────────────

_PD_SUBNETS = [{"id": 10, "subnet": "2001:db8:1::/64"}]
_PD_LABEL = "2001:db8:100:100::/56"
_PD_ADDRESS_LABEL = "2001:db8:1::10"


def _pd_record(**changes) -> dict:
    return lease_record("2001:db8:100:100::", **{"type": "IA_PD", "prefix_len": 56, "subnet_id": 10, **changes})


def _lease6_get(*records: dict):
    """A ``lease6-get`` responder that holds *records*, keyed as Kea 3.2 looks them up (address and type)."""
    held = {(record["ip-address"], record.get("type", "IA_NA")): record for record in records}

    def respond(body: dict) -> dict:
        arguments = body["arguments"]
        record = held.get((arguments["ip-address"], arguments.get("type", "IA_NA")))
        if record is None:
            return {"result": 3, "text": "Lease not found."}
        return {"result": 0, "text": "IPv6 lease found.", "arguments": record}

    return respond


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSyncByKind(_SyncViewBase):
    """The lease Sync claims an address as an IP Address and a delegated prefix as a Prefix in the sync VRF."""

    def setUp(self):
        super().setUp()
        from ipam.models import VRF

        from netbox_kea.models import SyncConfig

        self.vrf = VRF.objects.create(name="kea-sync-vrf")
        self.server.sync_vrf = self.vrf
        # Manual Sync does not depend on any automatic synchronization setting.
        flags = {
            "sync_enabled": False,
            "sync_leases_enabled": False,
            "sync_reservations_enabled": False,
            "sync_prefixes_enabled": False,
            "sync_ip_ranges_enabled": False,
        }
        for name, value in flags.items():
            setattr(self.server, name, value)
        self.server.save()
        SyncConfig.objects.filter(pk=1).update(**flags)

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease6_sync", args=[self.server.pk])

    def _post(self, label: str, *records: dict, subnets=_PD_SUBNETS):
        responses = {**_catalogue_responses_for_subnets(6, subnets), "lease6-get": _lease6_get(*records)}
        with stub_kea(responses) as kea:
            response = self.client.post(self._url(), {"ip_address": label})
        return response, kea

    def _login_with(self, *codenames: str):
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        user = User.objects.create(username=f"sync-{'-'.join(codenames)}")
        view = ObjectPermission.objects.create(name=f"view-server-{user.pk}", actions=["view"])
        view.object_types.add(ContentType.objects.get_for_model(Server))
        view.users.add(user)
        for codename in codenames:
            action, model = codename.split("_", 1)
            grant = ObjectPermission.objects.create(name=f"{codename}-{user.pk}", actions=[action])
            grant.object_types.add(ContentType.objects.get(app_label="ipam", model=model))
            grant.users.add(user)
        self.client.force_login(user)

    def _assert_no_ipam_rows(self):
        from ipam.models import Prefix

        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(NbIP.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_a_delegated_prefix_sync_creates_a_prefix_in_the_sync_vrf_and_no_ip_address(self):
        from ipam.models import Prefix

        response, kea = self._post(_PD_LABEL, _pd_record())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            kea.bodies("lease6-get")[0]["arguments"], {"ip-address": "2001:db8:100:100::", "type": "IA_PD"}
        )
        prefix = Prefix.objects.get()
        self.assertEqual((str(prefix.prefix), prefix.vrf_id), (_PD_LABEL, self.vrf.pk))
        self.assertFalse(NbIP.objects.exists())
        link = IPAMOwnershipLink.objects.get()
        self.assertEqual((link.server_id, link.source, link.prefix_id), (self.server.pk, "lease-prefix", prefix.pk))
        self.assertContains(response, f'href="{prefix.get_absolute_url()}"')
        self.assertContains(response, _PD_LABEL)

    def test_a_delegated_prefix_sync_adopts_an_existing_prefix_of_the_sync_vrf(self):
        from ipam.models import Prefix

        existing = Prefix.objects.create(prefix=_PD_LABEL, vrf=self.vrf, description="Operator prefix")
        other_vrf = Prefix.objects.create(prefix=_PD_LABEL, description="Global table prefix")

        response, _kea = self._post(_PD_LABEL, _pd_record())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Prefix.objects.count(), 2)
        self.assertEqual(IPAMOwnershipLink.objects.get().prefix_id, existing.pk)
        self.assertFalse(IPAMOwnershipLink.objects.filter(prefix=other_vrf).exists())
        self.assertContains(response, f'href="{existing.get_absolute_url()}"')

    def test_an_address_sync_still_claims_an_ip_address_with_the_flags_off(self):
        from ipam.models import Prefix

        response, _kea = self._post(_PD_ADDRESS_LABEL, lease_record(_PD_ADDRESS_LABEL, subnet_id=10))

        self.assertEqual(response.status_code, 200, response.content)
        ip = NbIP.objects.get()
        self.assertEqual((str(ip.address), ip.vrf_id), ("2001:db8:1::10/64", self.vrf.pk))
        self.assertEqual(IPAMOwnershipLink.objects.get().source, "lease")
        self.assertFalse(Prefix.objects.exists())

    def test_ip_address_permissions_do_not_authorize_a_delegated_prefix_sync(self):
        self._login_with("add_ipaddress", "change_ipaddress")

        response, kea = self._post(_PD_LABEL, _pd_record())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(kea.commands(), [])
        self._assert_no_ipam_rows()

    def test_prefix_permissions_do_not_authorize_an_address_sync(self):
        self._login_with("add_prefix", "change_prefix")

        response, kea = self._post(_PD_ADDRESS_LABEL, lease_record(_PD_ADDRESS_LABEL, subnet_id=10))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(kea.commands(), [])
        self._assert_no_ipam_rows()

    def test_a_delegated_prefix_sync_needs_both_prefix_permissions(self):
        for codenames in (("add_prefix",), ("change_prefix",)):
            with self.subTest(codenames=codenames):
                self._login_with(*codenames)
                response, _kea = self._post(_PD_LABEL, _pd_record())
                self.assertEqual(response.status_code, 403)
                self._assert_no_ipam_rows()

    def test_prefix_permissions_authorize_a_delegated_prefix_sync(self):
        from ipam.models import Prefix

        self._login_with("add_prefix", "change_prefix")

        response, _kea = self._post(_PD_LABEL, _pd_record())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(str(Prefix.objects.get().prefix), _PD_LABEL)

    def test_a_lease_that_is_not_current_is_not_synchronized(self):
        import time

        expired = {"cltt": int(time.time()) - 7200, "valid_lft": 3600}
        cases = {
            "reclaimed prefix": (_PD_LABEL, _pd_record(state=2)),
            "released prefix": (_PD_LABEL, _pd_record(state=3)),
            "expired prefix": (_PD_LABEL, _pd_record(**expired)),
            "registered prefix": (_PD_LABEL, _pd_record(state=4)),
            "reclaimed address": (_PD_ADDRESS_LABEL, lease_record(_PD_ADDRESS_LABEL, subnet_id=10, state=2)),
            "expired address": (_PD_ADDRESS_LABEL, lease_record(_PD_ADDRESS_LABEL, subnet_id=10, **expired)),
        }
        for case, (label, record) in cases.items():
            with self.subTest(case=case):
                response, kea = self._post(label, record)
                self.assertContains(response, "not current", status_code=409)
                self.assertEqual(kea.commands(), ["lease6-get"])
                self._assert_no_ipam_rows()

    def test_a_dhcpv4_lease_that_is_not_current_is_not_synchronized(self):
        import time

        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        expired = {"cltt": int(time.time()) - 7200, "valid_lft": 3600}
        for case, changes in {"declined": {"state": 1}, "expired": expired}.items():
            with self.subTest(case=case):
                record = lease_record("192.0.2.10", subnet_id=10, **changes)
                responses = {
                    **_catalogue_responses(4, 10, "192.0.2.0/24"),
                    "lease4-get": {"result": 0, "arguments": record},
                }
                with stub_kea(responses):
                    response = self.client.post(url, {"ip_address": "192.0.2.10"})
                self.assertContains(response, "not current", status_code=409)
                self._assert_no_ipam_rows()

    def test_a_changed_prefix_length_is_not_synchronized(self):
        response, _kea = self._post(_PD_LABEL, _pd_record(prefix_len=60))

        self.assertContains(response, "The lease changed in Kea", status_code=409)
        self._assert_no_ipam_rows()

    def test_a_subnet_id_absent_from_the_catalogue_is_a_generic_error(self):
        response, _kea = self._post(_PD_LABEL, _pd_record(subnet_id=99))

        self.assertEqual(response.content, b"Sync error: see server logs for details.")
        self.assertEqual(response.status_code, 500)
        self._assert_no_ipam_rows()

    def test_an_unavailable_catalogue_is_a_generic_error_for_a_delegated_prefix(self):
        responses = {
            "lease6-get": _lease6_get(_pd_record()),
            "subnet6-list": {"result": 1, "text": "private diagnostic"},
            "config-get": {"result": 1, "text": "private diagnostic"},
        }
        with stub_kea(responses):
            response = self.client.post(self._url(), {"ip_address": _PD_LABEL})

        self.assertEqual(response.content, b"Sync error: see server logs for details.")
        self.assertEqual(response.status_code, 500)
        self._assert_no_ipam_rows()

    def test_an_invalid_selection_is_refused_before_kea_is_read(self):
        for label in ("2001:db8:100:101::/56", "not-an-address", "192.0.2.0/24", "2001:db8::/129"):
            with self.subTest(label=label):
                response, kea = self._post(label, _pd_record())
                self.assertEqual(response.status_code, 400)
                self.assertEqual(kea.commands(), [])
                self._assert_no_ipam_rows()


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation4SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4SyncView(_SyncViewBase):
    """POST to server_reservation4_sync creates/updates NetBox IP with status=reserved."""

    def setUp(self):
        super().setUp()
        self._start_stub(
            {
                **_catalogue_responses(4, 1, "10.0.0.0/24"),
                "reservation-get": _reservation_get(
                    "mock-res.local",
                    "10.0.0.50",
                    **{"hw-address": "aa:bb:cc:00:00:02"},
                ),
            }
        )

    def _url(self, include_identity: bool = True):
        url = reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk, 1])
        return f"{url}?identifier_type=hw-address&identifier=aa%3Abb%3Acc%3A00%3A00%3A02" if include_identity else url

    def test_returns_200_on_valid_post(self):
        response = self.client.post(self._url(), {"ip_address": "10.0.0.50", "hostname": "res-host"})
        self.assertEqual(response.status_code, 200)

    def test_creates_ip_with_reserved_status(self):

        self.client.post(self._url())
        ip = NbIP.objects.filter(address__net_host="10.0.0.50").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_sets_dns_name(self):
        # hostname in POST is ignored; dns_name comes from Kea reservation data (mock returns "mock-res.local")
        self.client.post(self._url())
        ip = NbIP.objects.filter(address__net_host="10.0.0.50").first()
        self.assertEqual(ip.dns_name, "mock-res.local")

    def test_response_contains_the_synchronization_badge(self):
        response = self.client.post(self._url())
        self.assertContains(response, "Synchronized 1/1")
        ip = NbIP.objects.get(address__net_host="10.0.0.50")
        self.assertContains(response, f'href="{ip.get_absolute_url()}"')
        self.assertNotContains(response, "hx-post")

    def test_returns_400_when_identity_missing(self):
        response = self.client.post(self._url(include_identity=False))
        self.assertEqual(response.status_code, 400)

    def test_validation_error_from_an_ipam_receiver_is_handled(self):
        """Keep a Django `ValidationError` raised during the IPAM write off the response.

        `dns_name` is written from the Kea hostname, and netbox-dns validates it on
        IPAddress save. Django's ValidationError is neither a ValueError nor a
        DatabaseError, so it escaped this handler while both sibling handlers caught it.
        """

        def reject(sender, **kwargs):
            raise ValidationError("dns_name is not valid for the configured zone.")

        pre_save.connect(reject, sender=NbIP)
        self.addCleanup(pre_save.disconnect, reject, sender=NbIP)

        with self.assertLogs("netbox_kea.views.sync_views", level="ERROR"):
            response = self.client.post(self._url())

        self.assertContains(response, "Reservation synchronization failed", status_code=500)
        self.assertEqual(NbIP.objects.count(), 0)

    def test_kea_error_returns_an_actionable_hint(self):
        """A Kea failure explains the safe operator action instead of hiding it in logs."""
        with stub_kea(
            {
                **_catalogue_responses(4, 1, "10.0.0.0/24"),
                "reservation-get": {"result": 2, "text": "unknown command 'reservation-get'"},
            }
        ):
            with self.assertLogs("netbox_kea.views.sync_views", level="ERROR"):
                response = self.client.post(self._url())

        self.assertContains(response, "The required hook library may not be loaded", status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation6SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6SyncView(_SyncViewBase):
    """POST to server_reservation6_sync creates/updates NetBox IP for IPv6 reservation."""

    def setUp(self):
        super().setUp()
        self._start_stub(
            {
                **_catalogue_responses(6, 1, "2001:db8:1::/64"),
                "reservation-get": _reservation_get(
                    "mock-v6res.local",
                    "2001:db8:1::50",
                    version=6,
                    duid="01:02:03:04",
                ),
            }
        )

    def _url(self):
        url = reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk, 1])
        return f"{url}?identifier_type=duid&identifier=01%3A02%3A03%3A04"

    def test_returns_200_on_valid_post(self):
        response = self.client.post(
            self._url(),
            {"ip_address": "2001:db8:1::50", "hostname": "v6res"},
        )
        self.assertEqual(response.status_code, 200)

    def test_creates_ip_with_reserved_status(self):

        self.client.post(self._url())
        ip = NbIP.objects.filter(address__net_host="2001:db8:1::50").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_multi_address_sync_reports_and_links_each_address_in_server_vrf(self):
        from ipam.models import VRF

        from netbox_kea.models import IPAMOwnershipLink

        vrf = VRF.objects.create(name="reservation-vrf")
        self.server.sync_vrf = vrf
        self.server.save()
        with stub_kea(
            {
                **_catalogue_responses(6, 1, "2001:db8:1::/64"),
                "reservation-get": _reservation_get(
                    "multi.example.com",
                    "2001:db8:1::50",
                    version=6,
                    duid="01:02:03:04",
                    **{"ip-addresses": ["2001:db8:1::50", "2001:db8:1::51"]},
                ),
            }
        ):
            response = self.client.post(self._url())
        self.assertContains(response, "Synchronized 2/2")
        for address in ("2001:db8:1::50", "2001:db8:1::51"):
            self.assertContains(response, address)
            ip = NbIP.objects.get(vrf=vrf, address__net_host=address)
            self.assertContains(response, ip.get_absolute_url())
            link = IPAMOwnershipLink.objects.get(ip_address=ip)
            self.assertEqual((link.server_id, link.family, link.source), (self.server.pk, 6, "reservation"))
        self.assertEqual(NbIP.objects.count(), 2)


# ─────────────────────────────────────────────────────────────────────────────
# TestReservationBulkSyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4BulkSyncView(_SyncViewBase):
    """POST to server_reservation4_bulk_sync syncs all reservations to NetBox."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def test_complete_sync_removes_only_its_stale_reservation_link(self):
        ip = NbIP.objects.create(
            address="198.18.0.10/24", status="active", description="[kea-sync: lease + reservation]"
        )
        other_server = _make_server(name="other-owner")
        links = [
            IPAMOwnershipLink.objects.create(
                server=owner,
                family=4,
                source=source,
                ip_address=ip,
                confirmation=next_confirmation_number(),
                facts={"hostname": "", "prefix_length": 24},
            )
            for owner, source in [(self.server, "lease"), (self.server, "reservation"), (other_server, "reservation")]
        ]
        with stub_kea({**_catalogue_responses(4, 1, "198.18.0.0/24"), "reservation-get-page": _res_page([])}):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(IPAMOwnershipLink.objects.values_list("pk", flat=True)), {links[0].pk, links[2].pk})
        ip.refresh_from_db()
        self.assertEqual(ip.status, "active")
        summary = " ".join(str(message) for message in django_messages.get_messages(response.wsgi_request))
        self.assertIn("0 created, 0 updated, 0 conflicts skipped, 1 stale links cleaned", summary)

    def test_redirects_after_success(self):
        hosts = [
            {
                "ip-address": "10.0.10.1",
                "hostname": "bulk-host",
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:10:01",
            }
        ]
        with stub_kea({**_catalogue_responses(4, 1, "10.0.0.0/8"), "reservation-get-page": _res_page(hosts)}):
            response = self.client.post(self._url(), follow=False)
        # Must redirect back to reservations page
        self.assertIn(response.status_code, [302, 303])

    def test_creates_netbox_ips_for_all_reservations(self):
        hosts = [
            {
                "ip-address": "10.0.11.1",
                "hostname": "bulk-1",
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:11:01",
            },
            {
                "ip-address": "10.0.11.2",
                "hostname": "bulk-2",
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:11:02",
            },
        ]
        with stub_kea({**_catalogue_responses(4, 1, "10.0.0.0/8"), "reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())
        self.assertTrue(NbIP.objects.filter(address__net_host="10.0.11.1").exists())
        self.assertTrue(NbIP.objects.filter(address__net_host="10.0.11.2").exists())

    def test_created_ips_have_reserved_status(self):
        hosts = [
            {
                "ip-address": "10.0.12.1",
                "hostname": "bulk-rsv",
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:12:01",
            }
        ]
        with stub_kea({**_catalogue_responses(4, 1, "10.0.0.0/8"), "reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())
        ip = NbIP.objects.filter(address__net_host="10.0.12.1").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_a_skipped_global_reservation_keeps_its_netbox_ip(self):
        """The bulk sync skips a Global Reservation, but it still owns its address.

        Sharing a hostname with an In-Subnet Reservation once put that address in the
        stale set, so the sync deleted an IP no Kea record had released.
        """
        NbIP.objects.create(
            address="10.0.13.2/32",
            status="reserved",
            dns_name="shared-bulk",
            description="[kea-sync: reservation]",
        )
        # A marker without an ownership link does not authorize cleanup.
        NbIP.objects.create(
            address="10.0.13.99/32",
            status="reserved",
            dns_name="shared-bulk",
            description="[kea-sync: reservation]",
        )
        hosts = [
            {
                "ip-address": "10.0.13.1",
                "hostname": "shared-bulk",
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:13:01",
            },
            {"ip-address": "10.0.13.2", "hostname": "shared-bulk", "subnet-id": 0, "flex-id": "global-bulk"},
        ]
        with stub_kea({**_catalogue_responses(4, 1, "10.0.0.0/8"), "reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())

        self.assertTrue(NbIP.objects.filter(address__net_host="10.0.13.1").exists())
        self.assertTrue(
            NbIP.objects.filter(address__net_host="10.0.13.2").exists(),
            "the skipped Global Reservation lost its address to stale-IP cleanup",
        )
        self.assertTrue(
            NbIP.objects.filter(address__net_host="10.0.13.99").exists(),
            "an unowned marker address must stay",
        )

    def test_malformed_snapshot_fails_closed_with_a_message(self):
        malformed_page = {
            "result": 0,
            "arguments": {"hosts": None, "next": {"from": 0, "source-index": 0}},
        }
        with stub_kea({**_catalogue_responses(4, 1, "10.0.0.0/8"), "reservation-get-page": malformed_page}):
            response = self.client.post(self._url(), follow=True)

        self.assertRedirects(
            response,
            reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk]),
        )
        self.assertContains(response, "Reservation phase incomplete; cleanup skipped")
        self.assertEqual(NbIP.objects.count(), 0)

    def test_distinguishes_quarantined_records_from_truncated_traversal(self):
        from django.contrib import messages as django_messages

        hosts = [
            {"subnet-id": 1, "remote-id": "relay-value"},
            *({"subnet-id": 0, "flex-id": f"global-{index}"} for index in range(_RESERVATION_PAGE_SIZE - 1)),
        ]
        pages = queued(
            _res_page(hosts, next_from=1, next_source=1),
            requests.ConnectionError("next page failed"),
        )
        with stub_kea({**_catalogue_responses(4, 1, "10.0.0.0/8"), "reservation-get-page": pages}):
            response = self.client.post(self._url())

        message = " ".join(str(item) for item in django_messages.get_messages(response.wsgi_request))
        self.assertIn("1 quarantined", message)
        self.assertNotIn("2 quarantined", message)
        self.assertIn("Reservation list was read only in part", message)

    def test_returns_404_for_nonexistent_server(self):
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[99999])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, [302, 403])


# ─────────────────────────────────────────────────────────────────────────────
# Issue #9: Authorization checks before IPAM sync mutations
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncViewPermissionChecks(_SyncViewBase):
    """Sync endpoints must reject users without IPAM write permissions."""

    def setUp(self):
        super().setUp()
        # Create a non-privileged user with no IPAM permissions
        self.limited_user = User.objects.create_user(
            username="limited_sync_user",
            email="limited@example.com",
            password="limitedpass",
        )

    def _login_limited(self):
        self.client.logout()
        self.client.force_login(self.limited_user)

    def test_lease4_sync_requires_ipam_add_permission(self):
        self._login_limited()
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        response = self.client.post(url, {"ip_address": "192.168.99.1"})
        self.assertEqual(response.status_code, 403)

    def test_reservation4_sync_requires_ipam_add_permission(self):
        self._login_limited()
        url = reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk, 1])
        response = self.client.post(url, {"ip_address": "192.168.99.2"})
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_still_sync(self):
        # self.user is superuser — should succeed as before
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        stub = {
            **_catalogue_responses(4, 1, "192.168.99.0/24"),
            "lease4-get": _lease_get("mock-host.local", **{"hw-address": "aa:bb:cc:00:00:01", "valid-lft": 86400}),
        }
        with stub_kea(stub):
            response = self.client.post(url, {"ip_address": "192.168.99.3"})
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation6BulkSyncView  (issue #13)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6BulkSyncView(_SyncViewBase):
    """POST to server_reservation6_bulk_sync syncs all v6 reservations to NetBox."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[self.server.pk])

    def test_post_bulk_syncs_v6_reservations(self):
        """Bulk sync v6 reservation creates a reserved IPAddress masked by its catalogue Subnet."""
        hosts = [
            {
                "subnet-id": 1,
                "duid": "00:01:aa:bb",
                "ip-addresses": ["2001:db8::1"],
                "hostname": "host-v6",
            }
        ]
        with stub_kea({**_catalogue_responses(6, 1, "2001:db8::/32"), "reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())
        ip = NbIP.objects.filter(address__net_host="2001:db8::1").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")
        self.assertIn("/32", str(ip.address))

    def test_post_unauthenticated_redirects(self):
        self.client.logout()
        response = self.client.post(self._url(), content_type="application/json")
        self.assertEqual(response.status_code, 302)

    def test_post_nonexistent_server_returns_404(self):
        url = reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[99999])
        response = self.client.post(url, content_type="application/json")
        self.assertEqual(response.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# Issue #64: bulk-sync conflict protection + live IP-check endpoint
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationBulkSyncConflictProtection(_SyncViewBase):
    """Bulk reservation sync must not overwrite foreign NetBox IPs; conflicts are counted."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def test_foreign_ip_not_overwritten_and_counted(self):
        from django.contrib import messages as django_messages

        NbIP.objects.create(address="10.0.20.5/32", status="active", description="Router loopback")
        hosts = [
            {
                "ip-address": "10.0.20.5",
                "hostname": "foreign",
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:20:05",
            }
        ]
        # follow=True lands on the reservations list, which re-drains reservation-get-page
        # and enriches with lease4-get-by-state per subnet.
        stub = {
            **_catalogue_responses(4, 1, "10.0.0.0/8"),
            "list-commands": _reservation_mutation_commands(),
            "reservation-get-page": _res_page(hosts),
            "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
        }
        with stub_kea(stub):
            response = self.client.post(self._url(), follow=True)

        # Foreign IP left untouched.
        ip = NbIP.objects.get(address="10.0.20.5/32")
        self.assertEqual(ip.status, "active")
        self.assertEqual(ip.description, "Router loopback")
        # Conflict surfaced in the summary message.
        msgs = [str(m) for m in django_messages.get_messages(response.wsgi_request)]
        self.assertTrue(any("1 conflicts skipped" in m for m in msgs), msgs)

    def test_managed_ip_still_synced_alongside_conflict(self):
        NbIP.objects.create(address="10.0.20.6/32", status="active", description="Router loopback")
        hosts = [
            {
                "ip-address": "10.0.20.6",
                "hostname": "foreign",
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:20:06",
            },
            {
                "ip-address": "10.0.20.7",
                "hostname": "managed",
                "subnet-id": 1,
                "hw-address": "aa:bb:cc:00:20:07",
            },
        ]
        stub = {
            **_catalogue_responses(4, 1, "10.0.0.0/8"),
            "list-commands": _reservation_mutation_commands(),
            "reservation-get-page": _res_page(hosts),
            "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
        }
        with stub_kea(stub):
            self.client.post(self._url(), follow=True)

        # Foreign untouched, the other reservation claimed normally.
        self.assertEqual(NbIP.objects.get(address="10.0.20.6/32").status, "active")
        claimed = NbIP.objects.filter(address__net_host="10.0.20.7").first()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, "reserved")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationCheckNetboxIPView(_SyncViewBase):
    """GET endpoint that advises whether an IP already exists in NetBox IPAM."""

    def _url(self):
        return reverse("plugins:netbox_kea:reservation_check_ip", args=[self.server.pk])

    def test_empty_when_ip_missing(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_empty_when_ip_invalid(self):
        response = self.client.get(self._url(), {"ip": "not-an-ip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_empty_when_ip_not_in_netbox(self):
        response = self.client.get(self._url(), {"ip": "10.0.40.99"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_info_alert_for_kea_managed_ip(self):
        NbIP.objects.create(address="10.0.40.1/24", status="reserved", description="[kea-sync: reservation]")
        response = self.client.get(self._url(), {"ip": "10.0.40.1"})
        body = response.content.decode()
        self.assertIn("alert-info", body)
        self.assertIn("Already in NetBox IPAM", body)

    def test_warning_for_blank_description_ip_leaves_row_and_links_unchanged(self):
        ip = NbIP.objects.create(address="198.18.0.2/24", status="active", description="")
        before_row = NbIP.objects.values().get(pk=ip.pk)
        before_links = list(IPAMOwnershipLink.objects.filter(ip_address=ip).order_by("pk").values())
        response = self.client.get(self._url(), {"ip": "198.18.0.2"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alert-warning")
        self.assertContains(response, "This IP exists in NetBox and was <strong>not</strong> created by Kea sync")
        self.assertContains(response, "Syncing will overwrite this entry.")
        self.assertNotContains(response, "alert-info")
        self.assertNotContains(response, "Already in NetBox IPAM")
        self.assertEqual(NbIP.objects.values().get(pk=ip.pk), before_row)
        self.assertEqual(list(IPAMOwnershipLink.objects.filter(ip_address=ip).order_by("pk").values()), before_links)

    def test_warning_alert_for_foreign_ip(self):
        NbIP.objects.create(address="10.0.40.3/24", status="active", description="Router loopback")
        response = self.client.get(self._url(), {"ip": "10.0.40.3"})
        body = response.content.decode()
        self.assertIn("alert-warning", body)
        self.assertIn("not", body.lower())
        self.assertIn("Router loopback", body)

    def test_matches_noncanonical_ipv6_query(self):
        """A non-canonical IPv6 query (expanded/zero-padded) still matches the
        canonical stored record — the view normalizes the input before the lookup.

        The DB stores ``2001:db8::5/64``; querying with the fully-expanded form
        must canonicalize to the same value so the conflict advisory still fires.
        Without normalization the ``address__net_host`` lookup would miss it and
        silently suppress the warning.
        """
        NbIP.objects.create(address="2001:db8::5/64", status="active", description="Router loopback")
        response = self.client.get(self._url(), {"ip": "2001:0db8:0000:0000:0000:0000:0000:0005"})
        body = response.content.decode()
        self.assertIn("alert-warning", body)
        self.assertIn("Router loopback", body)

    def test_404_for_nonexistent_server(self):
        url = reverse("plugins:netbox_kea:reservation_check_ip", args=[99999])
        response = self.client.get(url, {"ip": "10.0.40.1"})
        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(self._url(), {"ip": "10.0.40.1"})
        self.assertIn(response.status_code, [302, 403])

    def test_respects_ipam_view_permission(self):
        """A user who can view the server but not IPAM IPs must get an empty advisory.

        The advisory leaks an IP's status/description/assignment, so the lookup
        must be scoped with ``.restrict(user, "view")``. With an unrestricted
        lookup this user would see the foreign-IP warning for an IP they have no
        permission to view.
        """
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        NbIP.objects.create(address="10.0.40.7/24", status="active", description="Router loopback")

        limited = User.objects.create_user(username="limited_ipcheck", password="pass")
        # Grant server view but deliberately NO ipam.view_ipaddress permission.
        perm = ObjectPermission.objects.create(name="view-server-only-ipcheck", actions=["view"])
        perm.object_types.add(ContentType.objects.get_for_model(Server))
        perm.users.add(limited)
        self.client.force_login(limited)

        response = self.client.get(self._url(), {"ip": "10.0.40.7"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_advisory_shown_when_user_has_ipam_view_permission(self):
        """The same lookup still renders the advisory once the user can view IPAM IPs."""
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress as IpamIP
        from users.models import ObjectPermission

        NbIP.objects.create(address="10.0.40.8/24", status="active", description="Router loopback")

        limited = User.objects.create_user(username="limited_ipcheck_ok", password="pass")
        server_perm = ObjectPermission.objects.create(name="view-server-ipcheck-ok", actions=["view"])
        server_perm.object_types.add(ContentType.objects.get_for_model(Server))
        server_perm.users.add(limited)
        ip_perm = ObjectPermission.objects.create(name="view-ipam-ipcheck-ok", actions=["view"])
        ip_perm.object_types.add(ContentType.objects.get_for_model(IpamIP))
        ip_perm.users.add(limited)
        self.client.force_login(limited)

        response = self.client.get(self._url(), {"ip": "10.0.40.8"})
        body = response.content.decode()
        self.assertIn("alert-warning", body)
        self.assertIn("Router loopback", body)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSyncEventDispatch(TransactionTestCase):
    """A claim row is a unit at the top level of the request, so its events dispatch after its own commit."""

    def test_a_committed_claim_whose_events_fail_to_dispatch_is_not_reported_as_a_sync_error(self):
        from ipam.models import Prefix

        from netbox_kea.event_scope import _NESTED_TRACKING, EventDispatchError

        if not _NESTED_TRACKING:
            self.skipTest("Before NetBox 4.6.9 a claim row is a plain transaction and dispatches with the request")
        self.client.force_login(User.objects.create_superuser(username="dispatch-user", password="dispatch-pass"))
        server = _make_server()
        responses = {**_catalogue_responses_for_subnets(6, _PD_SUBNETS), "lease6-get": _lease6_get(_pd_record())}
        url = reverse("plugins:netbox_kea:server_lease6_sync", args=[server.pk])
        with (
            override_settings(EVENTS_PIPELINE=["netbox_kea.tests.test_event_scope.fail_dispatch"]),
            stub_kea(responses),
            self.assertRaises(EventDispatchError),
        ):
            self.client.post(url, {"ip_address": _PD_LABEL})
        self.assertTrue(Prefix.objects.filter(prefix=_PD_LABEL).exists())
