# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Read-only synchronization badges, DCIM updates and ownership claim regressions."""

from __future__ import annotations

import logging

from django.test import TestCase, override_settings

from .kea_stub import _catalogue_responses_for_subnets, _typed_reservation, complete_lease, stub_kea, typed_lease
from .utils import _make_db_server, plugins_config


class TestBulkFetchNetboxIPs(TestCase):
    """bulk_fetch_netbox_ips returns a {ip_str: NbIPAddress} mapping."""

    def test_returns_empty_dict_for_empty_list(self):
        from netbox_kea.sync import bulk_fetch_netbox_ips

        self.assertEqual(bulk_fetch_netbox_ips([]), {})

    def test_returns_matching_ips(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import bulk_fetch_netbox_ips

        NbIP.objects.create(address="10.1.0.1/24", status="active")
        NbIP.objects.create(address="10.1.0.2/24", status="active")
        result = bulk_fetch_netbox_ips(["10.1.0.1", "10.1.0.99"])
        self.assertIn("10.1.0.1", result)
        self.assertNotIn("10.1.0.99", result)

    def test_ignores_ips_not_in_netbox(self):
        from netbox_kea.sync import bulk_fetch_netbox_ips

        result = bulk_fetch_netbox_ips(["99.99.99.99"])
        self.assertEqual(result, {})

    def test_result_value_is_nbip_object(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import bulk_fetch_netbox_ips

        ip = NbIP.objects.create(address="10.2.0.5/24", status="reserved")
        result = bulk_fetch_netbox_ips(["10.2.0.5"])
        self.assertEqual(result["10.2.0.5"].pk, ip.pk)


class TestUpdateMacDescription(TestCase):
    """The public MAC synchronizer updates the hostname token on real rows."""

    def _make_mac(self, description="", has_interface=False):
        from dcim.models import MACAddress
        from virtualization.models import VirtualMachine, VMInterface

        mac = MACAddress.objects.create(mac_address="aa:bb:cc:dd:ee:ff", description=description)
        if has_interface:
            vm = VirtualMachine.objects.create(name="mac-test")
            interface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
            mac.assigned_object = interface
            mac.save()
        return mac

    def _call(self, mac_obj, hostname):
        from netbox_kea.sync import sync_mac_address

        old_description = mac_obj.description
        result = sync_mac_address(str(mac_obj.mac_address), hostname)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, mac_obj.pk)
        mac_obj.refresh_from_db()
        return mac_obj.description != old_description

    def test_sets_description_on_empty_no_interface(self):
        mac = self._make_mac(description="")
        changed = self._call(mac, "myhost.example.com")
        self.assertTrue(changed)
        self.assertEqual(mac.description, "dhcp_hostname: myhost.example.com")

    def test_replaces_description_fully_when_no_interface(self):
        """No interface: replace the entire description with dhcp_hostname: value."""
        mac = self._make_mac(description="old manual description", has_interface=False)
        changed = self._call(mac, "newhost.example.com")
        self.assertTrue(changed)
        self.assertEqual(mac.description, "dhcp_hostname: newhost.example.com")

    def test_replaces_existing_token_when_no_interface(self):
        mac = self._make_mac(description="dhcp_hostname: oldhost.example.com", has_interface=False)
        changed = self._call(mac, "newhost.example.com")
        self.assertTrue(changed)
        self.assertEqual(mac.description, "dhcp_hostname: newhost.example.com")

    def test_no_change_when_same_hostname(self):
        mac = self._make_mac(description="dhcp_hostname: same.example.com", has_interface=False)
        changed = self._call(mac, "same.example.com")
        self.assertFalse(changed)

    def test_appends_token_when_has_interface_and_other_text(self):
        """Has interface: append dhcp_hostname: to existing description."""
        mac = self._make_mac(description="eth0 primary", has_interface=True)
        changed = self._call(mac, "server.example.com")
        self.assertTrue(changed)
        self.assertIn("dhcp_hostname: server.example.com", mac.description)
        self.assertIn("eth0 primary", mac.description)

    def test_replaces_existing_token_when_has_interface(self):
        """Has interface: replace only the dhcp_hostname: portion, keep manual text."""
        mac = self._make_mac(description="eth0 primary | dhcp_hostname: old.example.com", has_interface=True)
        changed = self._call(mac, "new.example.com")
        self.assertTrue(changed)
        self.assertIn("dhcp_hostname: new.example.com", mac.description)
        self.assertIn("eth0 primary", mac.description)
        self.assertNotIn("old.example.com", mac.description)

    def test_sets_description_on_empty_with_interface(self):
        """Even with an interface, an empty description gets set."""
        mac = self._make_mac(description="", has_interface=True)
        changed = self._call(mac, "host.example.com")
        self.assertTrue(changed)
        self.assertEqual(mac.description, "dhcp_hostname: host.example.com")

    def test_caps_description_at_200_chars(self):
        long_host = "h" * 250
        mac = self._make_mac(description="", has_interface=False)
        self._call(mac, long_host)
        self.assertLessEqual(len(mac.description), 200)


class TestSyncMacAddressErrors(TestCase):
    """sync_mac_address handles DB and parse errors gracefully."""

    def test_db_error_is_caught_and_logged(self):
        """ProgrammingError during get_or_create is caught; no exception propagates."""
        from unittest.mock import patch

        from django.db.utils import ProgrammingError

        try:
            from dcim.models import MACAddress
        except ImportError:
            self.skipTest("MACAddress not available in this NetBox version")

        try:
            from netaddr import EUI  # noqa: F401
        except ImportError:
            self.skipTest("netaddr not available")

        from netbox_kea.sync import sync_mac_address

        with patch.object(MACAddress.objects, "get_or_create", side_effect=ProgrammingError("boom")) as mock_goc:
            sync_mac_address("aa:bb:cc:dd:ee:ff", hostname="test-host")
        # Verify get_or_create was actually invoked (not bypassed by an earlier error)
        mock_goc.assert_called()

    def test_parse_error_is_caught_and_logged(self):
        """Invalid MAC string (caught by netaddr) does not propagate an exception."""
        try:
            from dcim.models import MACAddress  # noqa: F401
        except ImportError:
            self.skipTest("MACAddress not available in this NetBox version")

        try:
            from netaddr import EUI  # noqa: F401
        except ImportError:
            self.skipTest("netaddr not available")

        from netbox_kea.sync import sync_mac_address

        # Passing an obviously invalid MAC address exercises the AddrFormatError path.
        sync_mac_address("not-a-mac", hostname="test-host")
        # No exception should propagate — AddrFormatError is caught and logged.

    def test_the_log_message_does_not_contain_the_mac_address(self):
        from netbox_kea.sync import sync_mac_address

        with self.assertLogs("netbox_kea.sync", "DEBUG") as logs:
            sync_mac_address("aa:bb:cc:dd:ee:zz", hostname="test-host")

        self.assertEqual([record.levelname for record in logs.records], ["DEBUG"])
        # The formatted record includes the traceback text, where netaddr repeats the value.
        self.assertNotIn("aa:bb:cc:dd:ee:zz", logging.Formatter().format(logs.records[0]))


class TestSyncMacAddressImportErrors(TestCase):
    """sync_mac_address: ImportError for dcim.models and netaddr."""

    def test_dcim_import_error_returns_silently(self):
        """When dcim.models cannot be imported, sync_mac_address returns without raising."""
        import sys
        from unittest.mock import patch

        # Remove cached module so the import inside sync_mac_address triggers ImportError
        with patch.dict(sys.modules, {"dcim.models": None}):
            # Need to reload sync so the inner import runs fresh
            import importlib

            import netbox_kea.sync as sync_mod

            importlib.reload(sync_mod)
            # Should not raise even when dcim is unavailable
            sync_mod.sync_mac_address("aa:bb:cc:dd:ee:ff", hostname="test")

    def test_netaddr_import_error_returns_silently(self):
        """When netaddr cannot be imported, sync_mac_address logs debug and returns."""
        import sys
        import types
        from unittest.mock import patch

        # Inject a fake dcim.models so that import inside sync succeeds for the
        # dcim path but netaddr import fails, exercising the netaddr fallback.
        fake_dcim = types.ModuleType("dcim.models")
        fake_dcim.MACAddress = type("MACAddress", (), {})
        with patch.dict(sys.modules, {"netaddr": None, "netaddr.core": None, "dcim.models": fake_dcim}):
            import importlib

            import netbox_kea.sync as sync_mod

            importlib.reload(sync_mod)
            # Should not raise even when netaddr is unavailable
            sync_mod.sync_mac_address("aa:bb:cc:dd:ee:ff", hostname="test")


class TestDuplicateRowsListUrl(TestCase):
    """The list URL in DuplicateNetBoxRowsError shows exactly the duplicates, and only to a viewer with permission."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from ipam.models import VRF

        self.vrf = VRF.objects.create(name="kea-dup-vrf")
        self.user = get_user_model().objects.create_user(username="dup-viewer")
        self.client.force_login(self.user)

    def _grant_view(self, model):
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        perm = ObjectPermission.objects.create(name=f"view-{model._meta.model_name}", actions=["view"])
        perm.object_types.add(ContentType.objects.get_for_model(model))
        perm.users.add(self.user)

    def _listed_pks(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return sorted(row.pk for row in response.context["table"].data)

    def _duplicate_error(self, vrf, network, *, pool=None):
        from netbox_kea.ipam_reconciliation import PoolPhase, SubnetPhase, read_catalogue, reconcile

        server = _make_db_server(
            name=f"duplicates-{vrf.pk if vrf else 'global'}-{'pool' if pool else 'subnet'}", sync_vrf=vrf
        )
        subnet = {"id": 1, "subnet": network}
        if pool is not None:
            subnet["pools"] = [{"pool": pool}]
        with stub_kea(_catalogue_responses_for_subnets(4, [subnet])):
            observation = read_catalogue(server, 4)
            report = reconcile(server, 4, [PoolPhase(observation) if pool else SubnetPhase(observation)])
        self.assertEqual(report.prefix_errors, 1)
        self.assertEqual(len(report.duplicates), 1)
        return report.duplicates[0]

    def _range_error(self, vrf):
        return self._duplicate_error(vrf, "192.168.13.0/24", pool="192.168.13.50-192.168.13.100")

    def _prefix_error(self, vrf):
        return self._duplicate_error(vrf, "10.7.0.0/24")

    def _make_ranges(self):
        from ipam.models import IPRange
        from netaddr import IPNetwork

        def make(start, end, vrf):
            return IPRange.objects.create(start_address=IPNetwork(start), end_address=IPNetwork(end), vrf=vrf).pk

        return {
            None: [
                make("192.168.13.50/24", "192.168.13.100/24", None),
                make("192.168.13.50/25", "192.168.13.100/25", None),
            ],
            self.vrf: [
                make("192.168.13.50/24", "192.168.13.100/24", self.vrf),
                make("192.168.13.50/24", "192.168.13.100/24", self.vrf),
            ],
            "decoy": [make("192.168.13.50/24", "192.168.13.101/24", None)],
        }

    def _make_prefixes(self):
        from ipam.models import Prefix

        def make(vrf):
            return Prefix.objects.create(prefix="10.7.0.0/24", vrf=vrf).pk

        return {None: [make(None), make(None)], self.vrf: [make(self.vrf), make(self.vrf)]}

    def test_range_url_lists_exactly_the_duplicates_in_each_vrf(self):
        from ipam.models import IPRange

        pks = self._make_ranges()
        self._grant_view(IPRange)
        for vrf in (None, self.vrf):
            with self.subTest(vrf=vrf):
                error = self._range_error(vrf)
                self.assertEqual(error.pks, pks[vrf])
                self.assertEqual(self._listed_pks(error.list_url), pks[vrf])

    def test_prefix_url_lists_exactly_the_duplicates_in_each_vrf(self):
        from ipam.models import Prefix

        pks = self._make_prefixes()
        self._grant_view(Prefix)
        for vrf in (None, self.vrf):
            with self.subTest(vrf=vrf):
                error = self._prefix_error(vrf)
                self.assertEqual(error.pks, pks[vrf])
                self.assertEqual(self._listed_pks(error.list_url), pks[vrf])

    def test_url_shows_nothing_without_ipam_view_permission(self):
        self._make_ranges()
        self._make_prefixes()
        for error in (self._range_error(None), self._prefix_error(None)):
            with self.subTest(url=error.list_url):
                self.assertEqual(self.client.get(error.list_url).status_code, 403)


class TestIsKeaManagedIP(TestCase):
    """is_kea_managed_ip classifies an existing NetBox IP as safe-to-overwrite or foreign."""

    def test_blank_description_is_not_managed(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import is_kea_managed_ip

        ip = NbIP(address="10.0.0.1/24", status="active", description="")
        self.assertFalse(is_kea_managed_ip(ip))

    def test_synced_description_is_managed(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import is_kea_managed_ip

        for description in ("[kea-sync: lease]", "[kea-sync: lease] note", "Synced from Kea DHCP lease"):
            ip = NbIP(address="10.0.0.2/24", status="active", description=description)
            self.assertTrue(is_kea_managed_ip(ip), description)

    def test_foreign_description_is_not_managed(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import is_kea_managed_ip

        for description in ("Router loopback", "rack 4 [kea-sync: lease]", "[kea-sync: lease ]"):
            ip = NbIP(address="10.0.0.3/24", status="active", description=description)
            self.assertFalse(is_kea_managed_ip(ip), description)


@override_settings(PLUGINS_CONFIG=plugins_config())
class TestOwnershipClaimBehavior(TestCase):
    """Keep the former writer regressions at the public ownership boundary."""

    def setUp(self):
        self.server = _make_db_server()

    def _lease(self, *, hostname="host.example.invalid", force=False):
        from netbox_kea.ipam_reconciliation import claim

        record = {
            "ip-address": "198.18.0.20",
            "subnet-id": 1,
            "hostname": hostname,
            "hw-address": "aa:bb:cc:dd:ee:ff",
        }
        with stub_kea(_catalogue_responses_for_subnets(4, [{"id": 1, "subnet": "198.18.0.0/24"}])):
            return claim(self.server, 4, [typed_lease(complete_lease(record))], force=force).addresses["198.18.0.20"]

    def _reservation(self, *, force=False):
        from netbox_kea.ipam_reconciliation import claim

        reservation = _typed_reservation(
            {
                "ip-address": "198.18.0.20",
                "subnet-id": 1,
                "hostname": "host.example.invalid",
                "hw-address": "aa:bb:cc:dd:ee:ff",
            },
            prefix_length=24,
        )
        return claim(self.server, 4, [reservation], force=force).addresses["198.18.0.20"]

    def test_repeated_lease_claim_is_idempotent_and_keeps_the_mac(self):
        from dcim.models import MACAddress
        from ipam.models import IPAddress

        first = self._lease()
        second = self._lease()
        self.assertEqual((first.outcome, second.outcome), ("created", "unchanged"))
        self.assertEqual(first.ip.pk, second.ip.pk)
        self.assertEqual(IPAddress.objects.count(), 1)
        self.assertEqual(MACAddress.objects.count(), 1)
        self.assertEqual(MACAddress.objects.get().description, "dhcp_hostname: host.example.invalid")

    def test_changed_hostname_updates_ip_and_mac(self):
        from dcim.models import MACAddress

        self._lease()
        result = self._lease(hostname="changed.example.invalid")
        self.assertEqual(result.outcome, "updated")
        self.assertEqual(result.ip.dns_name, "changed.example.invalid")
        self.assertEqual(MACAddress.objects.get().description, "dhcp_hostname: changed.example.invalid")

    def test_hostless_report_preserves_manual_dns_name(self):
        result = self._lease()
        result.ip.dns_name = "operator.example.invalid"
        result.ip.save()
        result = self._lease(hostname="")
        self.assertEqual(result.ip.dns_name, "operator.example.invalid")

    def test_subnet_mask_corrects_the_same_legacy_row(self):
        from ipam.models import IPAddress

        ip = IPAddress.objects.create(address="198.18.0.20/32", status="dhcp", description="[kea-sync: lease]")
        result = self._lease()
        self.assertEqual(result.ip.pk, ip.pk)
        self.assertEqual(str(result.ip.address), "198.18.0.20/24")

    def test_reservation_mask_corrects_the_same_legacy_row(self):
        from ipam.models import IPAddress

        ip = IPAddress.objects.create(
            address="198.18.0.20/32", status="reserved", description="[kea-sync: reservation]"
        )
        result = self._reservation()
        self.assertEqual(result.ip.pk, ip.pk)
        self.assertEqual(str(result.ip.address), "198.18.0.20/24")

    def test_both_sources_keep_active_status_on_repeated_claims(self):
        self._lease()
        result = self._reservation()
        self.assertEqual(result.ip.status, "active")
        self.assertEqual(result.ip.description, "[kea-sync: lease + reservation]")
        self.assertEqual(self._lease().ip.status, "active")
        self.assertEqual(self._reservation().ip.status, "active")

    def test_legacy_marker_note_survives_both_source_claims(self):
        from ipam.models import IPAddress

        IPAddress.objects.create(
            address="198.18.0.20/32",
            status="dhcp",
            description="Synced from Kea DHCP lease operator note",
        )
        self._lease()
        result = self._reservation()
        self.assertEqual(result.ip.description, "[kea-sync: lease + reservation] operator note")

    def test_blank_and_foreign_rows_need_an_explicit_force(self):
        from ipam.models import IPAddress

        for description in ("", "Operator row"):
            with self.subTest(description=description):
                IPAddress.objects.all().delete()
                row = IPAddress.objects.create(address="198.18.0.20/32", status="active", description=description)
                self.assertEqual(self._lease().outcome, "conflict")
                row.refresh_from_db()
                self.assertEqual((str(row.address), row.description), ("198.18.0.20/32", description))
                claimed = self._lease(force=True)
                self.assertTrue(claimed.synchronized)
                self.assertEqual(claimed.ip.pk, row.pk)
                self.assertEqual(str(claimed.ip.address), "198.18.0.20/24")
                self.assertEqual(self._lease().outcome, "unchanged")

    def test_forced_claim_preserves_the_full_operator_note(self):
        from ipam.models import IPAddress

        for source, claim_record, status in (
            ("lease", self._lease, "dhcp"),
            ("reservation", self._reservation, "reserved"),
        ):
            for note in ("", "  Printer on floor 2\nKeep this note.  "):
                with self.subTest(source=source, note=note):
                    IPAddress.objects.all().delete()
                    row = IPAddress.objects.create(address="198.18.0.20/32", status="active", description=note)
                    claimed = claim_record(force=True)
                    row.refresh_from_db()
                    expected = f"[kea-sync: {source}]" + (f" {note}" if note else "")
                    self.assertEqual((claimed.outcome, claimed.ip.pk), ("updated", row.pk))
                    self.assertEqual(
                        (str(row.address), row.status, row.dns_name, row.description),
                        ("198.18.0.20/24", status, "host.example.invalid", expected),
                    )
                    self.assertEqual(claim_record().outcome, "unchanged")

    def test_forced_claim_refuses_an_operator_note_that_exceeds_the_description_limit(self):
        from ipam.models import IPAddress

        from netbox_kea.models import IPAMOwnershipLink

        for source, claim_record, status in (
            ("lease", self._lease, "dhcp"),
            ("reservation", self._reservation, "reserved"),
        ):
            for extra, outcome in ((0, "updated"), (1, "conflict")):
                with self.subTest(source=source, extra=extra):
                    IPAddress.objects.all().delete()
                    note = "n" * (200 - len(f"[kea-sync: {source}] ") + extra)
                    row = IPAddress.objects.create(
                        address="198.18.0.20/32", status="active", dns_name="operator.example.invalid", description=note
                    )
                    claimed = claim_record(force=True)
                    row.refresh_from_db()
                    self.assertEqual((claimed.outcome, claimed.ip.pk), (outcome, row.pk))
                    if extra:
                        self.assertFalse(claimed.synchronized)
                        self.assertEqual(
                            (str(row.address), row.status, row.dns_name, row.description),
                            ("198.18.0.20/32", "active", "operator.example.invalid", note),
                        )
                    else:
                        self.assertTrue(claimed.synchronized)
                        self.assertEqual((row.status, row.description), (status, f"[kea-sync: {source}] {note}"))
                        self.assertEqual(len(row.description), 200)
                    link = IPAMOwnershipLink.objects.get(ip_address=row)
                    self.assertEqual((link.server_id, link.source), (self.server.pk, source))
                    self.assertEqual(link.facts, {"hostname": "host.example.invalid", "prefix_length": 24})

    def test_reservation_force_claims_foreign_row_and_corrects_its_mask(self):
        from ipam.models import IPAddress

        row = IPAddress.objects.create(address="198.18.0.20/32", status="active", description="Operator row")
        self.assertEqual(self._reservation().outcome, "conflict")
        result = self._reservation(force=True)
        self.assertEqual(result.ip.pk, row.pk)
        self.assertEqual((str(result.ip.address), result.ip.status), ("198.18.0.20/24", "reserved"))
        self.assertEqual(self._reservation().outcome, "unchanged")

    def test_long_operator_note_refuses_status_change_without_touching_the_row(self):
        from ipam.models import IPAddress

        self._lease()
        row = IPAddress.objects.get()
        block = "[kea-sync: lease] "
        row.description = block + "n" * (200 - len(block))
        row.save()
        before = IPAddress.objects.values().get(pk=row.pk)
        self.assertEqual(self._reservation().outcome, "conflict")
        self.assertEqual(IPAddress.objects.values().get(pk=row.pk), before)
