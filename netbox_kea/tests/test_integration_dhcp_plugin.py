# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the optional netbox_dhcp adapter (real DB + real plugin).

Gated on the plugin being installed: netbox-kea's own CI matrix does not install
``netbox_dhcp``, so these run only where it is present (e.g. the dev container).
Only the Kea HTTP boundary is bypassed — we feed a ``config-get``-shaped dict
directly; the ORM, IPAM/DCIM models, and the ``netbox_dhcp`` models are all real.
"""

from __future__ import annotations

from django.apps import apps
from django.test import TestCase, override_settings, tag
from django.utils import timezone

from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config

from .utils import _make_db_server

DHCP_PLUGIN = "netbox_dhcp"
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


def _conf_v4():
    return {
        "subnet4": [
            {
                "id": 1,
                "subnet": "10.99.0.0/24",
                "pools": [{"pool": "10.99.0.10-10.99.0.100"}],
                "reservations": [
                    {"hw-address": "aa:bb:cc:dd:ee:01", "ip-address": "10.99.0.50", "hostname": "res-host"}
                ],
            }
        ]
    }


def _conf_v6():
    return {
        "subnet6": [
            {
                "id": 1,
                "subnet": "2001:db8:99::/64",
                "pools": [{"pool": "2001:db8:99::10-2001:db8:99::100"}],
                "reservations": [{"duid": "01:02:03:04:05", "ip-addresses": ["2001:db8:99::50"], "hostname": "res6"}],
            }
        ]
    }


@tag("dhcp_plugin")
@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class DhcpPluginAdapterTest(TestCase):
    """Importing a Kea config into netbox_dhcp via the guarded adapter."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise cls.skipException(f"{DHCP_PLUGIN} not installed")
        super().setUpClass()

    def setUp(self):
        self.server = _make_db_server(name=f"kea-int-{timezone.now().timestamp()}")
        from netbox_kea.integrations import dhcp_plugin

        self.adapter = dhcp_plugin

    # ── basic import ────────────────────────────────────────────────────────

    def test_v4_import_creates_subnet_pool_reservation_sharing_ipam(self):
        from dcim.models import MACAddress
        from ipam.models import IPAddress, IPRange, Prefix

        from netbox_kea.models import KeaDhcpLink

        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        Pool = apps.get_model(DHCP_PLUGIN, "Pool")
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")

        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(_conf_v4(), 4))

        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(summary.subnets_created, 1)
        self.assertEqual(summary.pools_created, 1)
        self.assertEqual(summary.reservations_created, 1)

        # Subnet linked by Kea identity, sharing the IPAM Prefix the sync owns.
        link = KeaDhcpLink.objects.get(server=self.server, family=4, kea_subnet_id=1)
        subnet = link.sys4_object
        self.assertIsInstance(subnet, Subnet)
        self.assertEqual(str(subnet.prefix.prefix), "10.99.0.0/24")
        self.assertEqual(subnet.prefix, Prefix.objects.get(prefix="10.99.0.0/24"))
        self.assertIsNone(subnet.shared_network)

        # Pool shares the IPAM IPRange.
        pool = Pool.objects.get(subnet=subnet)
        self.assertEqual(pool.ip_range, IPRange.objects.get(start_address="10.99.0.10/24"))

        # Reservation shares the same IPAddress (status reserved) + MACAddress the sync made.
        res = HostReservation.objects.get(subnet=subnet)
        shared_ip = IPAddress.objects.get(address="10.99.0.50/24")
        self.assertEqual(res.ipv4_address, shared_ip)
        self.assertEqual(res.hostname, "res-host")
        self.assertEqual(res.hw_address, MACAddress.objects.get(mac_address="aa:bb:cc:dd:ee:01"))

    def test_v6_import_uses_ipv6_addresses_m2m(self):
        from ipam.models import IPAddress

        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")

        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(_conf_v6(), 6))
        self.assertEqual(summary.errors, 0, summary.warnings)

        subnet = Subnet.objects.get(prefix__prefix="2001:db8:99::/64")
        res = HostReservation.objects.get(subnet=subnet)
        self.assertEqual(res.duid, "01:02:03:04:05")
        self.assertIsNone(res.ipv4_address)
        self.assertIn(IPAddress.objects.get(address="2001:db8:99::50/64"), res.ipv6_addresses.all())

    # ── the subnet_id decoupling (decision 5) ────────────────────────────────

    def test_dualstack_v4_and_v6_subnet_id_1_both_import_without_collision(self):
        from netbox_kea.models import KeaDhcpLink

        self.adapter.import_server_config(self.server, parse_dhcp_config(_conf_v4(), 4))
        self.adapter.import_server_config(self.server, parse_dhcp_config(_conf_v6(), 6))

        link4 = KeaDhcpLink.objects.get(server=self.server, family=4, kea_subnet_id=1)
        link6 = KeaDhcpLink.objects.get(server=self.server, family=6, kea_subnet_id=1)
        # Same Kea subnet-id (1) for both families, but distinct plugin Subnets +
        # distinct globally-unique plugin subnet_ids — no UniqueConstraint collision.
        self.assertNotEqual(link4.object_id, link6.object_id)
        self.assertNotEqual(link4.sys4_object.subnet_id, link6.sys4_object.subnet_id)

    # ── idempotency ──────────────────────────────────────────────────────────

    def test_reimport_is_idempotent(self):
        from netbox_kea.models import KeaDhcpLink

        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        Pool = apps.get_model(DHCP_PLUGIN, "Pool")
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")

        self.adapter.import_server_config(self.server, parse_dhcp_config(_conf_v4(), 4))
        second = self.adapter.import_server_config(self.server, parse_dhcp_config(_conf_v4(), 4))

        self.assertEqual(second.subnets_created, 0)
        self.assertEqual(second.pools_created, 0)
        self.assertEqual(second.reservations_created, 0)
        self.assertEqual(KeaDhcpLink.objects.filter(server=self.server, family=4, kea_subnet_id=1).count(), 1)
        self.assertEqual(Subnet.objects.filter(prefix__prefix="10.99.0.0/24").count(), 1)
        self.assertEqual(Pool.objects.count(), 1)
        self.assertEqual(HostReservation.objects.count(), 1)

    # ── deferred reporting ────────────────────────────────────────────────────

    def test_shared_network_subnets_flattened_and_reported(self):
        conf = {
            "shared-networks": [
                {"name": "office", "subnet4": [{"id": 5, "subnet": "10.50.0.0/24"}]},
            ]
        }
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertEqual(summary.shared_networks_deferred, 1)
        subnet = Subnet.objects.get(prefix__prefix="10.50.0.0/24")
        # Flattened onto the DHCPServer, not a (prefix-requiring) SharedNetwork.
        self.assertIsNotNone(subnet.dhcp_server)
        self.assertIsNone(subnet.shared_network)


@tag("dhcp_plugin")
@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class DhcpPluginStaleCleanupGuardTest(TestCase):
    """Stale-IP cleanup must never remove an IP a netbox_dhcp reservation references."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise cls.skipException(f"{DHCP_PLUGIN} not installed")
        super().setUpClass()

    def setUp(self):
        self.server = _make_db_server(name=f"kea-clean-{timezone.now().timestamp()}")

    def test_cleanup_skips_sys4_referenced_ip(self):
        from ipam.models import IPAddress

        from netbox_kea.integrations import dhcp_plugin
        from netbox_kea.sync import _cleanup_stale_ips

        # Two Kea-synced IPs share one hostname; one will be referenced by a reservation.
        conf = {
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.77.0.0/24",
                    "reservations": [
                        {"hw-address": "aa:bb:cc:dd:ee:77", "ip-address": "10.77.0.50", "hostname": "mover"}
                    ],
                }
            ]
        }
        dhcp_plugin.import_server_config(self.server, parse_dhcp_config(conf, 4))
        referenced = IPAddress.objects.get(address="10.77.0.50/24")

        # An unreferenced, same-hostname Kea-synced IP (the kind cleanup is meant to remove).
        unreferenced = IPAddress.objects.create(
            address="10.77.0.51/24",
            status="dhcp",
            dns_name="mover",
            description="Synced from Kea DHCP lease",
        )

        # Device "moved" to a third IP → cleanup runs for hostname "mover".
        cleaned = _cleanup_stale_ips("10.77.0.99", "mover", mode="remove")

        self.assertEqual(cleaned, 1)  # only the unreferenced one
        self.assertFalse(IPAddress.objects.filter(pk=unreferenced.pk).exists())
        self.assertTrue(IPAddress.objects.filter(pk=referenced.pk).exists())
        self.assertIn(referenced.pk, dhcp_plugin.sys4_referenced_ip_ids())
