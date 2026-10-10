# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""A manual Sync needs an unconstrained NetBox grant for each write that it makes.

The automatic sync is not limited by permission constraints, so a manual Sync makes the same writes or none.
These tests drive the real views with real ObjectPermission rows. Only the Kea transport is stubbed.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dcim.models import MACAddress
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape
from ipam.models import IPAddress, IPRange, Prefix
from users.models import Group, ObjectPermission

from netbox_kea import ipam_reconciliation
from netbox_kea.ipam_reconciliation import LEASE, RESERVATION
from netbox_kea.models import Server
from netbox_kea.views import dhcp_plugin_sync as dps

from .kea_stub import _catalogue_responses, _res_get, _res_page, _subnet_list, complete_lease, stub_kea
from .test_reservation_mutation_views import _mutation_responses
from .test_sync_views import _lease_get
from .test_views_dhcp_plugin import _sync_responses
from .test_views_leases import _lease_stub, _reservation_stub
from .utils import _PLUGINS_CONFIG, _make_db_server, plugins_config

User = get_user_model()

_ADDRESS_WRITES = "ipam.add_ipaddress, ipam.change_ipaddress, dcim.add_macaddress, dcim.change_macaddress"
_IPAM_IMPORT_WRITES = (
    "ipam.add_prefix, ipam.change_prefix, ipam.add_iprange, ipam.change_iprange, "
    "ipam.add_ipaddress, ipam.change_ipaddress, dcim.add_macaddress, dcim.change_macaddress"
)
_PLUGIN_IMPORT_WRITES = (
    "netbox_dhcp.add_dhcpserver, netbox_dhcp.change_dhcpserver, netbox_dhcp.add_optiondefinition, "
    "netbox_dhcp.add_option, netbox_dhcp.change_option, netbox_dhcp.add_clientclass, netbox_dhcp.change_clientclass, "
    "netbox_dhcp.add_subnet, netbox_dhcp.change_subnet, netbox_dhcp.add_pool, "
    "netbox_dhcp.add_hostreservation, netbox_dhcp.change_hostreservation"
)
_IMPORT_WRITES = f"{_IPAM_IMPORT_WRITES}, {_PLUGIN_IMPORT_WRITES}"
_PLUGIN_MODELS = ("DHCPServer", "OptionDefinition", "Option", "ClientClass", "Subnet", "Pool", "HostReservation")
_CONSTRAINT = {"vrf__name": "lab"}


def _reason(names: str) -> str:
    noun = "permission" if "," not in names else "permissions"
    return (
        f"Manual Sync needs unconstrained {names} {noun}: the automatic sync is not limited by permission constraints."
    )


def _grant(models, actions, *, user=None, group=None, constraints=None, enabled=True) -> ObjectPermission:
    permission = ObjectPermission.objects.create(
        name=f"grant-{ObjectPermission.objects.count()}",
        actions=list(actions),
        constraints=constraints,
        enabled=enabled,
    )
    permission.object_types.add(*(ContentType.objects.get_for_model(model) for model in models))
    if user is not None:
        permission.users.add(user)
    if group is not None:
        permission.groups.add(group)
    return permission


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _PermissionTestBase(TestCase):
    """A user who may view and change the Server; each test grants the IPAM and DCIM permissions."""

    def setUp(self):
        self.server = _make_db_server(name="manual-sync-kea")
        self.user = User.objects.create_user(username="sync-operator")
        _grant([Server], ["view", "change"], user=self.user)
        self.client.force_login(self.user)

    def _grant_address_writes(self, **kwargs):
        return _grant([IPAddress, MACAddress], ["add", "change"], **kwargs)

    def _assert_refused(self, response, names: str):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content.decode(), _reason(names))
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(MACAddress.objects.exists())


class TestLeaseRowSync(_PermissionTestBase):
    """The lease row Sync claims one lease: an IP address and its MAC address."""

    def _post(self):
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        stub = {
            **_catalogue_responses(4, 1, "192.168.99.0/24"),
            "lease4-get": _lease_get("host.example", **{"hw-address": "aa:bb:cc:00:00:01"}),
        }
        with stub_kea(stub):
            return self.client.post(url, {"ip_address": "192.168.99.3"})

    def _assert_synced(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertTrue(IPAddress.objects.filter(address__net_host="192.168.99.3").exists())
        self.assertTrue(MACAddress.objects.filter(mac_address="aa:bb:cc:00:00:01").exists())

    def test_an_active_superuser_syncs(self):
        self.client.force_login(User.objects.create_superuser(username="sync-admin"))
        self._assert_synced(self._post())

    def test_an_unconstrained_user_grant_syncs(self):
        self._grant_address_writes(user=self.user)
        self._assert_synced(self._post())

    def test_an_unconstrained_group_grant_syncs(self):
        group = Group.objects.create(name="ipam-writers")
        self.user.groups.add(group)
        self._grant_address_writes(group=group)
        self._assert_synced(self._post())

    def test_a_constrained_grant_is_refused_with_the_reason(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        self._assert_refused(self._post(), _ADDRESS_WRITES)

    def test_the_reason_names_only_the_constrained_permissions(self):
        _grant([IPAddress], ["add", "change"], user=self.user, constraints=[_CONSTRAINT])
        _grant([MACAddress], ["add", "change"], user=self.user)
        self._assert_refused(self._post(), "ipam.add_ipaddress, ipam.change_ipaddress")

    def test_a_missing_mac_permission_is_refused(self):
        _grant([IPAddress], ["add", "change"], user=self.user)
        self._assert_refused(self._post(), "dcim.add_macaddress, dcim.change_macaddress")

    def test_a_disabled_grant_does_not_count(self):
        self._grant_address_writes(user=self.user, enabled=False)
        self._assert_refused(self._post(), _ADDRESS_WRITES)

    def test_an_unconstrained_grant_beside_a_constrained_one_syncs(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        self._grant_address_writes(user=self.user)
        self._assert_synced(self._post())

    def test_an_empty_constraint_set_in_the_list_is_no_constraint(self):
        self._grant_address_writes(user=self.user, constraints=[_CONSTRAINT, {}])
        self._assert_synced(self._post())

    @override_settings(
        DEFAULT_PERMISSIONS={
            "ipam.add_ipaddress": None,
            "ipam.change_ipaddress": (),
            "dcim.add_macaddress": None,
            "dcim.change_macaddress": None,
        }
    )
    def test_unconstrained_default_permissions_count(self):
        self._assert_synced(self._post())

    @override_settings(
        DEFAULT_PERMISSIONS={
            "ipam.add_ipaddress": (_CONSTRAINT,),
            "ipam.change_ipaddress": None,
            "dcim.add_macaddress": None,
            "dcim.change_macaddress": None,
        }
    )
    def test_a_constrained_default_permission_is_refused(self):
        self._assert_refused(self._post(), "ipam.add_ipaddress")


class TestLeaseSyncControls(_PermissionTestBase):
    """The lease table shows Sync disabled, with the reason, to a user that the rule refuses."""

    _LEASE4 = complete_lease({"ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1})

    def _search(self):
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        responses = {
            "subnet4-list": _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}]),
            "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
            "reservation-get": {"result": 3},
        }
        with _reservation_stub(4, responses):
            return self.client.get(url, {"by": "ip", "q": "10.0.0.5"}, HTTP_HX_REQUEST="true")

    def test_a_constrained_user_sees_a_disabled_sync_with_the_reason(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        response = self._search()
        self.assertEqual(response.status_code, 200)
        sync_url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        self.assertNotContains(response, f'hx-post="{sync_url}"')
        self.assertContains(response, f'title="{_reason(_ADDRESS_WRITES)}"')
        self.assertRegex(response.content.decode(), r'<button[^>]*\bdisabled\b[^>]*>\s*<i class="mdi mdi-sync"')

    def test_an_unconstrained_user_sees_the_sync_button(self):
        self._grant_address_writes(user=self.user)
        response = self._search()
        sync_url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        self.assertContains(response, f'hx-post="{sync_url}"')
        self.assertNotContains(response, "Manual Sync needs")


class TestLeaseAddSync(_PermissionTestBase):
    """The lease add form syncs the created lease only for a user that the rule allows."""

    _SUBNETS4 = _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}])

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])

    def test_the_form_shows_the_checkbox_disabled_with_the_reason(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.content.decode(), r'<input[^>]*name="sync_to_netbox"[^>]*disabled')
        self.assertContains(response, _reason(_ADDRESS_WRITES))
        self.assertNotContains(response, "checkbox below")

    def test_an_unconstrained_user_is_offered_the_checkbox(self):
        self._grant_address_writes(user=self.user)
        response = self.client.get(self._url())
        self.assertNotRegex(response.content.decode(), r'<input[^>]*name="sync_to_netbox"[^>]*disabled')
        self.assertContains(response, "checkbox below")

    def test_a_constrained_user_creates_the_lease_without_a_sync(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        data = {"ip_address": "10.0.0.200", "subnet_id": "1", "hw_address": "aa:bb:cc:dd:ee:ff", "sync_to_netbox": "on"}
        readback = {
            "result": 0,
            "arguments": complete_lease(
                {"ip-address": "10.0.0.200", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1}
            ),
        }
        responses = {"lease4-add": {"result": 0}, "lease4-get": readback, "subnet4-list": self._SUBNETS4}
        with _lease_stub(responses) as kea:
            response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        # The view reads the created Lease back for the lease_added signal, and claims nothing.
        self.assertEqual(kea.commands(), ["lease4-add", "lease4-get"])
        self.assertFalse(IPAddress.objects.exists())
        self.assertIn(
            f"Lease created, but it was not synced to NetBox. {_reason(_ADDRESS_WRITES)}",
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_an_unconstrained_user_syncs_the_created_lease(self):
        self._grant_address_writes(user=self.user)
        data = {"ip_address": "10.0.0.200", "subnet_id": "1", "hw_address": "aa:bb:cc:dd:ee:ff", "sync_to_netbox": "on"}
        readback = {
            "result": 0,
            "arguments": complete_lease(
                {"ip-address": "10.0.0.200", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1}
            ),
        }
        responses = {"lease4-add": {"result": 0}, "lease4-get": readback, "subnet4-list": self._SUBNETS4}
        with _lease_stub(responses):
            response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.200").exists())


class TestReservationRowSync(_PermissionTestBase):
    """The Reservation row Sync all claims each address of one Reservation and its MAC address."""

    def _post(self):
        url = reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk, 1])
        stub = {
            **_catalogue_responses(4, 1, "10.0.0.0/24"),
            "reservation-get": _res_get({"subnet-id": 1, "ip-address": "10.0.0.50", "hw-address": "aa:bb:cc:00:00:02"}),
        }
        with stub_kea(stub):
            return self.client.post(f"{url}?identifier_type=hw-address&identifier=aa%3Abb%3Acc%3A00%3A00%3A02")

    def test_a_constrained_grant_is_refused_with_the_reason(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        self._assert_refused(self._post(), _ADDRESS_WRITES)

    def test_an_unconstrained_grant_syncs(self):
        self._grant_address_writes(user=self.user)
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.50", status="reserved").exists())


class TestReservationSyncControls(_PermissionTestBase):
    """The Reservation tables show both Sync controls disabled, with the reason, to a refused user."""

    _HOSTS = [{"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]

    def _responses(self):
        return {
            **_catalogue_responses(4, 1, "198.18.0.0/24"),
            "reservation-get-page": _res_page(self._HOSTS),
            "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
        }

    def test_the_server_tab_shows_disabled_controls_with_the_reason(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        with stub_kea(self._responses()):
            response = self.client.get(reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk]))
        self.assertEqual(response.status_code, 200)
        row = response.context["table"].data.data[0]
        self.assertIsNone(row["sync_url"])
        self.assertEqual(row["sync_refusal"], _reason(_ADDRESS_WRITES))
        self.assertIsNone(response.context["bulk_sync_url"])
        self.assertEqual(response.context["bulk_sync_refusal"], _reason(_ADDRESS_WRITES))
        body = response.content.decode()
        self.assertEqual(body.count(f'title="{_reason(_ADDRESS_WRITES)}"'), 2)
        self.assertIn("Sync All to NetBox", body)
        self.assertNotIn(reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk]), body)

    def test_the_combined_tab_shows_a_disabled_sync_with_the_reason(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        url = reverse("plugins:netbox_kea:combined_reservations4") + f"?server={self.server.pk}"
        with stub_kea(self._responses()):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'title="{_reason(_ADDRESS_WRITES)}"')
        self.assertNotContains(
            response, reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk, 1])
        )

    def test_an_unconstrained_user_sees_both_sync_controls(self):
        self._grant_address_writes(user=self.user)
        with stub_kea(self._responses()):
            response = self.client.get(reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk]))
        self.assertIsNotNone(response.context["table"].data.data[0]["sync_url"])
        self.assertIsNotNone(response.context["bulk_sync_url"])
        self.assertNotContains(response, "Manual Sync needs")


class TestReservationAddSync(_PermissionTestBase):
    """The Reservation add form syncs the new Reservation only for a user that the rule allows."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    def test_the_form_shows_the_checkbox_disabled_with_the_reason(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        with stub_kea(_mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.content.decode(), r'<input[^>]*name="sync_to_netbox"[^>]*disabled')
        self.assertContains(response, _reason(_ADDRESS_WRITES))
        self.assertNotContains(response, "checkbox below")

    def test_a_constrained_user_creates_the_reservation_without_a_sync(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        raw = {"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        responses.update({"reservation-add": {"result": 0}, "reservation-get": _res_get(raw)})
        data = {
            "subnet_cidr": "198.18.0.0/24",
            "ip_address": "198.18.0.20",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "sync_to_netbox": "on",
        }
        with stub_kea(responses) as kea:
            response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("reservation-add"), 1)
        self.assertFalse(IPAddress.objects.exists())
        self.assertIn(
            f"Reservation created, but it was not synced to NetBox. {_reason(_ADDRESS_WRITES)}",
            [str(message) for message in get_messages(response.wsgi_request)],
        )


class TestBulkReservationSync(_PermissionTestBase):
    """Bulk Reservation Sync runs a Reservation phase and its stale cleanup."""

    def _post(self):
        hosts = [{"ip-address": "10.0.11.1", "hostname": "bulk-1", "subnet-id": 1, "hw-address": "aa:bb:cc:00:11:01"}]
        with stub_kea({**_catalogue_responses(4, 1, "10.0.0.0/8"), "reservation-get-page": _res_page(hosts)}):
            return self.client.post(reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk]))

    def test_a_constrained_grant_is_refused_with_the_reason(self):
        self._grant_address_writes(user=self.user, constraints=_CONSTRAINT)
        self._assert_refused(self._post(), _ADDRESS_WRITES)

    @override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
    def test_no_delete_permission_is_needed_because_the_cleanup_cannot_delete(self):
        self._grant_address_writes(user=self.user)
        response = self._post()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.11.1", status="reserved").exists())


@override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
class TestReconcilePermissions(TestCase):
    """A reconcile call needs delete only when its stale cleanup can remove the last link of an IP address."""

    def setUp(self):
        self.server = _make_db_server(name="reconcile-permissions-kea")

    def test_complete_lease_and_reservation_phases_need_delete_in_the_remove_mode(self):
        self.assertIn(
            "ipam.delete_ipaddress", ipam_reconciliation.reconcile_permissions(self.server, LEASE, RESERVATION)
        )

    def test_a_lease_phase_needs_delete_when_the_server_does_not_sync_reservations(self):
        self.server.sync_reservations_enabled = False
        self.assertIn("ipam.delete_ipaddress", ipam_reconciliation.reconcile_permissions(self.server, LEASE))

    def test_a_lease_phase_alone_cannot_delete_while_the_server_syncs_reservations(self):
        self.server.sync_reservations_enabled = True
        self.assertNotIn("ipam.delete_ipaddress", ipam_reconciliation.reconcile_permissions(self.server, LEASE))

    def test_a_reservation_phase_alone_cannot_delete(self):
        self.assertEqual(
            ipam_reconciliation.reconcile_permissions(self.server, RESERVATION),
            ("ipam.add_ipaddress", "ipam.change_ipaddress", "dcim.add_macaddress", "dcim.change_macaddress"),
        )

    @override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="deprecate"))
    def test_the_deprecate_mode_never_deletes(self):
        self.assertNotIn(
            "ipam.delete_ipaddress", ipam_reconciliation.reconcile_permissions(self.server, LEASE, RESERVATION)
        )


class TestDhcpPluginSync(_PermissionTestBase):
    """The DHCP plugin import claims Prefixes, IP Ranges, IP addresses and MAC addresses."""

    def setUp(self):
        super().setUp()
        self.server.sync_dhcp_plugin_enabled = True
        self.server.dhcp6 = False
        self.server.save(update_fields=["sync_dhcp_plugin_enabled", "dhcp6"])
        self.url = reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[self.server.pk])

    def _post(self):
        conf = {4: {"subnet4": [{"id": 1, "subnet": "10.88.0.0/24", "pools": [{"pool": "10.88.0.10-10.88.0.99"}]}]}}
        with (
            patch.object(dps.dhcp_plugin, "is_available", return_value=True, autospec=True),
            stub_kea(_sync_responses(conf)),
        ):
            return self.client.post(self.url)

    def _assert_import_refused(self, response, reason: str):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content.decode(), reason)
        self.assertFalse(Prefix.objects.exists())
        self.assertFalse(IPRange.objects.exists())

    def test_a_constrained_grant_is_refused_with_the_reason(self):
        _grant([Prefix, IPRange, IPAddress, MACAddress], ["add", "change"], user=self.user, constraints=_CONSTRAINT)
        self._assert_import_refused(self._post(), _reason(_IMPORT_WRITES))

    def test_address_grants_alone_are_refused(self):
        self._grant_address_writes(user=self.user)
        self._assert_import_refused(
            self._post(),
            _reason(
                "ipam.add_prefix, ipam.change_prefix, ipam.add_iprange, ipam.change_iprange, " + _PLUGIN_IMPORT_WRITES
            ),
        )

    def test_ipam_grants_alone_are_refused_for_the_dhcp_plugin_writes(self):
        _grant([Prefix, IPRange, IPAddress, MACAddress], ["add", "change"], user=self.user)
        self._assert_import_refused(self._post(), _reason(_PLUGIN_IMPORT_WRITES))

    def test_a_user_who_may_not_change_the_server_is_refused(self):
        viewer = User.objects.create_user(username="dhcp-viewer")
        _grant([Server], ["view"], user=viewer)
        _grant([Prefix, IPRange, IPAddress, MACAddress], ["add", "change"], user=viewer)
        self.client.force_login(viewer)
        self._assert_import_refused(self._post(), "Sync to DHCP plugin needs change permission on this Server.")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDhcpPluginSyncWithThePlugin(_PermissionTestBase):
    """The import needs an unconstrained grant of each DHCP plugin model that it writes."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed("netbox_dhcp"):
            raise unittest.SkipTest("netbox_dhcp not installed")
        super().setUpClass()

    def setUp(self):
        super().setUp()
        self.server.sync_dhcp_plugin_enabled = True
        self.server.dhcp6 = False
        self.server.save(update_fields=["sync_dhcp_plugin_enabled", "dhcp6"])
        _grant([Prefix, IPRange, IPAddress, MACAddress], ["add", "change"], user=self.user)

    def _post(self):
        conf = {4: {"subnet4": [{"id": 1, "subnet": "10.88.0.0/24", "pools": [{"pool": "10.88.0.10-10.88.0.99"}]}]}}
        with stub_kea(_sync_responses(conf)):
            return self.client.post(reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[self.server.pk]))

    def test_a_constrained_dhcp_plugin_grant_is_refused(self):
        _grant(_plugin_models(), ["add", "change"], user=self.user, constraints={"name": "lab"})
        response = self._post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content.decode(), _reason(_PLUGIN_IMPORT_WRITES))
        self.assertFalse(apps.get_model("netbox_dhcp", "Subnet").objects.exists())

    def test_unconstrained_grants_import_the_config(self):
        _grant(_plugin_models(), ["add", "change"], user=self.user)
        response = self._post()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(apps.get_model("netbox_dhcp", "Subnet").objects.filter(prefix__prefix="10.88.0.0/24").exists())
        self.assertTrue(apps.get_model("netbox_dhcp", "Pool").objects.exists())


def _plugin_models():
    return [apps.get_model("netbox_dhcp", name) for name in _PLUGIN_MODELS]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestDhcpPluginSyncControl(_PermissionTestBase):
    """The DHCP plugin tab shows the import button disabled, with the reason, to a refused user."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed("netbox_dhcp"):
            raise unittest.SkipTest("netbox_dhcp not installed")
        super().setUpClass()

    def _get(self):
        self.server.sync_dhcp_plugin_enabled = True
        self.server.dhcp6 = False
        self.server.save(update_fields=["sync_dhcp_plugin_enabled", "dhcp6"])
        conf = {4: {"subnet4": [{"id": 1, "subnet": "10.88.0.0/24"}]}}
        with stub_kea(_sync_responses(conf)):
            return self.client.get(reverse("plugins:netbox_kea:server_dhcp_plugin", args=[self.server.pk]))

    def test_a_constrained_user_sees_a_disabled_button_with_the_reason(self):
        _grant([Prefix, IPRange, IPAddress, MACAddress], ["add", "change"], user=self.user, constraints=_CONSTRAINT)
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'title="{escape(_reason(_IMPORT_WRITES))}"')
        self.assertContains(response, "Sync to DHCP plugin now")
        self.assertNotContains(response, reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[self.server.pk]))

    def test_an_unconstrained_user_sees_the_import_form(self):
        _grant([Prefix, IPRange, IPAddress, MACAddress, *_plugin_models()], ["add", "change"], user=self.user)
        response = self._get()
        self.assertContains(response, reverse("plugins:netbox_kea:server_dhcp_plugin_sync", args=[self.server.pk]))
        self.assertNotContains(response, "Manual Sync needs")
