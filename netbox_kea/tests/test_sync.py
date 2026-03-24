"""Tests for netbox_kea.sync — IPAM synchronization helpers.

All tests that hit the database extend django.test.TestCase so each test
runs in a transaction that is rolled back afterwards.
"""

from __future__ import annotations

from django.test import TestCase

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

    def test_sets_status_active(self):
        from netbox_kea.sync import sync_lease_to_netbox

        ip_obj, _ = sync_lease_to_netbox(self._LEASE)
        self.assertEqual(ip_obj.status, "active")

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
        self.assertEqual(ip_obj.status, "active")

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
        from netbox_kea.sync import sync_lease_to_netbox

        sync_lease_to_netbox(self._LEASE)
        self.assertEqual(MACAddress.objects.count(), 1)

    def test_sync_lease_does_not_create_mac_when_no_hw_address(self):
        """F8: sync_lease_to_netbox skips MACAddress when lease has no hw-address."""
        try:
            from dcim.models import MACAddress
        except (ImportError, AttributeError):
            self.skipTest("MACAddress not available in this NetBox version")
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

    def test_updates_existing_ip_status_to_reserved(self):
        """An existing 'active' IP gets promoted to 'reserved' by a reservation sync."""
        from ipam.models import IPAddress as NbIP

        from netbox_kea.sync import sync_reservation_to_netbox

        NbIP.objects.create(address="192.168.51.200/32", status="active")
        ip_obj, created = sync_reservation_to_netbox(self._RESERVATION)
        self.assertFalse(created)
        self.assertEqual(ip_obj.status, "reserved")

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
