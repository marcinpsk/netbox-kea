"""Tests for netbox_kea.sync — IPAM synchronization helpers.

All tests that hit the database extend django.test.TestCase so each test
runs in a transaction that is rolled back afterwards.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

# ─────────────────────────────────────────────────────────────────────────────
# TestFindPrefixLength
# ─────────────────────────────────────────────────────────────────────────────


class TestFindPrefixLength(TestCase):
    """find_prefix_length returns the prefix len of the containing NetBox prefix,
    falling back to 32 (IPv4) or 128 (IPv6) when no prefix exists."""

    def test_returns_32_for_ipv4_with_no_prefix(self):
        from netbox_kea.sync import find_prefix_length

        self.assertEqual(find_prefix_length("192.168.99.1"), 32)

    def test_returns_128_for_ipv6_with_no_prefix(self):
        from netbox_kea.sync import find_prefix_length

        self.assertEqual(find_prefix_length("2001:db8::1"), 128)

    def test_returns_prefix_len_from_netbox_when_prefix_exists(self):
        from ipam.models import Prefix

        from netbox_kea.sync import find_prefix_length

        p = Prefix.objects.create(prefix="10.50.0.0/24", status="active")
        try:
            self.assertEqual(find_prefix_length("10.50.0.100"), 24)
        finally:
            p.delete()

    def test_uses_longest_matching_prefix(self):
        """When a /24 and /16 both contain the IP, /24 wins."""
        from ipam.models import Prefix

        from netbox_kea.sync import find_prefix_length

        p16 = Prefix.objects.create(prefix="10.50.0.0/16", status="active")
        p24 = Prefix.objects.create(prefix="10.50.1.0/24", status="active")
        try:
            self.assertEqual(find_prefix_length("10.50.1.5"), 24)
        finally:
            p16.delete()
            p24.delete()


# ─────────────────────────────────────────────────────────────────────────────
# TestGetNetboxIP
# ─────────────────────────────────────────────────────────────────────────────


class TestGetNetboxIP(TestCase):
    """get_netbox_ip returns the first matching NetBox IPAddress or None."""

    def test_returns_none_when_not_found(self):
        from netbox_kea.sync import get_netbox_ip

        self.assertIsNone(get_netbox_ip("172.16.0.99"))

    def test_returns_ip_object_when_found(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import get_netbox_ip

        ip = NbIP.objects.create(address="172.16.0.1/24", status="active")
        result = get_netbox_ip("172.16.0.1")
        self.assertEqual(result.pk, ip.pk)

    def test_matches_regardless_of_prefix_length(self):
        """get_netbox_ip finds the IP even if stored with /24 prefix."""
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import get_netbox_ip

        ip = NbIP.objects.create(address="172.16.5.10/25", status="active")
        result = get_netbox_ip("172.16.5.10")
        self.assertEqual(result.pk, ip.pk)


# ─────────────────────────────────────────────────────────────────────────────
# TestSyncLeaseToNetbox
# ─────────────────────────────────────────────────────────────────────────────


class TestSyncLeaseToNetbox(TestCase):
    """sync_lease_to_netbox creates or updates a NetBox IPAddress from a Kea lease."""

    _LEASE = {
        "ip-address": "192.168.50.100",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "hostname": "testhost.example.com",
        "subnet-id": 1,
    }

    def test_creates_new_ip_when_not_exists(self):
        from netbox_kea.sync import sync_lease_to_netbox

        ip_obj, created = sync_lease_to_netbox(self._LEASE)
        self.assertTrue(created)
        self.assertIsNotNone(ip_obj.pk)

    def test_sets_status_dhcp_for_dynamic_lease(self):
        """A new lease without a pre-existing reservation uses 'dhcp' status."""
        from netbox_kea.sync import sync_lease_to_netbox

        ip_obj, _ = sync_lease_to_netbox(self._LEASE)
        self.assertEqual(ip_obj.status, "dhcp")

    def test_sets_dns_name_from_hostname(self):
        from netbox_kea.sync import sync_lease_to_netbox

        ip_obj, _ = sync_lease_to_netbox(self._LEASE)
        self.assertEqual(ip_obj.dns_name, "testhost.example.com")

    def test_returns_created_false_on_second_call(self):
        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox(self._LEASE)
        _, created = sync_lease_to_netbox(self._LEASE)
        self.assertFalse(created)

    def test_does_not_create_duplicate_ip(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox(self._LEASE)
        sync_lease_to_netbox(self._LEASE)
        self.assertEqual(NbIP.objects.filter(address__startswith="192.168.50.100/").count(), 1)

    def test_updates_dns_name_on_second_call(self):
        """A second sync with a new hostname must update dns_name."""
        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox(self._LEASE)
        updated = {**self._LEASE, "hostname": "new-hostname.example.com"}
        ip_obj, _ = sync_lease_to_netbox(updated)
        self.assertEqual(ip_obj.dns_name, "new-hostname.example.com")

    def test_address_uses_slash32_fallback_when_no_netbox_prefix(self):
        from netbox_kea.sync import sync_lease_to_netbox

        ip_obj, _ = sync_lease_to_netbox(self._LEASE)
        self.assertTrue(str(ip_obj.address).endswith("/32"))

    def test_address_uses_prefix_len_when_netbox_prefix_exists(self):
        from ipam.models import Prefix

        from netbox_kea.sync import sync_lease_to_netbox

        p = Prefix.objects.create(prefix="192.168.50.0/24", status="active")
        try:
            ip_obj, _ = sync_lease_to_netbox(self._LEASE)
            self.assertTrue(str(ip_obj.address).endswith("/24"))
        finally:
            p.delete()

    def test_description_contains_kea(self):
        from netbox_kea.sync import sync_lease_to_netbox

        ip_obj, _ = sync_lease_to_netbox(self._LEASE)
        self.assertIn("Kea", ip_obj.description)

    def test_works_without_hostname(self):
        """Leases without a hostname field must not error."""
        from netbox_kea.sync import sync_lease_to_netbox

        lease = {k: v for k, v in self._LEASE.items() if k != "hostname"}
        ip_obj, created = sync_lease_to_netbox(lease)
        self.assertTrue(created)
        self.assertEqual(ip_obj.status, "dhcp")

    def test_second_sync_does_not_overwrite_manual_dns_name(self):
        """If user has manually set a dns_name and lease has no hostname, keep it."""
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_lease_to_netbox

        # Pre-create IP with manual dns_name
        NbIP.objects.create(
            address="192.168.50.100/32",
            status="reserved",
            dns_name="manual.example.com",
        )
        lease_no_hostname = {k: v for k, v in self._LEASE.items() if k != "hostname"}
        ip_obj, _ = sync_lease_to_netbox(lease_no_hostname)
        # dns_name should be preserved since lease has no hostname
        self.assertEqual(ip_obj.dns_name, "manual.example.com")

    # ── F8: MAC address sync ──────────────────────────────────────────────────

    def test_sync_lease_creates_mac_address_entry(self):
        """F8: sync_lease_to_netbox creates a MACAddress DCIM entry when hw-address is present."""
        try:
            from dcim.models import MACAddress
        except (ImportError, AttributeError):
            self.skipTest("MACAddress not available in this NetBox version")
        try:
            import netaddr  # noqa: F401
        except ImportError:
            self.skipTest("netaddr not available")
        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox(self._LEASE)
        self.assertEqual(MACAddress.objects.count(), 1)

    def test_sync_lease_does_not_create_mac_when_no_hw_address(self):
        """F8: sync_lease_to_netbox skips MACAddress when lease has no hw-address."""
        try:
            from dcim.models import MACAddress
        except (ImportError, AttributeError):
            self.skipTest("MACAddress not available in this NetBox version")
        try:
            import netaddr  # noqa: F401
        except ImportError:
            self.skipTest("netaddr not available")
        from netbox_kea.sync import sync_lease_to_netbox

        lease = {"ip-address": "192.168.50.101", "hostname": "nomaclease"}
        sync_lease_to_netbox(lease)
        self.assertEqual(MACAddress.objects.count(), 0)

    def test_sync_lease_does_not_create_duplicate_mac(self):
        """F8: Calling sync_lease_to_netbox twice does not create duplicate MACAddress entries."""
        try:
            from dcim.models import MACAddress
        except (ImportError, AttributeError):
            self.skipTest("MACAddress not available in this NetBox version")
        try:
            import netaddr  # noqa: F401
        except ImportError:
            self.skipTest("netaddr not available")
        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox(self._LEASE)
        sync_lease_to_netbox(self._LEASE)
        self.assertEqual(MACAddress.objects.count(), 1)


# ─────────────────────────────────────────────────────────────────────────────
# TestSyncReservationToNetbox
# ─────────────────────────────────────────────────────────────────────────────


class TestSyncReservationToNetbox(TestCase):
    """sync_reservation_to_netbox creates IPAddress with status=reserved."""

    _RESERVATION = {
        "ip-address": "192.168.51.200",
        "hw-address": "11:22:33:44:55:66",
        "hostname": "reserved-host.example.com",
        "subnet-id": 1,
    }

    def test_creates_ip_with_reserved_status(self):
        from netbox_kea.sync import sync_reservation_to_netbox

        ip_obj, created = sync_reservation_to_netbox(self._RESERVATION)
        self.assertTrue(created)
        self.assertEqual(ip_obj.status, "reserved")

    def test_sets_dns_name_from_hostname(self):
        from netbox_kea.sync import sync_reservation_to_netbox

        ip_obj, _ = sync_reservation_to_netbox(self._RESERVATION)
        self.assertEqual(ip_obj.dns_name, "reserved-host.example.com")

    def test_does_not_create_duplicate(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_reservation_to_netbox

        sync_reservation_to_netbox(self._RESERVATION)
        sync_reservation_to_netbox(self._RESERVATION)
        self.assertEqual(NbIP.objects.filter(address__startswith="192.168.51.200/").count(), 1)

    def test_returns_created_false_on_second_call(self):
        from netbox_kea.sync import sync_reservation_to_netbox

        sync_reservation_to_netbox(self._RESERVATION)
        _, created = sync_reservation_to_netbox(self._RESERVATION)
        self.assertFalse(created)

    def test_raises_on_reservation_with_no_ip(self):
        from netbox_kea.sync import sync_reservation_to_netbox

        with self.assertRaises(ValueError):
            sync_reservation_to_netbox({"hw-address": "11:22:33:44:55:66"})

    def test_description_contains_kea(self):
        from netbox_kea.sync import sync_reservation_to_netbox

        ip_obj, _ = sync_reservation_to_netbox(self._RESERVATION)
        self.assertIn("Kea", ip_obj.description)

    def test_updates_existing_active_ip_stays_active_with_reservation_sync(self):
        """An existing 'active' IP (has a lease) stays 'active' when a reservation is synced."""
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_reservation_to_netbox

        NbIP.objects.create(address="192.168.51.200/32", status="active")
        ip_obj, created = sync_reservation_to_netbox(self._RESERVATION)
        self.assertFalse(created)
        self.assertEqual(ip_obj.status, "active")

    # ── F8: MAC address sync ──────────────────────────────────────────────────

    def test_sync_reservation_creates_mac_address_entry(self):
        """F8: sync_reservation_to_netbox creates a MACAddress DCIM entry when hw-address is present."""
        try:
            from dcim.models import MACAddress
        except (ImportError, AttributeError):
            self.skipTest("MACAddress not available in this NetBox version")
        from netbox_kea.sync import sync_reservation_to_netbox

        sync_reservation_to_netbox(self._RESERVATION)
        self.assertEqual(MACAddress.objects.count(), 1)

    def test_sync_reservation_skips_mac_when_no_hw_address(self):
        """F8: sync_reservation_to_netbox skips MACAddress when reservation has no hw-address."""
        try:
            from dcim.models import MACAddress
        except (ImportError, AttributeError):
            self.skipTest("MACAddress not available in this NetBox version")
        from netbox_kea.sync import sync_reservation_to_netbox

        reservation = {"ip-address": "192.168.51.201", "duid": "00:01:02:03", "subnet-id": 1}
        sync_reservation_to_netbox(reservation)
        self.assertEqual(MACAddress.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# TestBulkFetchNetboxIPs
# ─────────────────────────────────────────────────────────────────────────────


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


# ─────────────────────────────────────────────────────────────────────────────
# Issue #3: Multi-address IPv6 reservation sync
# ─────────────────────────────────────────────────────────────────────────────


class TestSyncReservationMultiAddressV6(TestCase):
    """sync_reservation_to_netbox must sync ALL ip-addresses for DHCPv6."""

    def test_syncs_all_addresses_from_ip_addresses_list(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_reservation_to_netbox

        reservation = {
            "ip-addresses": ["2001:db8::1", "2001:db8::2"],
            "duid": "aa:bb:cc:dd:ee:ff",
            "hostname": "v6host.example.com",
            "subnet-id": 1,
        }
        ip_obj, created = sync_reservation_to_netbox(reservation)
        self.assertTrue(created)
        self.assertTrue(
            NbIP.objects.filter(address__startswith="2001:db8::1/").exists(),
            "First address must be synced",
        )
        self.assertTrue(
            NbIP.objects.filter(address__startswith="2001:db8::2/").exists(),
            "Second address must also be synced",
        )

    def test_returns_first_address_as_primary(self):
        from netbox_kea.sync import sync_reservation_to_netbox

        reservation = {
            "ip-addresses": ["2001:db8::10", "2001:db8::11"],
            "duid": "11:22:33:44:55:66",
            "subnet-id": 2,
        }
        ip_obj, _ = sync_reservation_to_netbox(reservation)
        self.assertTrue(str(ip_obj.address).startswith("2001:db8::10/"))

    def test_single_ip_address_field_still_works(self):
        """Backward compat: ip-address (singular) still works."""
        from netbox_kea.sync import sync_reservation_to_netbox

        reservation = {"ip-address": "10.0.0.55", "hw-address": "aa:bb:cc:dd:ee:01", "subnet-id": 1}
        ip_obj, created = sync_reservation_to_netbox(reservation)
        self.assertTrue(created)
        self.assertTrue(str(ip_obj.address).startswith("10.0.0.55/"))


# ─────────────────────────────────────────────────────────────────────────────
# P1 — IP Status Semantics (dhcp / reserved / active)
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeIpStatus(TestCase):
    """_compute_ip_status returns the correct NetBox IP status based on source and current state."""

    def _call(self, desired_from, current_status):
        from netbox_kea.sync import _compute_ip_status

        return _compute_ip_status(desired_from, current_status)

    # ── lease sync ─────────────────────────────────────────────────────────────
    def test_new_ip_lease_sync_returns_dhcp(self):
        self.assertEqual(self._call("lease", None), "dhcp")

    def test_existing_deprecated_ip_lease_sync_returns_dhcp(self):
        self.assertEqual(self._call("lease", "deprecated"), "dhcp")

    def test_existing_reserved_ip_lease_sync_returns_active(self):
        """Reserved IP + lease → both reservation and lease → active."""
        self.assertEqual(self._call("lease", "reserved"), "active")

    def test_existing_active_ip_lease_sync_stays_active(self):
        """Already-active IP + another lease sync → stays active (don't downgrade)."""
        self.assertEqual(self._call("lease", "active"), "active")

    def test_existing_dhcp_ip_lease_sync_stays_dhcp(self):
        self.assertEqual(self._call("lease", "dhcp"), "dhcp")

    # ── reservation sync ───────────────────────────────────────────────────────
    def test_new_ip_reservation_sync_returns_reserved(self):
        self.assertEqual(self._call("reservation", None), "reserved")

    def test_existing_dhcp_ip_reservation_sync_returns_active(self):
        """dhcp IP + reservation → now has both → active."""
        self.assertEqual(self._call("reservation", "dhcp"), "active")

    def test_existing_active_ip_reservation_sync_stays_active(self):
        self.assertEqual(self._call("reservation", "active"), "active")

    def test_existing_deprecated_ip_reservation_sync_returns_reserved(self):
        self.assertEqual(self._call("reservation", "deprecated"), "reserved")

    def test_existing_reserved_ip_reservation_sync_stays_reserved(self):
        """Re-syncing a reservation keeps the IP reserved (no lease exists)."""
        self.assertEqual(self._call("reservation", "reserved"), "reserved")


class TestSyncLeaseStatusSemantics(TestCase):
    """Integration: sync_lease_to_netbox uses dhcp/active semantics correctly."""

    _LEASE = {
        "ip-address": "10.10.0.50",
        "hw-address": "ca:fe:00:00:00:01",
        "hostname": "device-a.example.com",
    }

    def test_new_lease_gets_dhcp_status(self):
        from netbox_kea.sync import sync_lease_to_netbox

        ip_obj, _ = sync_lease_to_netbox(self._LEASE)
        self.assertEqual(ip_obj.status, "dhcp")

    def test_lease_upgrades_reserved_to_active(self):
        """IP was reserved (from a previous reservation sync), now also has a lease → active."""
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_lease_to_netbox

        NbIP.objects.create(address="10.10.0.50/32", status="reserved", description="Synced from Kea DHCP reservation")
        ip_obj, _ = sync_lease_to_netbox(self._LEASE)
        self.assertEqual(ip_obj.status, "active")

    def test_lease_keeps_active_when_already_active(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_lease_to_netbox

        NbIP.objects.create(address="10.10.0.50/32", status="active", description="Synced from Kea DHCP lease")
        ip_obj, _ = sync_lease_to_netbox(self._LEASE)
        self.assertEqual(ip_obj.status, "active")


class TestSyncReservationStatusSemantics(TestCase):
    """Integration: sync_reservation_to_netbox uses reserved/active semantics correctly."""

    _RESERVATION = {
        "ip-address": "10.10.0.60",
        "hw-address": "ca:fe:00:00:00:02",
        "hostname": "device-b.example.com",
    }

    def test_new_reservation_gets_reserved_status(self):
        from netbox_kea.sync import sync_reservation_to_netbox

        ip_obj, created = sync_reservation_to_netbox(self._RESERVATION)
        self.assertTrue(created)
        self.assertEqual(ip_obj.status, "reserved")

    def test_reservation_upgrades_dhcp_to_active(self):
        """IP was dhcp (from a lease sync), now also has a reservation → active."""
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_reservation_to_netbox

        NbIP.objects.create(address="10.10.0.60/32", status="dhcp", description="Synced from Kea DHCP lease")
        ip_obj, _ = sync_reservation_to_netbox(self._RESERVATION)
        self.assertEqual(ip_obj.status, "active")

    def test_reservation_sync_keeps_active_when_already_active(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_reservation_to_netbox

        NbIP.objects.create(address="10.10.0.60/32", status="active", description="Synced from Kea DHCP lease")
        ip_obj, _ = sync_reservation_to_netbox(self._RESERVATION)
        self.assertEqual(ip_obj.status, "active")


# ─────────────────────────────────────────────────────────────────────────────
# P2 — MAC Address Description Annotation
# ─────────────────────────────────────────────────────────────────────────────


class TestUpdateMacDescription(TestCase):
    """_update_mac_description annotates a MACAddress.description with dhcp_hostname: token."""

    def _make_mac(self, description="", has_interface=False):
        """Create a minimal MAC-like object for testing _update_mac_description."""
        try:
            from dcim.models import MACAddress  # noqa: F401 — just to skip if unavailable
        except ImportError:
            self.skipTest("MACAddress not available")
        import types

        return types.SimpleNamespace(description=description, assigned_object=object() if has_interface else None)

    def _call(self, mac_obj, hostname):
        from netbox_kea.sync import _update_mac_description

        return _update_mac_description(mac_obj, hostname)

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


class TestSyncMacAddressWithHostname(TestCase):
    """sync_lease_to_netbox sets dhcp_hostname: on the MAC description."""

    _LEASE = {
        "ip-address": "10.20.0.1",
        "hw-address": "de:ad:be:ef:00:01",
        "hostname": "host-a.example.com",
    }

    def test_sync_lease_sets_mac_description(self):
        try:
            from dcim.models import MACAddress
        except ImportError:
            self.skipTest("MACAddress not available")
        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox(self._LEASE)
        mac = MACAddress.objects.first()
        self.assertIsNotNone(mac)
        self.assertIn("dhcp_hostname: host-a.example.com", mac.description)

    def test_sync_lease_updates_mac_description_on_resync(self):
        try:
            from dcim.models import MACAddress
        except ImportError:
            self.skipTest("MACAddress not available")
        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox(self._LEASE)
        updated = {**self._LEASE, "hostname": "renamed-host.example.com"}
        sync_lease_to_netbox(updated)
        mac = MACAddress.objects.first()
        self.assertIn("dhcp_hostname: renamed-host.example.com", mac.description)
        self.assertNotIn("host-a.example.com", mac.description)

    def test_sync_lease_skips_mac_description_when_no_hostname(self):
        try:
            from dcim.models import MACAddress
        except ImportError:
            self.skipTest("MACAddress not available")
        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox({"ip-address": "10.20.0.2", "hw-address": "de:ad:be:ef:00:02"})
        mac = MACAddress.objects.first()
        self.assertIsNotNone(mac)
        self.assertEqual(mac.description, "")


# ─────────────────────────────────────────────────────────────────────────────
# P4 — Stale IP Cleanup
# ─────────────────────────────────────────────────────────────────────────────

_STALE_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30, "stale_ip_cleanup": "remove"}}
_DEPRECATE_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30, "stale_ip_cleanup": "deprecate"}}
_NONE_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30, "stale_ip_cleanup": "none"}}


class TestCleanupStaleIps(TestCase):
    """_cleanup_stale_ips removes or deprecates old Kea-synced IPs for the same hostname."""

    _HOSTNAME = "moving-device.example.com"
    _OLD_IP = "10.30.0.10"
    _NEW_IP = "10.30.0.20"
    _KEA_DESC = "Synced from Kea DHCP lease"

    def _create_old_ip(self, status="dhcp", description=None):
        from ipam.models import IPAddress as NbIP

        return NbIP.objects.create(
            address=f"{self._OLD_IP}/32",
            status=status,
            dns_name=self._HOSTNAME,
            description=description or self._KEA_DESC,
        )

    def _call(self, mode="remove"):
        from netbox_kea.sync import _cleanup_stale_ips

        return _cleanup_stale_ips(self._NEW_IP, self._HOSTNAME, mode=mode)

    def test_removes_stale_ip_in_remove_mode(self):
        from ipam.models import IPAddress as NbIP

        self._create_old_ip()
        count = self._call(mode="remove")
        self.assertEqual(count, 1)
        self.assertFalse(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())

    def test_deprecates_stale_ip_in_deprecate_mode(self):
        from ipam.models import IPAddress as NbIP

        self._create_old_ip()
        count = self._call(mode="deprecate")
        self.assertEqual(count, 1)
        ip = NbIP.objects.get(address__startswith=f"{self._OLD_IP}/")
        self.assertEqual(ip.status, "deprecated")

    def test_does_nothing_in_none_mode(self):
        from ipam.models import IPAddress as NbIP

        self._create_old_ip()
        count = self._call(mode="none")
        self.assertEqual(count, 0)
        self.assertTrue(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())

    def test_skips_ips_without_kea_description(self):
        from ipam.models import IPAddress as NbIP

        self._create_old_ip(description="Manually assigned by ops team")
        count = self._call(mode="remove")
        self.assertEqual(count, 0)
        self.assertTrue(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())

    def test_skips_ips_with_different_hostname(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import _cleanup_stale_ips

        NbIP.objects.create(
            address=f"{self._OLD_IP}/32",
            status="dhcp",
            dns_name="other-device.example.com",
            description=self._KEA_DESC,
        )
        count = _cleanup_stale_ips(self._NEW_IP, self._HOSTNAME, mode="remove")
        self.assertEqual(count, 0)
        self.assertTrue(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())

    def test_does_not_remove_current_ip(self):
        """The IP being synced is never touched by stale cleanup."""
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import _cleanup_stale_ips

        NbIP.objects.create(
            address=f"{self._NEW_IP}/32",
            status="dhcp",
            dns_name=self._HOSTNAME,
            description=self._KEA_DESC,
        )
        count = _cleanup_stale_ips(self._NEW_IP, self._HOSTNAME, mode="remove")
        self.assertEqual(count, 0)
        self.assertTrue(NbIP.objects.filter(address__startswith=f"{self._NEW_IP}/").exists())

    def test_does_not_remove_deprecated_ips(self):
        """Already-deprecated IPs are not touched (not in (dhcp, active, reserved))."""
        self._create_old_ip(status="deprecated")
        count = self._call(mode="remove")
        self.assertEqual(count, 0)

    def test_does_not_cross_family_cleanup_ipv4_for_ipv6(self):
        """Syncing an IPv4 address must not remove an IPv6 IP with same hostname."""
        from ipam.models import IPAddress as NbIP

        NbIP.objects.create(
            address="2001:db8::1/128",
            status="dhcp",
            dns_name=self._HOSTNAME,
            description=self._KEA_DESC,
        )
        count = self._call(mode="remove")
        self.assertEqual(count, 0)
        self.assertTrue(NbIP.objects.filter(address__startswith="2001:db8::1/").exists())

    def test_skips_when_no_hostname(self):
        """No hostname → no cleanup (can't match safely)."""
        from netbox_kea.sync import _cleanup_stale_ips

        self._create_old_ip()
        count = _cleanup_stale_ips(self._NEW_IP, "", mode="remove")
        self.assertEqual(count, 0)


class TestSyncLeaseWithStaleCleanup(TestCase):
    """Integration: sync_lease_to_netbox removes stale IPs via PLUGINS_CONFIG."""

    _LEASE_NEW = {
        "ip-address": "10.40.0.20",
        "hw-address": "aa:bb:cc:dd:00:01",
        "hostname": "migrated-host.example.com",
    }
    _OLD_IP = "10.40.0.10"

    def _create_old_kea_ip(self, status="dhcp"):
        from ipam.models import IPAddress as NbIP

        return NbIP.objects.create(
            address=f"{self._OLD_IP}/32",
            status=status,
            dns_name="migrated-host.example.com",
            description="Synced from Kea DHCP lease",
        )

    @override_settings(PLUGINS_CONFIG={"netbox_kea": {"kea_timeout": 30}})
    def test_removes_old_ip_by_default(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_lease_to_netbox

        self._create_old_kea_ip()
        sync_lease_to_netbox(self._LEASE_NEW)
        self.assertFalse(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())
        self.assertTrue(NbIP.objects.filter(address__startswith="10.40.0.20/").exists())

    @override_settings(PLUGINS_CONFIG=_DEPRECATE_PLUGINS_CONFIG)
    def test_deprecates_old_ip_in_deprecate_mode(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_lease_to_netbox

        self._create_old_kea_ip()
        sync_lease_to_netbox(self._LEASE_NEW)
        old = NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").first()
        self.assertIsNotNone(old)
        self.assertEqual(old.status, "deprecated")

    @override_settings(PLUGINS_CONFIG=_NONE_PLUGINS_CONFIG)
    def test_leaves_old_ip_when_mode_is_none(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_lease_to_netbox

        self._create_old_kea_ip()
        sync_lease_to_netbox(self._LEASE_NEW)
        self.assertTrue(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())

    @override_settings(PLUGINS_CONFIG=_STALE_PLUGINS_CONFIG)
    def test_does_not_remove_old_ip_when_no_hostname(self):
        """Lease without hostname: no stale cleanup (unsafe to match)."""
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_lease_to_netbox

        self._create_old_kea_ip()
        sync_lease_to_netbox({"ip-address": "10.40.0.20", "hw-address": "aa:bb:cc:dd:00:01"})
        self.assertTrue(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())


class TestSyncReservationWithStaleCleanup(TestCase):
    """Integration: sync_reservation_to_netbox removes stale IPs when hostname matches."""

    _RESERVATION_NEW = {
        "ip-address": "10.50.0.20",
        "hw-address": "bb:cc:dd:ee:00:01",
        "hostname": "moved-device.example.com",
    }
    _OLD_IP = "10.50.0.10"

    def _create_old_kea_ip(self):
        from ipam.models import IPAddress as NbIP

        return NbIP.objects.create(
            address=f"{self._OLD_IP}/32",
            status="reserved",
            dns_name="moved-device.example.com",
            description="Synced from Kea DHCP reservation",
        )

    @override_settings(PLUGINS_CONFIG=_STALE_PLUGINS_CONFIG)
    def test_removes_old_reserved_ip_for_same_hostname(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_reservation_to_netbox

        self._create_old_kea_ip()
        sync_reservation_to_netbox(self._RESERVATION_NEW)
        self.assertFalse(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())
        self.assertTrue(NbIP.objects.filter(address__startswith="10.50.0.20/").exists())


# ─────────────────────────────────────────────────────────────────────────────
# TestNetboxDnsAvailable
# ─────────────────────────────────────────────────────────────────────────────


class TestNetboxDnsAvailable(TestCase):
    """netbox_dns_available() returns a bool based on importlib.util.find_spec."""

    def test_returns_false_when_spec_is_none(self):
        """netbox_dns_available returns False when netbox_dns is not installed."""
        import importlib.util as _ilu
        from unittest.mock import patch

        with patch.object(_ilu, "find_spec", return_value=None):
            from netbox_kea.sync import netbox_dns_available

            self.assertFalse(netbox_dns_available())

    def test_returns_true_when_spec_is_present(self):
        """netbox_dns_available returns True when find_spec returns a non-None object."""
        import importlib.util as _ilu
        from unittest.mock import MagicMock, patch

        with patch.object(_ilu, "find_spec", return_value=MagicMock()):
            from netbox_kea.sync import netbox_dns_available

            self.assertTrue(netbox_dns_available())


# ─────────────────────────────────────────────────────────────────────────────
# TestCleanupStaleIpsUnknownMode
# ─────────────────────────────────────────────────────────────────────────────


class TestCleanupStaleIpsUnknownMode(TestCase):
    """_cleanup_stale_ips with an unrecognised mode logs and returns 0."""

    _HOSTNAME = "moving-device.example.com"
    _OLD_IP = "10.30.0.11"
    _KEA_DESC = "Synced from Kea DHCP lease"

    def test_unknown_mode_returns_zero_and_does_not_delete(self):
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import _cleanup_stale_ips

        NbIP.objects.create(
            address=f"{self._OLD_IP}/32",
            status="dhcp",
            dns_name=self._HOSTNAME,
            description=self._KEA_DESC,
        )
        count = _cleanup_stale_ips("10.30.0.99", self._HOSTNAME, mode="unknown")
        self.assertEqual(count, 0)
        self.assertTrue(NbIP.objects.filter(address__startswith=f"{self._OLD_IP}/").exists())


# ─────────────────────────────────────────────────────────────────────────────
# TestSyncMacAddressErrors
# ─────────────────────────────────────────────────────────────────────────────


class TestSyncMacAddressErrors(TestCase):
    """_sync_mac_address handles DB and parse errors gracefully."""

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

        from netbox_kea.sync import _sync_mac_address

        with patch.object(MACAddress.objects, "get_or_create", side_effect=ProgrammingError("boom")) as mock_goc:
            _sync_mac_address("aa:bb:cc:dd:ee:ff", hostname="test-host")
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

        from netbox_kea.sync import _sync_mac_address

        # Passing an obviously invalid MAC address exercises the except Exception path.
        _sync_mac_address("not-a-mac", hostname="test-host")
        # No exception should propagate.
