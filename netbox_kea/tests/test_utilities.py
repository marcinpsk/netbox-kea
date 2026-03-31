# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.utilities — pure helper functions."""

from datetime import datetime
from unittest import TestCase
from unittest.mock import MagicMock, patch

from netbox_kea.utilities import (
    _enrich_lease,
    check_dhcp_enabled,
    format_duration,
    format_leases,
    format_option_data,
    is_hex_string,
    parse_subnet_stats,
)


class TestFormatDuration(TestCase):
    """Tests for format_duration()."""

    def test_none_returns_none(self):
        self.assertIsNone(format_duration(None))

    def test_zero_seconds(self):
        self.assertEqual(format_duration(0), "00:00:00")

    def test_one_second(self):
        self.assertEqual(format_duration(1), "00:00:01")

    def test_one_minute(self):
        self.assertEqual(format_duration(60), "00:01:00")

    def test_one_hour(self):
        self.assertEqual(format_duration(3600), "01:00:00")

    def test_mixed_hms(self):
        # 1h 23m 45s
        self.assertEqual(format_duration(3600 + 23 * 60 + 45), "01:23:45")

    def test_large_hours(self):
        self.assertEqual(format_duration(100 * 3600), "100:00:00")

    def test_59_59_59(self):
        self.assertEqual(format_duration(59 * 3600 + 59 * 60 + 59), "59:59:59")


class TestEnrichLease(TestCase):
    """Tests for _enrich_lease()."""

    def _now(self):
        return datetime(2024, 1, 1, 12, 0, 0)

    def test_missing_cltt_and_valid_lft_adds_state_label(self):
        lease = {"ip_address": "10.0.0.1"}
        result = _enrich_lease(self._now(), lease)
        self.assertEqual(result["ip_address"], "10.0.0.1")
        self.assertEqual(result["state_label"], "Unknown")
        self.assertNotIn("expires_at", result)
        self.assertNotIn("expires_in", result)

    def test_hyphen_keys_replaced_with_underscore(self):
        lease = {"ip-address": "10.0.0.1", "cltt": 0, "valid_lft": 3600}
        result = _enrich_lease(self._now(), lease)
        self.assertIn("ip_address", result)
        self.assertNotIn("ip-address", result)

    def test_expires_at_added(self):
        # cltt=0, valid_lft=3600 → expires at epoch+3600
        lease = {"cltt": 0, "valid_lft": 3600}
        result = _enrich_lease(self._now(), lease)
        self.assertIn("expires_at", result)
        self.assertIsInstance(result["expires_at"], datetime)

    def test_expires_in_added(self):
        lease = {"cltt": 0, "valid_lft": 3600}
        result = _enrich_lease(self._now(), lease)
        self.assertIn("expires_in", result)

    def test_cltt_converted_to_datetime(self):
        lease = {"cltt": 0, "valid_lft": 0}
        result = _enrich_lease(self._now(), lease)
        self.assertIsInstance(result["cltt"], datetime)


class TestFormatLeases(TestCase):
    """Tests for format_leases() — applies enrichment to a list."""

    def test_empty_list(self):
        self.assertEqual(format_leases([]), [])

    def test_single_lease_enriched(self):
        leases = [{"cltt": 0, "valid_lft": 3600}]
        result = format_leases(leases)
        self.assertEqual(len(result), 1)
        self.assertIn("expires_at", result[0])

    def test_multiple_leases_all_enriched(self):
        leases = [{"cltt": 0, "valid_lft": 3600}, {"cltt": 100, "valid_lft": 7200}]
        result = format_leases(leases)
        self.assertEqual(len(result), 2)
        for lease in result:
            self.assertIn("expires_at", lease)


class TestEnrichLeaseIPSortKey(TestCase):
    """F1: _enrich_lease() must inject _ip_sort_key for numeric IP sort in tables."""

    def _now(self):
        return datetime(2024, 1, 1, 12, 0, 0)

    def test_adds_ip_sort_key_for_ipv4(self):
        """IPv4 lease gets _ip_sort_key equal to the integer representation of the address."""
        import ipaddress

        lease = {"ip-address": "10.0.0.101"}
        result = _enrich_lease(self._now(), lease)
        self.assertIn("_ip_sort_key", result)
        self.assertEqual(result["_ip_sort_key"], int(ipaddress.ip_address("10.0.0.101")))

    def test_adds_ip_sort_key_for_ipv6(self):
        """IPv6 lease gets _ip_sort_key equal to the integer representation of the address."""
        import ipaddress

        lease = {"ip-address": "2001:db8::1"}
        result = _enrich_lease(self._now(), lease)
        self.assertIn("_ip_sort_key", result)
        self.assertEqual(result["_ip_sort_key"], int(ipaddress.ip_address("2001:db8::1")))

    def test_ip_sort_key_absent_when_no_ip_address(self):
        """Lease with no ip-address field must not have _ip_sort_key injected."""
        lease = {"cltt": 0, "valid_lft": 3600}
        result = _enrich_lease(self._now(), lease)
        self.assertNotIn("_ip_sort_key", result)


class TestFormatLeasesNumericIPSort(TestCase):
    """F1: output of format_leases() has _ip_sort_key giving correct numeric order."""

    def test_numeric_sort_by_ip(self):
        """IPs that sort lexicographically wrong must sort correctly by _ip_sort_key."""
        leases = [
            {"ip-address": "10.0.0.101"},
            {"ip-address": "10.0.0.90"},
            {"ip-address": "10.0.0.9"},
        ]
        result = format_leases(leases)
        sorted_result = sorted(result, key=lambda r: r["_ip_sort_key"])
        ips = [r["ip_address"] for r in sorted_result]
        self.assertEqual(ips, ["10.0.0.9", "10.0.0.90", "10.0.0.101"])


class TestEnrichLeaseExpiryClass(TestCase):
    """F10: _enrich_lease() must inject expiry_class for visual lease state indicators."""

    def _now(self):
        return datetime(2024, 1, 1, 12, 0, 0)

    def test_expired_lease_has_danger_class(self):
        """Lease whose expiry is in the past gets 'text-danger'."""
        # cltt=0, valid_lft=1 → expires at epoch+1, long before 2024
        lease = {"cltt": 0, "valid_lft": 1}
        result = _enrich_lease(self._now(), lease)
        self.assertEqual(result["expiry_class"], "text-danger")

    def test_soon_expiring_lease_has_warning_class(self):
        """Lease expiring in < 300 seconds gets 'text-warning'."""
        now = self._now()
        now_ts = int(now.timestamp())
        # Set up: expires_in will be 250 seconds (< 300 threshold)
        lease = {"cltt": now_ts - 3600 + 250, "valid_lft": 3600}
        result = _enrich_lease(now, lease)
        self.assertEqual(result["expiry_class"], "text-warning")

    def test_active_lease_has_empty_class(self):
        """Lease with plenty of time remaining gets empty string (no CSS class)."""
        now = self._now()
        now_ts = int(now.timestamp())
        # expires_in will be 1000 seconds (> 300 threshold)
        lease = {"cltt": now_ts - 3600 + 1000, "valid_lft": 3600}
        result = _enrich_lease(now, lease)
        self.assertEqual(result["expiry_class"], "")

    def test_no_expiry_data_has_empty_class(self):
        """Lease without cltt/valid_lft must still have expiry_class = ''."""
        lease = {"ip-address": "10.0.0.1"}
        result = _enrich_lease(self._now(), lease)
        self.assertIn("expiry_class", result)
        self.assertEqual(result["expiry_class"], "")


class TestIsHexString(TestCase):
    """Tests for is_hex_string()."""

    def test_valid_mac_address(self):
        self.assertTrue(is_hex_string("aa:bb:cc:dd:ee:ff", 6, 6))

    def test_valid_mac_with_dashes(self):
        self.assertTrue(is_hex_string("aa-bb-cc-dd-ee-ff", 6, 6))

    def test_valid_without_separators(self):
        self.assertTrue(is_hex_string("aabbccddeeff", 6, 6))

    def test_too_short(self):
        self.assertFalse(is_hex_string("aa:bb", 6, 6))

    def test_too_long(self):
        self.assertFalse(is_hex_string("aa:bb:cc:dd:ee:ff:00", 6, 6))

    def test_invalid_characters(self):
        self.assertFalse(is_hex_string("zz:bb:cc:dd:ee:ff", 6, 6))

    def test_empty_string(self):
        self.assertFalse(is_hex_string("", 1, 128))

    def test_single_byte_within_bounds(self):
        self.assertTrue(is_hex_string("ff", 1, 128))

    def test_duid_min_one_byte(self):
        # DUID min is 1 octet
        self.assertTrue(is_hex_string("ab", 1, 128))

    def test_mixed_case_accepted(self):
        self.assertTrue(is_hex_string("AA:BB:CC:DD:EE:FF", 6, 6))


class TestCheckDhcpEnabled(TestCase):
    """Tests for check_dhcp_enabled() — redirect guard."""

    def _make_server(self, dhcp4=True, dhcp6=True):
        server = MagicMock()
        server.dhcp4 = dhcp4
        server.dhcp6 = dhcp6
        server.get_absolute_url.return_value = "/plugins/kea/servers/1/"
        return server

    def test_version4_enabled_returns_none(self):
        server = self._make_server(dhcp4=True)
        with patch("netbox_kea.utilities.redirect") as mock_redirect:
            result = check_dhcp_enabled(server, 4)
        self.assertIsNone(result)
        mock_redirect.assert_not_called()

    def test_version6_enabled_returns_none(self):
        server = self._make_server(dhcp6=True)
        with patch("netbox_kea.utilities.redirect") as mock_redirect:
            result = check_dhcp_enabled(server, 6)
        self.assertIsNone(result)
        mock_redirect.assert_not_called()

    def test_version4_disabled_returns_redirect(self):
        server = self._make_server(dhcp4=False)
        with patch("netbox_kea.utilities.redirect", return_value="<redirect>") as mock_redirect:
            result = check_dhcp_enabled(server, 4)
        self.assertEqual(result, "<redirect>")
        mock_redirect.assert_called_once_with("/plugins/kea/servers/1/")

    def test_version6_disabled_returns_redirect(self):
        server = self._make_server(dhcp6=False)
        with patch("netbox_kea.utilities.redirect", return_value="<redirect>") as mock_redirect:
            result = check_dhcp_enabled(server, 6)
        self.assertEqual(result, "<redirect>")
        mock_redirect.assert_called_once_with("/plugins/kea/servers/1/")


# ---------------------------------------------------------------------------
# format_option_data
# ---------------------------------------------------------------------------


class TestFormatOptionData(TestCase):
    """Tests for format_option_data() — parses Kea option-data lists."""

    def test_empty_list_returns_empty_dict(self):
        self.assertEqual(format_option_data([]), {})

    def test_gateway_option3(self):
        opts = [{"code": 3, "name": "routers", "data": "10.0.0.1", "csv-format": True}]
        result = format_option_data(opts)
        self.assertEqual(result["gateway"], "10.0.0.1")

    def test_dns_servers_option6(self):
        opts = [{"code": 6, "name": "domain-name-servers", "data": "1.1.1.1, 8.8.8.8"}]
        result = format_option_data(opts)
        self.assertEqual(result["dns_servers"], "1.1.1.1, 8.8.8.8")

    def test_domain_name_option15(self):
        opts = [{"code": 15, "name": "domain-name", "data": "example.com"}]
        result = format_option_data(opts)
        self.assertEqual(result["domain_name"], "example.com")

    def test_ntp_servers_option42(self):
        opts = [{"code": 42, "name": "ntp-servers", "data": "192.168.1.123"}]
        result = format_option_data(opts)
        self.assertEqual(result["ntp_servers"], "192.168.1.123")

    def test_domain_search_option119(self):
        opts = [{"code": 119, "name": "domain-search", "data": "example.com, corp.local"}]
        result = format_option_data(opts)
        self.assertEqual(result["domain_search"], "example.com, corp.local")

    def test_v6_dns_option23(self):
        opts = [{"code": 23, "name": "dns-servers", "data": "2001:db8::1", "space": "dhcp6"}]
        result = format_option_data(opts, version=6)
        self.assertEqual(result["dns_servers"], "2001:db8::1")

    def test_v6_sntp_option31(self):
        opts = [{"code": 31, "name": "sntp-servers", "data": "2001:db8::ntp", "space": "dhcp6"}]
        result = format_option_data(opts, version=6)
        self.assertEqual(result["ntp_servers"], "2001:db8::ntp")

    def test_unknown_code_uses_option_name(self):
        opts = [{"code": 99, "name": "some-custom-option", "data": "foo"}]
        result = format_option_data(opts)
        self.assertIn("some_custom_option", result)
        self.assertEqual(result["some_custom_option"], "foo")

    def test_unknown_code_without_name_uses_code(self):
        opts = [{"code": 99, "data": "foo"}]
        result = format_option_data(opts)
        self.assertIn("option_99", result)

    def test_multiple_options_all_present(self):
        opts = [
            {"code": 3, "name": "routers", "data": "10.0.0.1"},
            {"code": 6, "name": "domain-name-servers", "data": "8.8.8.8"},
            {"code": 15, "name": "domain-name", "data": "example.com"},
        ]
        result = format_option_data(opts)
        self.assertEqual(len(result), 3)
        self.assertIn("gateway", result)
        self.assertIn("dns_servers", result)
        self.assertIn("domain_name", result)

    def test_option_name_dash_to_underscore(self):
        """Names with dashes must be converted to underscores for template access."""
        opts = [{"code": 44, "name": "netbios-name-servers", "data": "192.168.1.1"}]
        result = format_option_data(opts)
        self.assertIn("netbios_name_servers", result)
        self.assertNotIn("netbios-name-servers", result)

    def test_v4_code23_not_dns_servers(self):
        """Code 23 in v4 context (IP-TTL) should not be treated as dns_servers."""
        opts = [{"code": 23, "name": "default-ip-ttl", "data": "64"}]
        result = format_option_data(opts, version=4)
        # Falls back to name-based lookup — not the v6 dns_servers mapping
        self.assertNotIn("dns_servers", result)
        self.assertIn("default_ip_ttl", result)

    def test_v6_code23_is_dns_servers(self):
        """Code 23 in v6 context is the standard DNS server option."""
        opts = [{"code": 23, "data": "2001:db8::1"}]
        result = format_option_data(opts, version=6)
        self.assertIn("dns_servers", result)

    def test_v4_code6_is_dns_servers(self):
        """Code 6 in v4 context is DNS servers (standard DHCPv4)."""
        opts = [{"code": 6, "data": "8.8.8.8"}]
        result = format_option_data(opts, version=4)
        self.assertIn("dns_servers", result)

    def test_default_version_is_v4(self):
        """Calling without version defaults to v4 behaviour."""
        opts = [{"code": 3, "data": "10.0.0.1"}]
        result = format_option_data(opts)
        self.assertIn("gateway", result)


# ─────────────────────────────────────────────────────────────────────────────
# parse_subnet_stats
# ─────────────────────────────────────────────────────────────────────────────

_V4_STAT_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "result-set": {
                "columns": [
                    "subnet-id",
                    "total-addresses",
                    "assigned-addresses",
                    "declined-addresses",
                ],
                "rows": [[1, 100, 25, 0], [2, 50, 50, 0]],
            }
        },
    }
]

_V6_STAT_RESPONSE = [
    {
        "result": 0,
        "arguments": {
            "result-set": {
                "columns": [
                    "subnet-id",
                    "total-nas",
                    "assigned-nas",
                    "declined-nas",
                ],
                "rows": [[10, 256, 0, 0]],
            }
        },
    }
]


class TestParseSubnetStats(TestCase):
    """Tests for parse_subnet_stats() — parses stat-lease4/6-get responses."""

    def test_v4_25_percent_utilization(self):
        stats = parse_subnet_stats(_V4_STAT_RESPONSE, version=4)
        self.assertIn(1, stats)
        self.assertEqual(stats[1]["total"], 100)
        self.assertEqual(stats[1]["assigned"], 25)
        self.assertEqual(stats[1]["utilization"], "25%")

    def test_v4_100_percent_utilization(self):
        stats = parse_subnet_stats(_V4_STAT_RESPONSE, version=4)
        self.assertIn(2, stats)
        self.assertEqual(stats[2]["utilization"], "100%")

    def test_v6_uses_nas_columns(self):
        """DHCPv6 uses 'total-nas'/'assigned-nas' column names."""
        stats = parse_subnet_stats(_V6_STAT_RESPONSE, version=6)
        self.assertIn(10, stats)
        self.assertEqual(stats[10]["total"], 256)
        self.assertEqual(stats[10]["assigned"], 0)
        self.assertEqual(stats[10]["utilization"], "0%")

    def test_zero_total_does_not_divide_by_zero(self):
        response = [
            {
                "result": 0,
                "arguments": {
                    "result-set": {
                        "columns": ["subnet-id", "total-addresses", "assigned-addresses"],
                        "rows": [[99, 0, 0]],
                    }
                },
            }
        ]
        stats = parse_subnet_stats(response, version=4)
        self.assertIn(99, stats)
        self.assertEqual(stats[99]["utilization"], "0%")

    def test_empty_rows_returns_empty_dict(self):
        response = [
            {
                "result": 0,
                "arguments": {
                    "result-set": {
                        "columns": ["subnet-id", "total-addresses", "assigned-addresses"],
                        "rows": [],
                    }
                },
            }
        ]
        stats = parse_subnet_stats(response, version=4)
        self.assertEqual(stats, {})

    def test_missing_result_set_returns_empty_dict(self):
        """If 'result-set' key is absent (e.g. stat_cmds not loaded), return {}."""
        stats = parse_subnet_stats([{"result": 0, "arguments": {}}], version=4)
        self.assertEqual(stats, {})

    def test_empty_response_returns_empty_dict(self):
        stats = parse_subnet_stats([], version=4)
        self.assertEqual(stats, {})

    def test_multiple_subnets_all_present(self):
        stats = parse_subnet_stats(_V4_STAT_RESPONSE, version=4)
        self.assertEqual(len(stats), 2)
        self.assertIn(1, stats)
        self.assertIn(2, stats)

    def test_short_row_is_skipped_gracefully(self):
        """A row with too few columns must be skipped without raising IndexError."""
        response = [
            {
                "result": 0,
                "arguments": {
                    "result-set": {
                        "columns": ["subnet-id", "total-addresses", "assigned-addresses"],
                        "rows": [
                            [1, 100, 50],  # valid row
                            [2],  # malformed — too short
                            [3, 200, 100],  # valid row
                        ],
                    }
                },
            }
        ]
        stats = parse_subnet_stats(response, version=4)
        self.assertIn(1, stats)
        self.assertNotIn(2, stats)
        self.assertIn(3, stats)

    def test_row_with_none_values_handled(self):
        """A row with None in numeric fields must not raise."""
        response = [
            {
                "result": 0,
                "arguments": {
                    "result-set": {
                        "columns": ["subnet-id", "total-addresses", "assigned-addresses"],
                        "rows": [[1, None, None]],
                    }
                },
            }
        ]
        stats = parse_subnet_stats(response, version=4)
        self.assertIn(1, stats)
        self.assertEqual(stats[1]["utilization"], "0%")


# ─────────────────────────────────────────────────────────────────────────────
# kea_error_hint()
# ─────────────────────────────────────────────────────────────────────────────


class TestKeaErrorHint(TestCase):
    """Tests for kea_error_hint() — maps KeaException result codes to user hints."""

    def _make_exc(self, result_code: int, text: str = "some error"):  # type: ignore[return]
        from netbox_kea.kea import KeaException

        return KeaException({"result": result_code, "text": text, "arguments": None}, index=0)

    def test_import_available(self):
        """kea_error_hint can be imported from utilities."""
        from netbox_kea.utilities import kea_error_hint  # noqa: F401

    def test_result_2_mentions_hook(self):
        """result=2 (not supported) returns a hint about hook libraries."""
        from netbox_kea.utilities import kea_error_hint

        hint = kea_error_hint(self._make_exc(2))
        self.assertIn("hook", hint.lower())

    def test_result_3_mentions_not_found(self):
        """result=3 (empty result) returns a not-found hint."""
        from netbox_kea.utilities import kea_error_hint

        hint = kea_error_hint(self._make_exc(3))
        self.assertIn("found", hint.lower())

    def test_result_128_mentions_connectivity(self):
        """result=128 returns a connectivity/daemon hint."""
        from netbox_kea.utilities import kea_error_hint

        hint = kea_error_hint(self._make_exc(128))
        self.assertTrue("connect" in hint.lower() or "reach" in hint.lower() or "daemon" in hint.lower())

    def test_result_1_returns_non_empty_string(self):
        """result=1 (generic error) returns a non-empty string."""
        from netbox_kea.utilities import kea_error_hint

        hint = kea_error_hint(self._make_exc(1))
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)

    def test_unknown_code_includes_code_in_message(self):
        """Unknown result codes are included in the returned hint."""
        from netbox_kea.utilities import kea_error_hint

        hint = kea_error_hint(self._make_exc(42))
        self.assertIn("42", hint)

    def test_returns_string_type(self):
        """kea_error_hint always returns str, never None."""
        from netbox_kea.utilities import kea_error_hint

        for code in (0, 1, 2, 3, 128, 999):
            result = kea_error_hint(self._make_exc(code))
            self.assertIsInstance(result, str)


# ---------------------------------------------------------------------------
# TestParseReservationCsv
# ---------------------------------------------------------------------------


class TestParseReservationCsv(TestCase):
    """parse_reservation_csv() turns a CSV string into a list of dicts for reservation_add."""

    def _parse(self, content: str, version: int = 4) -> list:
        from netbox_kea.utilities import parse_reservation_csv

        return parse_reservation_csv(content, version)

    # v4 happy path

    def test_v4_single_row_all_fields(self):
        """Full v4 row maps to correct dict keys."""
        rows = self._parse(
            "ip-address,hw-address,hostname,subnet-id\n192.168.1.1,aa:bb:cc:dd:ee:ff,host1.example.com,3"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip-address"], "192.168.1.1")
        self.assertEqual(rows[0]["hw-address"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(rows[0]["hostname"], "host1.example.com")
        self.assertEqual(rows[0]["subnet-id"], 3)

    def test_v4_optional_hostname_empty(self):
        """Hostname column may be empty; key should be absent or empty string in output."""
        rows = self._parse("ip-address,hw-address,hostname,subnet-id\n10.0.0.5,11:22:33:44:55:66,,2")
        self.assertEqual(rows[0]["ip-address"], "10.0.0.5")
        # hostname absent or falsy when empty
        self.assertFalse(rows[0].get("hostname"))

    def test_v4_multiple_rows(self):
        """Multiple data rows produce multiple dicts."""
        csv = (
            "ip-address,hw-address,hostname,subnet-id\n"
            "10.0.0.1,aa:bb:cc:00:00:01,host1,1\n"
            "10.0.0.2,aa:bb:cc:00:00:02,host2,1\n"
        )
        rows = self._parse(csv)
        self.assertEqual(len(rows), 2)

    def test_strips_whitespace_and_skips_blank_lines(self):
        """Leading/trailing whitespace trimmed; blank lines skipped."""
        csv = "ip-address,hw-address,hostname,subnet-id\n\n  10.0.0.1 , aa:bb:cc:00:00:01 , host1 , 1 \n\n"
        rows = self._parse(csv)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip-address"], "10.0.0.1")

    def test_skips_comment_lines(self):
        """Lines starting with # are skipped."""
        csv = "ip-address,hw-address,hostname,subnet-id\n# this is a comment\n10.0.0.1,aa:bb:cc:00:00:01,host1,1\n"
        rows = self._parse(csv)
        self.assertEqual(len(rows), 1)

    def test_strips_bom(self):
        """UTF-8 BOM at start of file is ignored."""
        csv = "\ufeffip-address,hw-address,hostname,subnet-id\n10.0.0.1,aa:bb:cc:00:00:01,host1,1\n"
        rows = self._parse(csv)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip-address"], "10.0.0.1")

    # v6 happy path

    def test_v6_single_row(self):
        """v6 row uses ip-addresses (list) and duid."""
        csv = "ip-addresses,duid,hostname,subnet-id\n2001:db8::100,00:01:02:03:04:05,v6host.example.com,10\n"
        rows = self._parse(csv, version=6)
        self.assertEqual(len(rows), 1)
        self.assertIn("2001:db8::100", rows[0]["ip-addresses"])
        self.assertEqual(rows[0]["duid"], "00:01:02:03:04:05")
        self.assertEqual(rows[0]["subnet-id"], 10)

    # error cases

    def test_missing_required_field_raises_value_error(self):
        """Row missing a required field raises ValueError with the row number."""
        from netbox_kea.utilities import parse_reservation_csv

        csv = "ip-address,hw-address,hostname,subnet-id\n,aa:bb:cc:00:00:01,host1,1\n"
        with self.assertRaises(ValueError) as ctx:
            parse_reservation_csv(csv, version=4)
        self.assertIn("2", str(ctx.exception))  # row 2 (1-indexed, header = row 1)


# ---------------------------------------------------------------------------
# TestLeaseStateEnrich
# ---------------------------------------------------------------------------


class TestLeaseStateEnrich(TestCase):
    """Tests that _enrich_lease populates state_label correctly."""

    def _enrich(self, state_value):
        from netbox_kea.utilities import format_leases

        lease = {
            "ip-address": "10.0.0.1",
            "cltt": 1700000000,
            "valid-lft": 3600,
            "state": state_value,
        }
        return format_leases([lease])[0]

    def test_state_0_is_active(self):
        """State 0 maps to 'Active'."""
        lease = self._enrich(0)
        self.assertEqual(lease["state_label"], "Active")

    def test_state_1_is_declined(self):
        """State 1 maps to 'Declined'."""
        lease = self._enrich(1)
        self.assertEqual(lease["state_label"], "Declined")

    def test_state_2_is_expired(self):
        """State 2 maps to 'Expired'."""
        lease = self._enrich(2)
        self.assertEqual(lease["state_label"], "Expired")

    def test_unknown_state_is_unknown(self):
        """Unmapped state code falls back to 'Unknown'."""
        lease = self._enrich(99)
        self.assertEqual(lease["state_label"], "Unknown")

    def test_missing_state_is_unknown(self):
        """Lease with no state field falls back to 'Unknown'."""
        from netbox_kea.utilities import format_leases

        lease = {"ip-address": "10.0.0.2", "cltt": 1700000000, "valid-lft": 3600}
        result = format_leases([lease])[0]
        self.assertEqual(result.get("state_label"), "Unknown")


# ---------------------------------------------------------------------------
# TestParseLeaseCsv
# ---------------------------------------------------------------------------


class TestParseLeaseCsv(TestCase):
    """parse_lease_csv(version, csv_text) → list[dict] ready for lease_add."""

    def _parse(self, content: str, version: int = 4) -> list:
        from netbox_kea.utilities import parse_lease_csv

        return parse_lease_csv(version, content)

    # v4 happy path

    def test_v4_single_row_ip_only(self):
        """Minimal v4 row with only ip-address (all optional fields absent)."""
        rows = self._parse("ip-address\n10.0.0.5")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip-address"], "10.0.0.5")
        self.assertNotIn("hw-address", rows[0])
        self.assertNotIn("subnet-id", rows[0])

    def test_v4_all_fields(self):
        """Full v4 row maps to correct dict keys."""
        rows = self._parse(
            "ip-address,hw-address,subnet-id,valid-lft,hostname\n10.0.0.10,aa:bb:cc:dd:ee:ff,1,3600,host1.example.com"
        )
        self.assertEqual(rows[0]["ip-address"], "10.0.0.10")
        self.assertEqual(rows[0]["hw-address"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(rows[0]["subnet-id"], 1)
        self.assertEqual(rows[0]["valid-lft"], 3600)
        self.assertEqual(rows[0]["hostname"], "host1.example.com")

    def test_v4_optional_fields_empty_absent_from_output(self):
        """Empty optional fields do not appear in output dict."""
        rows = self._parse("ip-address,hw-address,subnet-id,valid-lft,hostname\n10.0.0.1,,,,")
        self.assertNotIn("hw-address", rows[0])
        self.assertNotIn("subnet-id", rows[0])
        self.assertNotIn("valid-lft", rows[0])
        self.assertNotIn("hostname", rows[0])

    def test_v4_missing_ip_address_raises(self):
        """Row without ip-address raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("hw-address\naa:bb:cc:dd:ee:ff")

    def test_v4_multiple_rows(self):
        """Multiple data rows produce multiple dicts."""
        rows = self._parse("ip-address,hw-address\n10.0.0.1,aa:bb:cc:00:00:01\n10.0.0.2,aa:bb:cc:00:00:02\n")
        self.assertEqual(len(rows), 2)

    def test_v4_strips_whitespace_and_skips_blank_lines(self):
        """Whitespace trimmed; blank lines skipped."""
        rows = self._parse("ip-address\n\n  10.0.0.1 \n\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip-address"], "10.0.0.1")

    def test_v4_skips_comment_lines(self):
        """Lines starting with # are skipped."""
        rows = self._parse("ip-address\n# comment\n10.0.0.1\n")
        self.assertEqual(len(rows), 1)

    def test_v4_strips_bom(self):
        """UTF-8 BOM is stripped."""
        rows = self._parse("\ufeffip-address\n10.0.0.1")
        self.assertEqual(rows[0]["ip-address"], "10.0.0.1")

    # v6 happy path

    def test_v6_all_required_fields(self):
        """v6 row requires ip-address, duid, iaid."""
        rows = self._parse(
            "ip-address,duid,iaid,subnet-id,hostname\n2001:db8::1,00:01:02:03,12345,1,v6host.example.com",
            version=6,
        )
        self.assertEqual(rows[0]["ip-address"], "2001:db8::1")
        self.assertEqual(rows[0]["duid"], "00:01:02:03")
        self.assertEqual(rows[0]["iaid"], 12345)
        self.assertEqual(rows[0]["subnet-id"], 1)
        self.assertEqual(rows[0]["hostname"], "v6host.example.com")

    def test_v6_missing_duid_raises(self):
        """v6 row missing duid raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("ip-address,iaid\n2001:db8::1,12345", version=6)

    def test_v6_missing_iaid_raises(self):
        """v6 row missing iaid raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("ip-address,duid\n2001:db8::1,00:01:02:03", version=6)


# ─────────────────────────────────────────────────────────────────────────────
# export_table
# ─────────────────────────────────────────────────────────────────────────────


class TestExportTable(TestCase):
    """Tests for export_table() — returns a CSV HTTP response from a django-tables2 Table."""

    def _call(self, table=None, filename="test.csv", use_selected_columns=False):
        from netbox_kea.utilities import export_table

        if table is None:
            table = MagicMock()
            table.available_columns = []
        return export_table(table, filename, use_selected_columns=use_selected_columns)

    @patch("netbox_kea.utilities.TableExport")
    def test_returns_http_response(self, MockExport):
        """export_table returns the HttpResponse from TableExport.response()."""
        from django.http import HttpResponse

        mock_exp = MagicMock()
        MockExport.return_value = mock_exp
        mock_exp.response.return_value = HttpResponse()

        result = self._call()

        self.assertIsNotNone(result)
        mock_exp.response.assert_called_once_with(filename="test.csv")

    @patch("netbox_kea.utilities.TableExport")
    def test_pk_and_actions_always_excluded(self, MockExport):
        """pk and actions columns are always excluded regardless of use_selected_columns."""
        mock_exp = MagicMock()
        MockExport.return_value = mock_exp
        mock_exp.response.return_value = MagicMock()

        self._call()

        call_kwargs = MockExport.call_args.kwargs
        exclude_columns = call_kwargs.get("exclude_columns", set())
        self.assertIn("pk", exclude_columns)
        self.assertIn("actions", exclude_columns)

    @patch("netbox_kea.utilities.TableExport")
    def test_use_selected_columns_adds_available_columns(self, MockExport):
        """When use_selected_columns=True, all available_columns names are also excluded."""
        mock_exp = MagicMock()
        MockExport.return_value = mock_exp
        mock_exp.response.return_value = MagicMock()

        table = MagicMock()
        table.available_columns = [("ip_address", MagicMock()), ("hostname", MagicMock())]

        self._call(table=table, use_selected_columns=True)

        call_kwargs = MockExport.call_args.kwargs
        exclude_columns = call_kwargs.get("exclude_columns", set())
        self.assertIn("ip_address", exclude_columns)
        self.assertIn("hostname", exclude_columns)

    @patch("netbox_kea.utilities.TableExport")
    def test_use_selected_columns_false_leaves_available_columns_in(self, MockExport):
        """When use_selected_columns=False (default), available_columns are NOT excluded."""
        mock_exp = MagicMock()
        MockExport.return_value = mock_exp
        mock_exp.response.return_value = MagicMock()

        table = MagicMock()
        table.available_columns = [("ip_address", MagicMock())]

        self._call(table=table, use_selected_columns=False)

        call_kwargs = MockExport.call_args.kwargs
        exclude_columns = call_kwargs.get("exclude_columns", set())
        self.assertNotIn("ip_address", exclude_columns)


# ─────────────────────────────────────────────────────────────────────────────
# OptionalViewTab
# ─────────────────────────────────────────────────────────────────────────────


class TestOptionalViewTab(TestCase):
    """Tests for OptionalViewTab — a ViewTab that can be conditionally hidden."""

    def _make_tab(self, is_enabled):
        from netbox_kea.utilities import OptionalViewTab

        return OptionalViewTab("Test Label", is_enabled=is_enabled)

    def test_render_returns_none_when_disabled(self):
        """render() returns None when is_enabled(instance) is False."""
        tab = self._make_tab(is_enabled=lambda _: False)
        instance = MagicMock()
        result = tab.render(instance)
        self.assertIsNone(result)

    def test_render_returns_dict_when_enabled(self):
        """render() returns a non-None dict when is_enabled(instance) is True."""
        tab = self._make_tab(is_enabled=lambda _: True)
        instance = MagicMock()
        result = tab.render(instance)
        self.assertIsNotNone(result)

    def test_is_enabled_receives_instance(self):
        """is_enabled callable is called with the instance passed to render()."""
        received = []
        tab = self._make_tab(is_enabled=lambda inst: received.append(inst) or True)
        sentinel = MagicMock()
        tab.render(sentinel)
        self.assertEqual(received, [sentinel])

    def test_stores_is_enabled_callable(self):
        """is_enabled callable is stored as tab.is_enabled after __init__."""
        fn = lambda _: True  # noqa: E731
        tab = self._make_tab(is_enabled=fn)
        self.assertIs(tab.is_enabled, fn)


# ─────────────────────────────────────────────────────────────────────────────
# F1: _enrich_reservation_sort_key — numeric sort for reservation IP columns
# ─────────────────────────────────────────────────────────────────────────────


class TestEnrichReservationSortKey(TestCase):
    """Tests for _enrich_reservation_sort_key() — injects _ip_sort_key into reservation dict."""

    def _call(self, reservation):
        from netbox_kea.utilities import _enrich_reservation_sort_key

        return _enrich_reservation_sort_key(reservation)

    def test_v4_ip_address_sets_sort_key(self):
        """DHCPv4 reservation with 'ip-address' gets correct integer sort key."""
        r = {"ip-address": "10.0.0.101"}
        result = self._call(r)
        import ipaddress

        self.assertEqual(result["_ip_sort_key"], int(ipaddress.ip_address("10.0.0.101")))

    def test_v4_sort_key_is_integer(self):
        """Sort key is an int so numeric comparison works."""
        r = {"ip-address": "192.168.1.1"}
        result = self._call(r)
        self.assertIsInstance(result["_ip_sort_key"], int)

    def test_missing_ip_address_no_sort_key(self):
        """Reservation without 'ip-address' does not get _ip_sort_key (no crash)."""
        r = {"duid": "00:01:02:03"}
        result = self._call(r)
        self.assertNotIn("_ip_sort_key", result)

    def test_v6_ip_address_sets_sort_key(self):
        """DHCPv6 reservation with 'ip-address' gets correct integer sort key."""
        r = {"ip-address": "2001:db8::1"}
        result = self._call(r)
        import ipaddress

        self.assertEqual(result["_ip_sort_key"], int(ipaddress.ip_address("2001:db8::1")))

    def test_numeric_order_correct(self):
        """10.0.0.90 sort key < 10.0.0.101 sort key (numeric, not lexicographic)."""
        r90 = self._call({"ip-address": "10.0.0.90"})
        r101 = self._call({"ip-address": "10.0.0.101"})
        self.assertLess(r90["_ip_sort_key"], r101["_ip_sort_key"])

    def test_invalid_ip_no_sort_key(self):
        """Invalid IP string in 'ip-address' must not raise and must not add _ip_sort_key."""
        r = {"ip-address": "not-an-ip"}
        result = self._call(r)
        self.assertNotIn("_ip_sort_key", result)


# ─────────────────────────────────────────────────────────────────────────────
# Additional coverage tests — lines missed in earlier batches
# ─────────────────────────────────────────────────────────────────────────────


class TestEnrichLeaseInvalidIp(TestCase):
    """_enrich_lease: invalid ip_address must not add _ip_sort_key."""

    def test_invalid_ip_no_ip_sort_key(self):
        from datetime import datetime

        from netbox_kea.utilities import _enrich_lease

        lease = {"ip-address": "not-an-ip", "state": 0}
        result = _enrich_lease(datetime(2024, 1, 1), lease)
        self.assertNotIn("_ip_sort_key", result)


class TestEnrichLeaseNonIntCltt(TestCase):
    """_enrich_lease: non-integer cltt/valid_lft must warn and return early."""

    def test_non_int_cltt_returns_without_expires(self):
        from datetime import datetime

        from netbox_kea.utilities import _enrich_lease

        lease = {"ip-address": "10.0.0.1", "cltt": "not-an-int", "valid_lft": 3600, "state": 0}
        result = _enrich_lease(datetime(2024, 1, 1), lease)
        self.assertNotIn("expires_at", result)
        self.assertNotIn("expires_in", result)

    def test_non_int_valid_lft_returns_without_expires(self):
        from datetime import datetime

        from netbox_kea.utilities import _enrich_lease

        lease = {"ip-address": "10.0.0.1", "cltt": 1000, "valid_lft": "bad", "state": 0}
        result = _enrich_lease(datetime(2024, 1, 1), lease)
        self.assertNotIn("expires_at", result)
        self.assertNotIn("expires_in", result)


class TestParseSubnetStatsMissingCoverage(TestCase):
    """parse_subnet_stats: error branches for non-zero result, None arguments, bad columns, bad rows."""

    def test_nonzero_result_returns_empty_dict(self):
        """Non-zero result in stat response returns empty dict."""
        from netbox_kea.utilities import parse_subnet_stats

        resp = [{"result": 1, "arguments": {"result-set": {"columns": ["subnet-id"], "rows": []}}}]
        self.assertEqual(parse_subnet_stats(resp, 4), {})

    def test_none_arguments_returns_empty_dict(self):
        """arguments=None returns empty dict."""
        from netbox_kea.utilities import parse_subnet_stats

        resp = [{"result": 0, "arguments": None}]
        self.assertEqual(parse_subnet_stats(resp, 4), {})

    def test_missing_subnet_id_column_returns_empty_dict(self):
        """Columns without 'subnet-id' trigger ValueError and return empty dict."""
        from netbox_kea.utilities import parse_subnet_stats

        resp = [
            {
                "result": 0,
                "arguments": {
                    "result-set": {
                        "columns": ["total-addresses", "assigned-addresses"],
                        "rows": [[100, 25]],
                    }
                },
            }
        ]
        self.assertEqual(parse_subnet_stats(resp, 4), {})

    def test_non_int_subnet_id_row_is_skipped(self):
        """Rows with non-integer subnet-id are skipped."""
        from netbox_kea.utilities import parse_subnet_stats

        resp = [
            {
                "result": 0,
                "arguments": {
                    "result-set": {
                        "columns": ["subnet-id", "total-addresses", "assigned-addresses"],
                        "rows": [["not-an-int", 100, 25]],
                    }
                },
            }
        ]
        stats = parse_subnet_stats(resp, 4)
        self.assertEqual(stats, {})


class TestParseIntRowField(TestCase):
    """_parse_int_row_field raises ValueError on non-integer values."""

    def test_non_int_value_raises(self):
        from netbox_kea.utilities import _parse_int_row_field

        with self.assertRaises(ValueError) as ctx:
            _parse_int_row_field({"field": "not-a-number"}, "field", 5)
        self.assertIn("5", str(ctx.exception))
        self.assertIn("field", str(ctx.exception))

    def test_missing_key_raises(self):
        from netbox_kea.utilities import _parse_int_row_field

        with self.assertRaises(ValueError):
            _parse_int_row_field({}, "missing_field", 3)


class TestParseReservationCsvValidationPaths(TestCase):
    """parse_reservation_csv: validation error paths for invalid IP, MAC, DUID, and version mismatches."""

    def _parse(self, content, version=4):
        from netbox_kea.utilities import parse_reservation_csv

        return parse_reservation_csv(content, version)

    def test_v4_invalid_ip_raises(self):
        """Non-parseable IP address raises ValueError."""
        csv = "ip-address,hw-address,subnet-id\n999.999.999.999,aa:bb:cc:dd:ee:ff,1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=4)
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_v4_ipv6_as_ipv4_raises(self):
        """Valid IPv6 address passed as IPv4 raises ValueError."""
        csv = "ip-address,hw-address,subnet-id\n::1,aa:bb:cc:dd:ee:ff,1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=4)
        self.assertIn("IPv4", str(ctx.exception))

    def test_v4_invalid_mac_raises(self):
        """Invalid MAC address format raises ValueError."""
        csv = "ip-address,hw-address,subnet-id\n10.0.0.1,zz:zz:zz:zz:zz:zz,1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=4)
        self.assertIn("MAC", str(ctx.exception))

    def test_v6_empty_ip_addresses_raises(self):
        """ip-addresses with only semicolons/spaces raises ValueError."""
        csv = "ip-addresses,duid,subnet-id\n  ;  ,00:01:02:03:04:05,1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=6)
        self.assertIn("ip-addresses", str(ctx.exception))

    def test_v6_invalid_ipv6_raises(self):
        """Non-parseable string in ip-addresses raises ValueError."""
        csv = "ip-addresses,duid,subnet-id\nnotanip,00:01:02:03:04:05,1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=6)
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_v6_ipv4_as_ipv6_raises(self):
        """IPv4 address in v6 ip-addresses raises ValueError."""
        csv = "ip-addresses,duid,subnet-id\n10.0.0.1,00:01:02:03:04:05,1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=6)
        self.assertIn("IPv6", str(ctx.exception))

    def test_v6_invalid_duid_raises(self):
        """Invalid DUID string raises ValueError."""
        csv = "ip-addresses,duid,subnet-id\n2001:db8::1,not-a-valid-duid!!!,1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=6)
        self.assertIn("DUID", str(ctx.exception))


class TestParseLeaseCsvValidationPaths(TestCase):
    """parse_lease_csv: validation error paths for invalid IP, MAC, DUID, and version mismatches."""

    def _parse(self, content, version=4):
        from netbox_kea.utilities import parse_lease_csv

        return parse_lease_csv(version, content)

    def test_v4_invalid_ip_raises(self):
        """Non-parseable IP address raises ValueError."""
        csv = "ip-address\nnot-an-ip"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=4)
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_v4_ipv6_as_ipv4_raises(self):
        """IPv6 address in v4 CSV raises ValueError."""
        csv = "ip-address\n2001:db8::1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=4)
        self.assertIn("IPv4", str(ctx.exception))

    def test_v6_invalid_duid_raises(self):
        """Invalid DUID format in v6 CSV raises ValueError."""
        csv = "ip-address,duid,iaid\n2001:db8::1,notvalid!!!,1"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=6)
        self.assertIn("DUID", str(ctx.exception))

    def test_v4_invalid_mac_raises(self):
        """Invalid MAC address in v4 CSV raises ValueError."""
        csv = "ip-address,hw-address\n10.0.0.1,zz:zz:zz:zz:zz:zz"
        with self.assertRaises(ValueError) as ctx:
            self._parse(csv, version=4)
        self.assertIn("MAC", str(ctx.exception))
