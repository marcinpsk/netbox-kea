# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the optional netbox_dhcp adapter (real DB + real plugin).

Gated on the plugin being installed. A dedicated CI job installs the exact
supported ``netbox_dhcp`` release. Other environments can skip this module.
Only the Kea HTTP boundary is bypassed. The ORM, IPAM/DCIM models, and the
``netbox_dhcp`` models are real.
"""

from __future__ import annotations

import ipaddress
import unittest
from unittest.mock import patch

from django.apps import apps
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.utils import timezone

from netbox_kea.kea import KeaClient
from netbox_kea.mappers.kea_to_dhcp import parse_dhcp_config
from netbox_kea.reservations import ReservationDiagnostic, ReservationSnapshot
from netbox_kea.subnet_catalogue import IdentityOnlyCatalogueSnapshot, SubnetIdentity, VerifiedSubnet

from .kea_stub import _res_page, stub_kea
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


def _reservation_snapshot(conf: dict, version: int, hosts: list[dict] | None = None):
    """Build the real typed Snapshot used by the optional adapter."""
    subnet_key = f"subnet{version}"
    entries = list(conf.get(subnet_key, []))
    for shared_network in conf.get("shared-networks", []):
        entries.extend(shared_network.get(subnet_key, []))
    verified = tuple(
        VerifiedSubnet(
            identity=SubnetIdentity(
                subnet_id=int(entry["id"]),
                network=ipaddress.ip_network(entry["subnet"]),
            ),
            configuration=None,
            shared_network=None,
        )
        for entry in entries
        if isinstance(entry, dict) and entry.get("id") is not None and entry.get("subnet")
    )
    # Identity-only: every VerifiedSubnet here carries configuration=None, which the
    # real builder only produces when no configuration source was read.
    catalogue = IdentityOnlyCatalogueSnapshot(
        server_id=1,
        family=version,
        observed_at=timezone.now(),
        subnets=verified,
        configured_subnets=(),
        diagnostics=(),
        identity_complete=True,
        configuration_complete=False,
        consistent=True,
        configuration_hash=None,
    )
    if hosts is None:
        hosts = []
        for entry in entries:
            for reservation in entry.get("reservations", []):
                hosts.append({"subnet-id": int(entry["id"]), **reservation})
    client = KeaClient(url="http://kea.example.invalid", send_service=False)
    with stub_kea({"reservation-get-page": _res_page(hosts)}):
        # Bound the page to the fixture so a larger fixture cannot silently truncate.
        return client.reservation_page(version, catalogue, limit=max(len(hosts), 1))


@tag("dhcp_plugin")
@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class DhcpPluginAdapterTest(TestCase):
    """Importing a Kea config into netbox_dhcp via the guarded adapter."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise unittest.SkipTest(f"{DHCP_PLUGIN} not installed")
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

        conf = _conf_v4()
        summary = self.adapter.import_server_config(
            self.server, parse_dhcp_config(conf, 4), _reservation_snapshot(conf, 4)
        )

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

        conf = _conf_v6()
        summary = self.adapter.import_server_config(
            self.server, parse_dhcp_config(conf, 6), _reservation_snapshot(conf, 6)
        )
        self.assertEqual(summary.errors, 0, summary.warnings)

        subnet = Subnet.objects.get(prefix__prefix="2001:db8:99::/64")
        res = HostReservation.objects.get(subnet=subnet)
        self.assertEqual(res.duid, "01:02:03:04:05")
        self.assertIsNone(res.ipv4_address)
        self.assertIn(IPAddress.objects.get(address="2001:db8:99::50/64"), res.ipv6_addresses.all())

    # ── the subnet_id decoupling (decision 5) ────────────────────────────────

    def test_dualstack_v4_and_v6_subnet_id_1_both_import_without_collision(self):
        from netbox_kea.models import KeaDhcpLink

        conf4 = _conf_v4()
        conf6 = _conf_v6()
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf4, 4), _reservation_snapshot(conf4, 4))
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf6, 6), _reservation_snapshot(conf6, 6))

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

        conf = _conf_v4()
        snapshot = _reservation_snapshot(conf, 4)
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4), snapshot)
        second = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4), snapshot)

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

    # ── per-object error isolation ───────────────────────────────────────────

    def test_bad_cidr_is_per_subnet_error_not_fatal(self):
        """One unparseable subnet CIDR is counted as an error; other subnets still import."""
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        conf = {
            "subnet4": [
                {"id": 1, "subnet": "not-a-cidr"},  # _ensure_prefix raises → caught per-subnet
                {"id": 2, "subnet": "10.51.0.0/24"},
            ]
        }
        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertGreaterEqual(summary.errors, 1)
        # The bad subnet did not abort the import: the good one is present.
        self.assertTrue(Subnet.objects.filter(prefix__prefix="10.51.0.0/24").exists())
        self.assertEqual(summary.subnets_created, 1)

    def test_reservation_resolver_failure_is_per_reservation(self):
        """A resolver error on one reservation does not stop the others in the subnet."""
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        conf = {
            "subnet4": [
                {
                    "id": 3,
                    "subnet": "10.53.0.0/24",
                    "reservations": [
                        {"hw-address": "aa:bb:cc:dd:ee:a1", "ip-address": "10.53.0.10", "hostname": "r1"},
                        {"hw-address": "aa:bb:cc:dd:ee:a2", "ip-address": "10.53.0.11", "hostname": "r2"},
                    ],
                }
            ]
        }
        real = self.adapter._ensure_reservation_addresses
        calls = {"n": 0}

        def flaky(res, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("resolver boom")
            return real(res, *args, **kwargs)

        with patch.object(self.adapter, "_ensure_reservation_addresses", side_effect=flaky):
            summary = self.adapter.import_server_config(
                self.server, parse_dhcp_config(conf, 4), _reservation_snapshot(conf, 4)
            )

        self.assertGreaterEqual(summary.errors, 1)
        self.assertEqual(summary.reservations_created, 1)
        self.assertTrue(HostReservation.objects.filter(hostname="r2").exists())

    # ── idempotency edge cases ────────────────────────────────────────────────

    def test_stale_link_is_relinked_not_collided(self):
        """A dangling KeaDhcpLink (its subnet deleted) is relinked on re-import."""
        from netbox_kea.models import KeaDhcpLink

        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        conf = {"subnet4": [{"id": 1, "subnet": "10.52.0.0/24"}]}  # bare: deletable without children

        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        link = KeaDhcpLink.objects.get(server=self.server, family=4, kea_subnet_id=1)
        old_pk = link.sys4_object.pk

        # Subnet deleted out from under the link; the link row survives, dangling.
        Subnet.objects.filter(pk=old_pk).delete()
        link.refresh_from_db()
        self.assertIsNone(link.sys4_object)

        # Re-import must relink the stale identity row, not violate the
        # keadhcplink_unique_subnet_identity constraint by creating a duplicate.
        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertEqual(summary.errors, 0, summary.warnings)
        links = KeaDhcpLink.objects.filter(server=self.server, family=4, kea_subnet_id=1)
        self.assertEqual(links.count(), 1)
        new_subnet = links.first().sys4_object
        self.assertIsNotNone(new_subnet)
        self.assertNotEqual(new_subnet.pk, old_pk)

    def test_reimport_clears_dropped_ipv6_addresses(self):
        """Re-importing a v6 reservation that lost its addresses clears the stale M2M relations."""
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        with_addrs = {
            "subnet6": [
                {
                    "id": 4,
                    "subnet": "2001:db8:53::/64",
                    "reservations": [
                        {
                            "duid": "0a:0b:0c",
                            "ip-addresses": ["2001:db8:53::10", "2001:db8:53::11"],
                            "hostname": "v6r",
                        }
                    ],
                }
            ]
        }
        self.adapter.import_server_config(
            self.server, parse_dhcp_config(with_addrs, 6), _reservation_snapshot(with_addrs, 6)
        )
        res = HostReservation.objects.get(hostname="v6r")
        self.assertEqual(res.ipv6_addresses.count(), 2)

        without_addrs = {
            "subnet6": [
                {
                    "id": 4,
                    "subnet": "2001:db8:53::/64",
                    "reservations": [{"duid": "0a:0b:0c", "ip-addresses": [], "hostname": "v6r"}],
                }
            ]
        }
        self.adapter.import_server_config(
            self.server, parse_dhcp_config(without_addrs, 6), _reservation_snapshot(without_addrs, 6)
        )
        res.refresh_from_db()
        self.assertEqual(res.ipv6_addresses.count(), 0)


@tag("dhcp_plugin")
@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class DhcpPluginOptionImportTest(TestCase):
    """Importing Kea ``option-data`` into netbox_dhcp ``Option`` rows (real ORM + defs)."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise unittest.SkipTest(f"{DHCP_PLUGIN} not installed")
        super().setUpClass()

    def setUp(self):
        self.server = _make_db_server(name=f"kea-opt-{timezone.now().timestamp()}")
        from netbox_kea.integrations import dhcp_plugin

        self.adapter = dhcp_plugin

    def _ct(self, model):
        from django.contrib.contenttypes.models import ContentType

        return ContentType.objects.get_for_model(model)

    def test_standard_subnet_option_binds_to_shipped_definition(self):
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        Option = apps.get_model(DHCP_PLUGIN, "Option")
        conf = {
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.21.0.0/24",
                    "option-data": [
                        {"code": 3, "name": "routers", "data": "10.21.0.1", "space": "dhcp4", "always-send": True}
                    ],
                }
            ]
        }
        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(summary.options_created, 1, summary.warnings)
        self.assertEqual(summary.options_skipped, 0, summary.warnings)

        subnet = Subnet.objects.get(prefix__prefix="10.21.0.0/24")
        opt = Option.objects.get(assigned_object_type=self._ct(Subnet), assigned_object_id=subnet.pk)
        self.assertEqual(opt.definition.code, 3)
        self.assertTrue(opt.definition.standard)  # bound to the sys4-shipped standard def
        self.assertEqual(opt.data, "10.21.0.1")
        self.assertEqual(opt.send_option, "always-send")

    def test_global_option_assigned_to_dhcp_server(self):
        DHCPServer = apps.get_model(DHCP_PLUGIN, "DHCPServer")
        Option = apps.get_model(DHCP_PLUGIN, "Option")
        conf = {
            "option-data": [{"code": 6, "name": "domain-name-servers", "data": "1.1.1.1", "space": "dhcp4"}],
            "subnet4": [],
        }
        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertEqual(summary.options_created, 1, summary.warnings)
        srv = DHCPServer.objects.get(name=self.server.name)
        opt = Option.objects.get(assigned_object_type=self._ct(DHCPServer), assigned_object_id=srv.pk)
        self.assertEqual(opt.definition.code, 6)

    def test_custom_option_def_is_created_and_bound(self):
        OptionDefinition = apps.get_model(DHCP_PLUGIN, "OptionDefinition")
        Option = apps.get_model(DHCP_PLUGIN, "Option")
        conf = {
            "option-def": [{"code": 224, "name": "my-custom", "space": "dhcp4", "type": "string"}],
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.22.0.0/24",
                    "option-data": [{"code": 224, "name": "my-custom", "data": "hello", "space": "dhcp4"}],
                }
            ],
        }
        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(summary.option_defs_created, 1, summary.warnings)
        self.assertEqual(summary.options_created, 1, summary.warnings)

        definition = OptionDefinition.objects.get(code=224, standard=False)
        self.assertEqual(definition.name, "my-custom")
        self.assertEqual(definition.dhcp_server.name, self.server.name)
        self.assertTrue(Option.objects.filter(definition=definition).exists())

    def test_unresolvable_option_is_skipped_not_fatal(self):
        # Custom code with no option-def → no definition to bind → skipped, import still ok.
        conf = {
            "subnet4": [
                {
                    "id": 1,
                    "subnet": "10.23.0.0/24",
                    "option-data": [{"code": 231, "data": "x", "space": "dhcp4"}],
                }
            ]
        }
        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertEqual(summary.errors, 0)
        self.assertEqual(summary.options_created, 0)
        self.assertEqual(summary.options_skipped, 1, summary.warnings)
        self.assertEqual(summary.subnets_created, 1)  # subnet still imported

    def test_reimport_updates_option_not_duplicated(self):
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        Option = apps.get_model(DHCP_PLUGIN, "Option")
        conf = {
            "subnet4": [
                {"id": 1, "subnet": "10.24.0.0/24", "option-data": [{"code": 3, "data": "10.24.0.1", "space": "dhcp4"}]}
            ]
        }
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        conf["subnet4"][0]["option-data"][0]["data"] = "10.24.0.254"
        second = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))

        self.assertEqual(second.options_created, 0)
        self.assertEqual(second.options_updated, 1)
        subnet = Subnet.objects.get(prefix__prefix="10.24.0.0/24")
        opt = Option.objects.get(assigned_object_type=self._ct(Subnet), assigned_object_id=subnet.pk)
        self.assertEqual(opt.data, "10.24.0.254")


@tag("dhcp_plugin")
@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class DhcpPluginTuningImportTest(TestCase):
    """Tuning fields land on DHCPServer/Subnet with parent-diff suppression (real ORM)."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise unittest.SkipTest(f"{DHCP_PLUGIN} not installed")
        super().setUpClass()

    def setUp(self):
        self.server = _make_db_server(name=f"kea-tune-{timezone.now().timestamp()}")
        from netbox_kea.integrations import dhcp_plugin

        self.adapter = dhcp_plugin

    def test_global_settings_applied_to_dhcp_server(self):
        from decimal import Decimal

        DHCPServer = apps.get_model(DHCP_PLUGIN, "DHCPServer")
        conf = {
            "valid-lifetime": 3600,
            "renew-timer": 900,
            "allocator": "iterative",
            "t1-percent": 0.5,
            "decline-probation-period": 86400,
            "ddns-replace-client-name": "when-not-present",
            "server-id": {"type": "LLT", "enterprise-id": 0},
            "host-reservation-identifiers": ["hw-address", "duid", "circuit-id", "client-id"],
            "subnet4": [],
        }
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))

        srv = DHCPServer.objects.get(name=self.server.name)
        self.assertEqual(srv.valid_lifetime, 3600)
        self.assertEqual(srv.renew_timer, 900)
        self.assertEqual(srv.allocator, "iterative")
        self.assertEqual(srv.t1_percent, Decimal("0.5"))
        self.assertEqual(srv.decline_probation_period, 86400)
        # hyphenated Kea value normalized to the plugin's underscored choice
        self.assertEqual(srv.ddns_replace_client_name, "when_not_present")
        # server-id dict reduced to its type
        self.assertEqual(srv.server_id, "LLT")
        self.assertEqual(srv.host_reservation_identifiers, ["hw-address", "duid", "circuit-id", "client-id"])

    def test_subnet_value_equal_to_global_is_suppressed(self):
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        conf = {
            "valid-lifetime": 3600,
            "subnet4": [
                # subnet repeats the (inherited) global value — must NOT be stored on the subnet
                {"id": 1, "subnet": "10.30.0.0/24", "valid-lifetime": 3600},
            ],
        }
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        subnet = Subnet.objects.get(prefix__prefix="10.30.0.0/24")
        self.assertIsNone(subnet.valid_lifetime)  # inherits from the DHCPServer parent

    def test_subnet_value_differing_from_global_is_stored(self):
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        conf = {
            "valid-lifetime": 3600,
            "subnet4": [
                {"id": 1, "subnet": "10.31.0.0/24", "valid-lifetime": 7200, "relay": {"ip-addresses": ["10.31.0.3"]}},
            ],
        }
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        subnet = Subnet.objects.get(prefix__prefix="10.31.0.0/24")
        self.assertEqual(subnet.valid_lifetime, 7200)  # genuine per-subnet override stored
        self.assertEqual(subnet.relay, "10.31.0.3")  # relay dict flattened to CSV

    def test_v6_style_decimal_does_not_raise(self):
        # 0.8 has no exact float repr; the str-based Decimal coercion must keep it clean.
        from decimal import Decimal

        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        conf = {
            "t2-percent": 0.5,
            "subnet6": [{"id": 1, "subnet": "2001:db8:30::/64", "t2-percent": 0.8}],
        }
        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 6))
        self.assertEqual(summary.errors, 0, summary.warnings)
        subnet = Subnet.objects.get(prefix__prefix="2001:db8:30::/64")
        self.assertEqual(subnet.t2_percent, Decimal("0.8"))

    def test_dualstack_global_first_family_wins_shared_fields(self):
        DHCPServer = apps.get_model(DHCP_PLUGIN, "DHCPServer")
        # v4 sets shared valid-lifetime; v6 supplies preferred-lifetime and a *different*
        # valid-lifetime that must NOT clobber v4's (single DHCPServer spans both).
        self.adapter.import_server_config(self.server, parse_dhcp_config({"valid-lifetime": 3600, "subnet4": []}, 4))
        self.adapter.import_server_config(
            self.server, parse_dhcp_config({"valid-lifetime": 4000, "preferred-lifetime": 3000, "subnet6": []}, 6)
        )
        srv = DHCPServer.objects.get(name=self.server.name)
        self.assertEqual(srv.valid_lifetime, 3600)  # v4 (first) wins the shared field
        self.assertEqual(srv.preferred_lifetime, 3000)  # v6 fills the gap

    def test_tuning_reimport_is_idempotent(self):
        conf = {
            "valid-lifetime": 3600,
            "subnet4": [{"id": 1, "subnet": "10.32.0.0/24", "valid-lifetime": 7200}],
        }
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        second = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertEqual(second.subnets_created, 0)
        self.assertEqual(second.subnets_updated, 0)  # nothing changed → not reported as updated

    def test_subnet_override_cleared_when_value_returns_to_inherited(self):
        # Finding 1: a removed Kea override must not linger as stale data on re-import.
        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        conf1 = {"valid-lifetime": 3600, "subnet4": [{"id": 7, "subnet": "10.42.0.0/24", "valid-lifetime": 7200}]}
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf1, 4))
        subnet = Subnet.objects.get(prefix__prefix="10.42.0.0/24")
        self.assertEqual(subnet.valid_lifetime, 7200)

        # Override removed in Kea (subnet value now equals the global) → must be cleared.
        conf2 = {"valid-lifetime": 3600, "subnet4": [{"id": 7, "subnet": "10.42.0.0/24", "valid-lifetime": 3600}]}
        second = self.adapter.import_server_config(self.server, parse_dhcp_config(conf2, 4))
        subnet.refresh_from_db()
        self.assertIsNone(subnet.valid_lifetime)  # inherits again, not stale 7200
        self.assertEqual(second.subnets_updated, 1)

    def test_global_setting_change_resyncs_on_reimport(self):
        # Finding 2: a changed global value must re-sync (was write-once before).
        DHCPServer = apps.get_model(DHCP_PLUGIN, "DHCPServer")
        self.adapter.import_server_config(self.server, parse_dhcp_config({"valid-lifetime": 3600, "subnet4": []}, 4))
        self.adapter.import_server_config(self.server, parse_dhcp_config({"valid-lifetime": 7200, "subnet4": []}, 4))
        srv = DHCPServer.objects.get(name=self.server.name)
        self.assertEqual(srv.valid_lifetime, 7200)


@tag("dhcp_plugin")
@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class DhcpPluginClientClassImportTest(TestCase):
    """Importing Kea ``client-classes`` into netbox_dhcp ``ClientClass`` rows."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise unittest.SkipTest(f"{DHCP_PLUGIN} not installed")
        super().setUpClass()

    def setUp(self):
        self.server = _make_db_server(name=f"kea-cc-{timezone.now().timestamp()}")
        from netbox_kea.integrations import dhcp_plugin

        self.adapter = dhcp_plugin

    def _conf(self):
        return {
            "next-server": "0.0.0.0",
            "client-classes": [
                {
                    "name": "voip",
                    "test": "substring(option[60].hex,0,6) == 'Aastra'",
                    "next-server": "192.0.2.254",
                    "boot-file-name": "/dev/null",
                    "server-hostname": "hal9000",
                    "option-data": [{"code": 3, "name": "routers", "data": "10.0.0.1", "space": "dhcp4"}],
                }
            ],
            "subnet4": [],
        }

    def test_client_class_created_with_settings_and_options(self):
        from django.contrib.contenttypes.models import ContentType

        ClientClass = apps.get_model(DHCP_PLUGIN, "ClientClass")
        Option = apps.get_model(DHCP_PLUGIN, "Option")
        DHCPServer = apps.get_model(DHCP_PLUGIN, "DHCPServer")

        summary = self.adapter.import_server_config(self.server, parse_dhcp_config(self._conf(), 4))
        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(summary.client_classes_created, 1)
        self.assertEqual(summary.options_created, 1)  # the class's routers option

        cc = ClientClass.objects.get(name=f"{self.server.name}: voip")
        self.assertEqual(cc.dhcp_server, DHCPServer.objects.get(name=self.server.name))
        self.assertEqual(cc.test, "substring(option[60].hex,0,6) == 'Aastra'")
        # BOOTP settings differ from the server baseline → stored on the class.
        self.assertEqual(cc.next_server, "192.0.2.254")
        self.assertEqual(cc.boot_file_name, "/dev/null")
        self.assertEqual(cc.server_hostname, "hal9000")
        # The class's option was imported and assigned to the ClientClass.
        ct = ContentType.objects.get_for_model(ClientClass)
        self.assertTrue(Option.objects.filter(assigned_object_type=ct, assigned_object_id=cc.pk).exists())

    def test_client_class_name_is_namespaced_to_server(self):
        ClientClass = apps.get_model(DHCP_PLUGIN, "ClientClass")
        self.adapter.import_server_config(self.server, parse_dhcp_config(self._conf(), 4))
        # Namespaced so two servers' identically-named Kea classes don't collide.
        self.assertTrue(ClientClass.objects.filter(name=f"{self.server.name}: voip").exists())
        self.assertFalse(ClientClass.objects.filter(name="voip").exists())

    def test_reimport_is_idempotent(self):
        ClientClass = apps.get_model(DHCP_PLUGIN, "ClientClass")
        self.adapter.import_server_config(self.server, parse_dhcp_config(self._conf(), 4))
        second = self.adapter.import_server_config(self.server, parse_dhcp_config(self._conf(), 4))
        self.assertEqual(second.client_classes_created, 0)
        self.assertEqual(second.client_classes_updated, 0)
        self.assertEqual(ClientClass.objects.filter(name=f"{self.server.name}: voip").count(), 1)

    def test_changed_test_expression_reported_as_updated(self):
        ClientClass = apps.get_model(DHCP_PLUGIN, "ClientClass")
        conf = self._conf()
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        conf["client-classes"][0]["test"] = "substring(option[60].hex,0,4) == 'Cisco'"
        second = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4))
        self.assertEqual(second.client_classes_updated, 1)
        cc = ClientClass.objects.get(name=f"{self.server.name}: voip")
        self.assertEqual(cc.test, "substring(option[60].hex,0,4) == 'Cisco'")


@tag("dhcp_plugin")
@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class DhcpPluginReservationSnapshotImportTest(TestCase):
    """Typed Reservation Snapshots import into the matching plugin scope."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise unittest.SkipTest(f"{DHCP_PLUGIN} not installed")
        super().setUpClass()

    def setUp(self):
        self.server = _make_db_server(name=f"kea-pageres-{timezone.now().timestamp()}")
        from netbox_kea.integrations import dhcp_plugin

        self.adapter = dhcp_plugin

    def test_db_reservation_imported_into_linked_subnet(self):
        from ipam.models import IPAddress

        Subnet = apps.get_model(DHCP_PLUGIN, "Subnet")
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")

        # Subnet is in config-get; the reservation is ONLY in the hosts DB (subnet-id 7).
        conf = {"subnet4": [{"id": 7, "subnet": "10.40.0.0/24"}]}
        hosts = [{"subnet-id": 7, "hw-address": "aa:bb:cc:dd:ee:40", "ip-address": "10.40.0.50", "hostname": "db-host"}]
        summary = self.adapter.import_server_config(
            self.server, parse_dhcp_config(conf, 4), _reservation_snapshot(conf, 4, hosts)
        )

        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(summary.reservations_created, 1, summary.warnings)
        subnet = Subnet.objects.get(prefix__prefix="10.40.0.0/24")
        res = HostReservation.objects.get(subnet=subnet)
        self.assertEqual(res.hostname, "db-host")
        # Shares the same IPAM IPAddress the reservation sync maintains.
        self.assertEqual(res.ipv4_address, IPAddress.objects.get(address="10.40.0.50/24"))

    def test_global_reservation_attached_to_dhcp_server(self):
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        DHCPServer = apps.get_model(DHCP_PLUGIN, "DHCPServer")

        conf = {"subnet4": []}
        hosts = [
            {"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:00", "ip-address": "10.0.0.9", "hostname": "global-host"}
        ]
        summary = self.adapter.import_server_config(
            self.server, parse_dhcp_config(conf, 4), _reservation_snapshot(conf, 4, hosts)
        )

        self.assertEqual(summary.errors, 0, summary.warnings)
        res = HostReservation.objects.get(hostname="global-host")
        self.assertIsNone(res.subnet)
        self.assertEqual(res.dhcp_server, DHCPServer.objects.get(name=self.server.name))
        self.assertIsNone(res.ipv4_address)

    def test_manually_curated_address_is_left_unchanged_but_still_linked(self):
        """The unattended import never claims a foreign IPAM row, and still points at it."""
        from ipam.models import IPAddress

        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        curated = IPAddress.objects.create(
            address="10.42.0.50/24",
            status="active",
            description="Held for the core switch by NetOps",
        )
        conf = {"subnet4": [{"id": 7, "subnet": "10.42.0.0/24"}]}
        hosts = [{"subnet-id": 7, "hw-address": "aa:bb:cc:dd:ee:42", "ip-address": "10.42.0.50", "hostname": "curated"}]

        summary = self.adapter.import_server_config(
            self.server, parse_dhcp_config(conf, 4), _reservation_snapshot(conf, 4, hosts)
        )

        curated.refresh_from_db()
        self.assertEqual(curated.description, "Held for the core switch by NetOps")
        self.assertEqual(curated.status, "active")
        self.assertEqual(curated.dns_name, "")
        self.assertEqual(summary.foreign_addresses_skipped, 1)
        self.assertTrue(
            any("10.42.0.50" in warning and "left unchanged" in warning for warning in summary.warnings),
            summary.warnings,
        )
        self.assertEqual(HostReservation.objects.get(hostname="curated").ipv4_address, curated)

    def test_unverified_subnet_id_is_quarantined_before_adapter(self):
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        conf = {"subnet4": []}  # subnet-id 99 was never imported
        hosts = [{"subnet-id": 99, "hw-address": "aa:bb:cc:dd:ee:99", "ip-address": "10.99.0.5"}]
        summary = self.adapter.import_server_config(
            self.server, parse_dhcp_config(conf, 4), _reservation_snapshot(conf, 4, hosts)
        )

        self.assertEqual(summary.reservations_created, 0)
        self.assertEqual(summary.reservations_quarantined, 1)
        self.assertFalse(HostReservation.objects.exists())
        self.assertTrue(any("cannot verify" in warning for warning in summary.warnings), summary.warnings)

    def test_reimport_is_idempotent(self):
        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        conf = {"subnet4": [{"id": 7, "subnet": "10.41.0.0/24"}]}
        hosts = [{"subnet-id": 7, "hw-address": "aa:bb:cc:dd:ee:41", "ip-address": "10.41.0.50"}]

        snapshot = _reservation_snapshot(conf, 4, hosts)
        self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4), snapshot)
        second = self.adapter.import_server_config(self.server, parse_dhcp_config(conf, 4), snapshot)
        self.assertEqual(second.reservations_created, 0)
        self.assertEqual(HostReservation.objects.count(), 1)

    def test_global_hardware_reservation_keeps_its_identity_across_reimports(self):
        """A Global Scope skips the IPAM sync, which must not cost the reservation its MAC."""
        from dcim.models import MACAddress

        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")

        conf = {"subnet4": []}
        hosts = [{"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:11", "ip-address": "10.11.0.9", "hostname": "g-hw"}]
        intent = parse_dhcp_config(conf, 4)
        snapshot = _reservation_snapshot(conf, 4, hosts)

        first = self.adapter.import_server_config(self.server, intent, snapshot)
        second = self.adapter.import_server_config(self.server, intent, snapshot)

        self.assertEqual(first.errors, 0, first.warnings)
        self.assertEqual(second.errors, 0, second.warnings)
        self.assertEqual(second.reservations_created, 0)
        self.assertEqual(HostReservation.objects.count(), 1)
        self.assertEqual(
            HostReservation.objects.get().hw_address,
            MACAddress.objects.get(mac_address="aa:bb:cc:dd:ee:11"),
        )

    def test_dual_stack_global_reservations_keep_separate_rows(self):
        """One identifier reserved globally in both protocols is two Reservations, not one.

        ``netbox_dhcp`` derives a reservation's family from its Subnet, and a Global
        Reservation has none, so both families matched the same row: the second import
        overwrote the first one's hostname and options instead of creating its own row.
        ``KeaDhcpLink`` carries the family that the row cannot.
        """
        from netbox_kea.models import KeaDhcpLink

        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")

        identifier = "aa:bb:cc:dd:ee:41"
        conf4, conf6 = {"subnet4": []}, {"subnet6": []}
        hosts4 = [{"subnet-id": 0, "hw-address": identifier, "hostname": "dual-v4"}]
        hosts6 = [{"subnet-id": 0, "hw-address": identifier, "hostname": "dual-v6"}]

        for _ in range(2):  # idempotent: the second pass must update, never duplicate
            v4 = self.adapter.import_server_config(
                self.server, parse_dhcp_config(conf4, 4), _reservation_snapshot(conf4, 4, hosts4)
            )
            v6 = self.adapter.import_server_config(
                self.server, parse_dhcp_config(conf6, 6), _reservation_snapshot(conf6, 6, hosts6)
            )

        self.assertEqual(v4.errors, 0, v4.warnings)
        self.assertEqual(v6.errors, 0, v6.warnings)
        self.assertEqual(v4.reservations_created, 0)
        self.assertEqual(v6.reservations_created, 0)

        rows = list(HostReservation.objects.order_by("name"))
        self.assertEqual(len(rows), 2, [row.name for row in rows])
        v4_row, v6_row = rows
        self.assertIn("DHCPv4", v4_row.name)
        self.assertIn("DHCPv6", v6_row.name)
        # Each row keeps its own facts; one shared row kept only the last import's.
        self.assertEqual(v4_row.hostname, "dual-v4")
        self.assertEqual(v6_row.hostname, "dual-v6")
        self.assertEqual(
            sorted(KeaDhcpLink.objects.filter(kea_identity__isnull=False).values_list("family", "kea_identity")),
            [(4, f"hw-address:{identifier}"), (6, f"hw-address:{identifier}")],
        )

    def test_global_reservation_imported_before_the_link_is_adopted(self):
        """Relink a Global row from an earlier release instead of duplicating it."""
        from netbox_kea.models import KeaDhcpLink

        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")

        conf = {"subnet4": []}
        hosts = [{"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:42", "ip-address": "10.42.0.9", "hostname": "legacy"}]
        intent = parse_dhcp_config(conf, 4)
        snapshot = _reservation_snapshot(conf, 4, hosts)

        self.adapter.import_server_config(self.server, intent, snapshot)
        legacy = HostReservation.objects.get()
        legacy.name = "legacy name from an earlier import"
        legacy.save()
        KeaDhcpLink.objects.filter(kea_identity__isnull=False).delete()

        summary = self.adapter.import_server_config(self.server, intent, snapshot)

        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertEqual(summary.reservations_created, 0)
        self.assertEqual(HostReservation.objects.count(), 1)
        adopted = HostReservation.objects.get()
        self.assertEqual(adopted.pk, legacy.pk)
        self.assertIn("DHCPv4", adopted.name)
        self.assertEqual(
            KeaDhcpLink.objects.filter(
                server=self.server, family=4, kea_identity="hw-address:aa:bb:cc:dd:ee:42"
            ).count(),
            1,
        )

    def test_addressless_hardware_reservation_keeps_its_identity_across_reimports(self):
        """An identifier-only host reserves no address, so the IPAM sync skips it too."""
        from dcim.models import MACAddress

        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")

        conf = {"subnet4": [{"id": 12, "subnet": "10.12.0.0/24"}]}
        hosts = [{"subnet-id": 12, "hw-address": "aa:bb:cc:dd:ee:12", "hostname": "id-only"}]
        intent = parse_dhcp_config(conf, 4)
        snapshot = _reservation_snapshot(conf, 4, hosts)

        first = self.adapter.import_server_config(self.server, intent, snapshot)
        second = self.adapter.import_server_config(self.server, intent, snapshot)

        self.assertEqual(first.errors, 0, first.warnings)
        self.assertEqual(second.errors, 0, second.warnings)
        self.assertEqual(second.reservations_created, 0)
        self.assertEqual(HostReservation.objects.count(), 1)
        self.assertEqual(
            HostReservation.objects.get().hw_address,
            MACAddress.objects.get(mac_address="aa:bb:cc:dd:ee:12"),
        )

    def test_global_v6_reservation_does_not_invent_ipam_scope(self):
        from ipam.models import IPAddress

        intent = parse_dhcp_config({"subnet6": []}, 6)
        hosts = [{"subnet-id": 0, "duid": "01:02:03:0a", "ip-addresses": ["2001:db8:aa::5"], "hostname": "g6"}]
        snapshot = _reservation_snapshot({"subnet6": []}, 6, hosts)
        summary = self.adapter.import_server_config(self.server, intent, snapshot)

        self.assertEqual(summary.errors, 0, summary.warnings)
        self.assertFalse(IPAddress.objects.filter(address__startswith="2001:db8:aa::5/").exists())

    def test_reservation_whose_mac_row_cannot_be_written_is_skipped(self):
        """Importing it would store a row with no identifier, which no later run can match."""
        from django.db.utils import OperationalError

        HostReservation = apps.get_model(DHCP_PLUGIN, "HostReservation")
        conf = {"subnet4": [{"id": 7, "subnet": "10.43.0.0/24"}]}
        hosts = [{"subnet-id": 7, "hw-address": "aa:bb:cc:dd:ee:43", "ip-address": "10.43.0.50"}]
        intent = parse_dhcp_config(conf, 4)
        snapshot = _reservation_snapshot(conf, 4, hosts)

        with patch("dcim.models.MACAddress.objects.get_or_create", side_effect=OperationalError("no MAC row")):
            first = self.adapter.import_server_config(self.server, intent, snapshot)
            second = self.adapter.import_server_config(self.server, intent, snapshot)

        self.assertEqual(HostReservation.objects.count(), 0)
        self.assertEqual(first.reservations_created, 0)
        self.assertTrue(first.reservations_unread)
        self.assertTrue(
            any("hardware address could not be resolved" in warning for warning in second.warnings),
            second.warnings,
        )


@tag("dhcp_plugin")
@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class DhcpPluginStaleCleanupGuardTest(TestCase):
    """Stale-IP cleanup must never remove an IP a netbox_dhcp reservation references."""

    @classmethod
    def setUpClass(cls):
        if not apps.is_installed(DHCP_PLUGIN):
            raise unittest.SkipTest(f"{DHCP_PLUGIN} not installed")
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
        dhcp_plugin.import_server_config(self.server, parse_dhcp_config(conf, 4), _reservation_snapshot(conf, 4))
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


class ImportSummaryCompletenessTest(SimpleTestCase):
    """``reservations_unread`` must follow the Snapshot's own completeness flag.

    Needs no ``netbox_dhcp`` model: an incomplete Snapshot with no records never
    reaches the per-record upsert, so this runs in the ordinary unit-test job.
    """

    def _snapshot(self, *, complete):
        diagnostics = (
            ()
            if complete
            else (
                ReservationDiagnostic(
                    code="page-fetch-failed",
                    message="Reservation page traversal did not complete.",
                    source_position="pages[1]",
                ),
            )
        )
        return ReservationSnapshot(family=4, records=(), diagnostics=diagnostics, complete=complete, next_cursor=None)

    def _import(self, snapshot):
        from netbox_kea.integrations.dhcp_plugin import ImportSummary, import_reservation_snapshot

        summary = ImportSummary()
        import_reservation_snapshot(None, None, snapshot, None, summary)
        return summary

    def test_absent_snapshot_reports_the_counts_as_unread(self):
        summary = self._import(None)

        self.assertTrue(summary.reservations_unread)

    def test_incomplete_snapshot_reports_the_counts_as_unread(self):
        # A truncated traversal imports only part of the record set, so the counts
        # are no more complete than for a Snapshot that could not be read at all.
        summary = self._import(self._snapshot(complete=False))

        self.assertTrue(summary.reservations_unread)
        self.assertEqual(summary.reservations_quarantined, 1)

    def test_complete_snapshot_reports_the_counts_as_read(self):
        summary = self._import(self._snapshot(complete=True))

        self.assertFalse(summary.reservations_unread)
        self.assertEqual(summary.reservations_quarantined, 0)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class SkippedReservationCompletenessTest(TestCase):
    """A record the importer drops must leave the counts marked incomplete.

    Needs the real ``KeaDhcpLink`` table to resolve the Subnet, but never reaches the
    per-record upsert, so it runs without ``netbox_dhcp`` installed.
    """

    def setUp(self):
        self.server = _make_db_server()

    def test_record_for_an_unlinked_subnet_id_reports_the_counts_as_unread(self):
        from netbox_kea.integrations.dhcp_plugin import ImportSummary, import_reservation_snapshot

        conf = {"subnet4": [{"id": 7, "subnet": "198.18.7.0/24"}]}
        hosts = [{"subnet-id": 7, "hw-address": "aa:bb:cc:00:00:07", "ip-address": "198.18.7.10"}]
        snapshot = _reservation_snapshot(conf, 4, hosts)
        summary = ImportSummary()

        # No KeaDhcpLink exists for subnet-id 7, so the record cannot be imported.
        import_reservation_snapshot(self.server, None, snapshot, None, summary)

        self.assertTrue(snapshot.complete)
        self.assertEqual(summary.reservations_created, 0)
        self.assertTrue(summary.reservations_unread)
        self.assertIn("reservation for unknown subnet-id 7 skipped", summary.warnings)
