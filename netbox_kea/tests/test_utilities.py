# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2026 Andrew Backeby <andrew@backeby.eu>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for netbox_kea.utilities — pure helper functions."""

import ipaddress
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from django.http import HttpResponse

from netbox_kea.constants import Family
from netbox_kea.leases import DHCPv4LeaseRequest, DHCPv6LeaseRequest
from netbox_kea.models import Server
from netbox_kea.tests.kea_stub import lease_record, typed_lease
from netbox_kea.utilities import (
    check_dhcp_enabled,
    format_duration,
    format_option_data,
    is_hex_string,
    lease_rows,
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


_NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_NOW_TS = int(_NOW.timestamp())


def _row(address: str = "10.0.0.1", **changes) -> dict:
    """Return the presentation row of one typed Lease, evaluated at ``_NOW``."""
    changes.setdefault("cltt", _NOW_TS - 60)
    [row] = lease_rows([typed_lease(lease_record(address, **changes))], evaluated_at=_NOW)
    return row


class TestLeaseRows(TestCase):
    """lease_rows() projects typed Leases into display rows; it parses nothing."""

    def test_empty_list(self):
        self.assertEqual(lease_rows([], evaluated_at=_NOW), [])

    def test_rows_keep_the_typed_lease_and_its_display_values(self):
        lease = typed_lease(lease_record("10.0.0.1", cltt=_NOW_TS - 60, valid_lft=3600, state=1))
        [row] = lease_rows([lease], evaluated_at=_NOW)

        self.assertIs(row["lease"], lease)
        self.assertEqual(
            {key: row[key] for key in ("ip_address", "family", "kind", "prefix_length", "subnet_id", "state_label")},
            {
                "ip_address": "10.0.0.1",
                "family": 4,
                "kind": "address",
                "prefix_length": None,
                "subnet_id": 10,
                "state_label": "Declined",
            },
        )
        self.assertEqual(row["client_id"], "01:aa:bb:cc:00:00:10")

    def test_every_state_has_a_label(self):
        for state, label in enumerate(("Active", "Declined", "Expired", "Released")):
            with self.subTest(state=state):
                self.assertEqual(_row(state=state)["state_label"], label)
        self.assertEqual(_row("2001:db8::1", state=4)["state_label"], "Registered")

    def test_delegated_prefix_rows_show_kind_and_length(self):
        row = _row("2001:db8:100:100::", type="IA_PD", prefix_len=56)
        self.assertEqual((row["family"], row["kind"], row["prefix_length"]), (6, "delegated-prefix", 56))
        self.assertEqual((row["duid"], row["iaid"]), ("00:01:00:01:2c:4f:00:01:aa:bb:cc:00:00:01", 1))

    def test_timestamps_are_utc_aware(self):
        """A naive value is rendered verbatim by the table columns, so both derived times must be aware UTC."""
        row = _row(cltt=1, valid_lft=3600)
        self.assertEqual(row["cltt"], datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(row["expires_at"], datetime(1970, 1, 1, 1, 0, 1, tzinfo=timezone.utc))

    def test_an_infinite_lifetime_has_no_expiration(self):
        row = _row(valid_lft=0xFFFFFFFF)
        self.assertEqual((row["expires_at"], row["expires_in"], row["expiry_class"]), (None, None, ""))

    def test_numeric_sort_by_ip(self):
        """IPs that sort lexicographically wrong must sort correctly by _ip_sort_key."""
        rows = [_row(address) for address in ("10.0.0.101", "10.0.0.90", "10.0.0.9")]
        ips = [row["ip_address"] for row in sorted(rows, key=lambda row: row["_ip_sort_key"])]
        self.assertEqual(ips, ["10.0.0.9", "10.0.0.90", "10.0.0.101"])

    def test_expiry_class_marks_expired_and_soon_expiring_leases(self):
        cases = ((_NOW_TS - 3601, "text-danger"), (_NOW_TS - 3600 + 250, "text-warning"), (_NOW_TS - 3600 + 300, ""))
        for cltt, expected in cases:
            with self.subTest(cltt=cltt):
                self.assertEqual(_row(cltt=cltt, valid_lft=3600)["expiry_class"], expected)


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
        server = MagicMock(spec=Server)
        server.dhcp4 = dhcp4
        server.dhcp6 = dhcp6
        server.get_absolute_url.return_value = "/plugins/kea/servers/1/"
        return server

    def test_version4_enabled_returns_none(self):
        server = self._make_server(dhcp4=True)
        with patch("netbox_kea.utilities.redirect", autospec=True) as mock_redirect:
            result = check_dhcp_enabled(server, 4)
        self.assertIsNone(result)
        mock_redirect.assert_not_called()

    def test_version6_enabled_returns_none(self):
        server = self._make_server(dhcp6=True)
        with patch("netbox_kea.utilities.redirect", autospec=True) as mock_redirect:
            result = check_dhcp_enabled(server, 6)
        self.assertIsNone(result)
        mock_redirect.assert_not_called()

    def test_version4_disabled_returns_redirect(self):
        server = self._make_server(dhcp4=False)
        with patch("netbox_kea.utilities.redirect", return_value="<redirect>", autospec=True) as mock_redirect:
            result = check_dhcp_enabled(server, 4)
        self.assertEqual(result, "<redirect>")
        mock_redirect.assert_called_once_with("/plugins/kea/servers/1/")

    def test_version6_disabled_returns_redirect(self):
        server = self._make_server(dhcp6=False)
        with patch("netbox_kea.utilities.redirect", return_value="<redirect>", autospec=True) as mock_redirect:
            result = check_dhcp_enabled(server, 6)
        self.assertEqual(result, "<redirect>")
        mock_redirect.assert_called_once_with("/plugins/kea/servers/1/")


# ---------------------------------------------------------------------------
# format_option_data
# ---------------------------------------------------------------------------


class TestFormatOptionData(TestCase):
    """Tests for format_option_data() — parses Kea option-data lists."""

    def test_empty_list_returns_empty_dict(self):
        self.assertEqual(format_option_data([], version=4), {})

    def test_gateway_option3(self):
        opts = [{"code": 3, "name": "routers", "data": "10.0.0.1", "csv-format": True}]
        result = format_option_data(opts, version=4)
        self.assertEqual(result["gateway"], "10.0.0.1")

    def test_dns_servers_option6(self):
        opts = [{"code": 6, "name": "domain-name-servers", "data": "1.1.1.1, 8.8.8.8"}]
        result = format_option_data(opts, version=4)
        self.assertEqual(result["dns_servers"], "1.1.1.1, 8.8.8.8")

    def test_domain_name_option15(self):
        opts = [{"code": 15, "name": "domain-name", "data": "example.com"}]
        result = format_option_data(opts, version=4)
        self.assertEqual(result["domain_name"], "example.com")

    def test_ntp_servers_option42(self):
        opts = [{"code": 42, "name": "ntp-servers", "data": "192.168.1.123"}]
        result = format_option_data(opts, version=4)
        self.assertEqual(result["ntp_servers"], "192.168.1.123")

    def test_domain_search_option119(self):
        opts = [{"code": 119, "name": "domain-search", "data": "example.com, corp.local"}]
        result = format_option_data(opts, version=4)
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
        result = format_option_data(opts, version=4)
        self.assertIn("some_custom_option", result)
        self.assertEqual(result["some_custom_option"], "foo")

    def test_unknown_code_without_name_uses_code(self):
        opts = [{"code": 99, "data": "foo"}]
        result = format_option_data(opts, version=4)
        self.assertIn("option_99", result)

    def test_multiple_options_all_present(self):
        opts = [
            {"code": 3, "name": "routers", "data": "10.0.0.1"},
            {"code": 6, "name": "domain-name-servers", "data": "8.8.8.8"},
            {"code": 15, "name": "domain-name", "data": "example.com"},
        ]
        result = format_option_data(opts, version=4)
        self.assertEqual(len(result), 3)
        self.assertIn("gateway", result)
        self.assertIn("dns_servers", result)
        self.assertIn("domain_name", result)

    def test_option_name_dash_to_underscore(self):
        """Names with dashes must be converted to underscores for template access."""
        opts = [{"code": 44, "name": "netbios-name-servers", "data": "192.168.1.1"}]
        result = format_option_data(opts, version=4)
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

    def test_result_1_ipv4_subnet_mismatch_returns_specific_hint(self):
        """host_cmds' IPv4 out-of-subnet error returns a specific, actionable hint."""
        from netbox_kea.utilities import kea_error_hint

        text = "specified reservation '10.0.0.5' is not matching the IPv4 subnet prefix '192.168.1.0/24'"
        hint = kea_error_hint(self._make_exc(1, text=text))
        self.assertEqual(hint, "The reserved IP address is outside the subnet's CIDR range.")

    def test_result_1_ipv6_subnet_mismatch_returns_specific_hint(self):
        """host_cmds' IPv6 out-of-subnet error returns a specific, actionable hint."""
        from netbox_kea.utilities import kea_error_hint

        text = "specified reservation '2001:db8:9::1' is not matching the IPv6 subnet prefix '2001:db8:1::/64'"
        hint = kea_error_hint(self._make_exc(1, text=text))
        self.assertEqual(hint, "The reserved IP address is outside the subnet's CIDR range.")

    def test_result_1_other_errors_still_use_generic_message(self):
        """A result=1 error unrelated to subnet mismatch keeps the generic hint (no Kea text leaked)."""
        from netbox_kea.utilities import kea_error_hint

        hint = kea_error_hint(self._make_exc(1, text="Host database not available, cannot add host."))
        self.assertEqual(hint, "Kea reported an error. Check the server logs for details.")

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
# TestParseLeaseCsv
# ---------------------------------------------------------------------------


class TestParseLeaseCsv(TestCase):
    """parse_lease_csv(version, csv_text) → (file line number, typed creation request) pairs."""

    def _parse(self, content: str, version: Family = 4) -> list:
        from netbox_kea.utilities import parse_lease_csv

        return parse_lease_csv(version, content)

    # v4 happy path

    def test_v4_minimal_row(self):
        """A v4 row needs only ip-address and hw-address; Kea supplies the other facts."""
        (row,) = self._parse("ip-address,hw-address\n10.0.0.5,AA:BB:CC:DD:EE:FF")
        self.assertEqual(
            row, (2, DHCPv4LeaseRequest(address=ipaddress.IPv4Address("10.0.0.5"), hw_address="aa:bb:cc:dd:ee:ff"))
        )

    def test_v4_all_fields(self):
        """A full v4 row maps each column to its request field."""
        ((_number, request),) = self._parse(
            "ip-address,hw-address,subnet-id,valid-lft,hostname\n10.0.0.10,aa:bb:cc:dd:ee:ff,1,3600,host1.example.com"
        )
        self.assertEqual(
            (str(request.address), request.hw_address, request.subnet_id, request.valid_lifetime, request.hostname),
            ("10.0.0.10", "aa:bb:cc:dd:ee:ff", 1, 3600, "host1.example.com"),
        )

    def test_v4_empty_optional_fields_stay_unset(self):
        """Empty optional fields leave the request field unset."""
        ((_number, request),) = self._parse(
            "ip-address,hw-address,subnet-id,valid-lft,hostname\n10.0.0.1,aa:bb:cc:dd:ee:ff,,,"
        )
        self.assertEqual((request.subnet_id, request.valid_lifetime, request.hostname), (None, None, None))

    def test_v4_missing_required_columns_raise(self):
        """A v4 row without ip-address or hw-address raises ValueError with its file line number."""
        for content in ("hw-address\naa:bb:cc:dd:ee:ff", "ip-address\n10.0.0.1"):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "^Line 2: missing required"):
                self._parse(content)

    def test_v4_multiple_rows(self):
        """Multiple data rows produce one request each, with their file line numbers."""
        rows = self._parse("ip-address,hw-address\n10.0.0.1,aa:bb:cc:00:00:01\n10.0.0.2,aa:bb:cc:00:00:02\n")
        self.assertEqual([number for number, _request in rows], [2, 3])

    def test_v4_strips_whitespace_skips_blank_and_comment_lines_and_the_bom(self):
        rows = self._parse("\ufeffip-address,hw-address\n\n# comment\n  10.0.0.1 ,aa:bb:cc:00:00:01\n\n")
        self.assertEqual([str(request.address) for _number, request in rows], ["10.0.0.1"])

    def test_numbers_name_the_physical_line_of_each_row(self):
        """The header, the BOM line, skipped lines and every line of a quoted field count."""
        rows = self._parse(
            "\ufeffip-address,hw-address,note\n"
            '10.0.0.1,aa:bb:cc:00:00:01,"two\nlines"\n'
            "# comment\n"
            "\n"
            "10.0.0.2,aa:bb:cc:00:00:02,one line\n"
        )
        self.assertEqual(
            [(number, str(request.address)) for number, request in rows], [(2, "10.0.0.1"), (6, "10.0.0.2")]
        )

    # v6 happy path

    def test_v6_all_required_fields(self):
        """v6 row requires ip-address, duid, iaid."""
        ((_number, request),) = self._parse(
            "ip-address,duid,iaid,subnet-id,hostname\n2001:db8::1,00:01:02:03,12345,1,v6host.example.com",
            version=6,
        )
        self.assertIsInstance(request, DHCPv6LeaseRequest)
        self.assertEqual(
            (str(request.address), request.duid, request.iaid, request.subnet_id, request.hostname),
            ("2001:db8::1", "00:01:02:03", 12345, 1, "v6host.example.com"),
        )

    def test_v6_missing_duid_or_iaid_raises(self):
        for content in ("ip-address,iaid\n2001:db8::1,12345", "ip-address,duid\n2001:db8::1,00:01:02:03"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self._parse(content, version=6)


# ─────────────────────────────────────────────────────────────────────────────
# export_table
# ─────────────────────────────────────────────────────────────────────────────


class TestExportTable(TestCase):
    """Tests for export_table() — returns a CSV HTTP response from a django-tables2 Table."""

    def _call(self, table=None, filename="test.csv", use_selected_columns=False):
        from netbox_kea.utilities import export_table

        if table is None:
            table = MagicMock()  # mock-ok: table input; TableExport (the real boundary) is patched
            table.available_columns = []
        return export_table(table, filename, use_selected_columns=use_selected_columns)

    @patch("netbox_kea.utilities.TableExport", autospec=True)
    def test_returns_http_response(self, MockExport):
        """export_table returns the HttpResponse from TableExport.response()."""
        from django.http import HttpResponse

        mock_exp = MagicMock()  # mock-ok: TableExport instance (external lib, patched)
        MockExport.return_value = mock_exp
        mock_exp.response.return_value = HttpResponse()

        result = self._call()

        self.assertIsNotNone(result)
        mock_exp.response.assert_called_once_with(filename="test.csv")

    @patch("netbox_kea.utilities.TableExport", autospec=True)
    def test_pk_and_actions_always_excluded(self, MockExport):
        """pk and actions columns are always excluded regardless of use_selected_columns."""
        mock_exp = MagicMock()  # mock-ok: TableExport instance (external lib, patched)
        MockExport.return_value = mock_exp
        mock_exp.response.return_value = HttpResponse()

        self._call()

        call_kwargs = MockExport.call_args.kwargs
        exclude_columns = call_kwargs.get("exclude_columns", set())
        self.assertIn("pk", exclude_columns)
        self.assertIn("actions", exclude_columns)

    @patch("netbox_kea.utilities.TableExport", autospec=True)
    def test_use_selected_columns_adds_available_columns(self, MockExport):
        """When use_selected_columns=True, all available_columns names are also excluded."""
        mock_exp = MagicMock()  # mock-ok: TableExport instance (external lib, patched)
        MockExport.return_value = mock_exp
        mock_exp.response.return_value = HttpResponse()

        table = MagicMock()  # mock-ok: table input; TableExport (the real boundary) is patched
        table.available_columns = [
            ("ip_address", None),
            ("hostname", None),
        ]

        self._call(table=table, use_selected_columns=True)

        call_kwargs = MockExport.call_args.kwargs
        exclude_columns = call_kwargs.get("exclude_columns", set())
        self.assertIn("ip_address", exclude_columns)
        self.assertIn("hostname", exclude_columns)

    @patch("netbox_kea.utilities.TableExport", autospec=True)
    def test_use_selected_columns_false_leaves_available_columns_in(self, MockExport):
        """When use_selected_columns=False (default), available_columns are NOT excluded."""
        mock_exp = MagicMock()  # mock-ok: TableExport instance (external lib, patched)
        MockExport.return_value = mock_exp
        mock_exp.response.return_value = HttpResponse()

        table = MagicMock()  # mock-ok: table input; TableExport (the real boundary) is patched
        table.available_columns = [("ip_address", None)]

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
        instance = object()
        result = tab.render(instance)
        self.assertIsNone(result)

    def test_render_returns_dict_when_enabled(self):
        """render() returns a non-None dict when is_enabled(instance) is True."""
        tab = self._make_tab(is_enabled=lambda _: True)
        instance = object()
        result = tab.render(instance)
        self.assertIsNotNone(result)

    def test_is_enabled_receives_instance(self):
        """is_enabled callable is called with the instance passed to render()."""
        received = []
        tab = self._make_tab(is_enabled=lambda inst: received.append(inst) or True)
        sentinel = object()
        tab.render(sentinel)
        self.assertEqual(received, [sentinel])

    def test_stores_is_enabled_callable(self):
        """is_enabled callable is stored as tab.is_enabled after __init__."""
        fn = lambda _: True  # noqa: E731
        tab = self._make_tab(is_enabled=fn)
        self.assertIs(tab.is_enabled, fn)


# ─────────────────────────────────────────────────────────────────────────────
# Additional coverage tests — lines missed in earlier batches
# ─────────────────────────────────────────────────────────────────────────────


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


class TestParseLeaseCsvValidationPaths(TestCase):
    """parse_lease_csv names the file line and the column of an invalid value, never the value."""

    def _refused(self, content, version=4) -> str:
        from netbox_kea.utilities import parse_lease_csv

        with self.assertRaises(ValueError) as ctx:
            parse_lease_csv(version, content)
        return str(ctx.exception)

    def test_each_invalid_value_names_its_row_and_column_only(self):
        cases = (
            ("ip-address,hw-address\nnot-an-ip,aa:bb:cc:00:00:01", 4, "Line 2: 'ip-address' is not an IPv4 address."),
            (
                "ip-address,hw-address\n2001:db8::1,aa:bb:cc:00:00:01",
                4,
                "Line 2: 'ip-address' is not valid for a DHCPv4 lease.",
            ),
            (
                "ip-address,hw-address\n10.0.0.1,zz:zz:zz:zz:zz:zz",
                4,
                "Line 2: 'hw-address' is not valid for a DHCPv4 lease.",
            ),
            ("ip-address,duid,iaid\n2001:db8::1,notvalid!!!,1", 6, "Line 2: 'duid' is not valid for a DHCPv6 lease."),
            ("ip-address,duid,iaid\n2001:db8::1,00:01,-1", 6, "Line 2: 'iaid' must be an integer."),
            ("ip-address,duid,iaid\n2001:db8::1,00:00:00,1", 6, "Line 2: 'duid' is not valid for a DHCPv6 lease."),
        )
        for content, version, message in cases:
            with self.subTest(content=content):
                self.assertEqual(self._refused(content, version), message)

    def test_an_invalid_value_after_skipped_lines_names_its_file_line(self):
        content = "ip-address,hw-address\n# comment\n\n10.0.0.1,zz:zz:zz:zz:zz:zz\n"
        self.assertEqual(self._refused(content), "Line 4: 'hw-address' is not valid for a DHCPv4 lease.")


class TestKeaOptionDatalist(TestCase):
    """kea_option_datalist template tag: DHCP-version handling."""

    def test_invalid_version_falls_back_to_4(self):
        """A non-int dhcp_version degrades to v4 rather than raising."""
        from netbox_kea.templatetags.kea_options import kea_option_datalist

        ctx = kea_option_datalist("not-an-int")
        self.assertEqual(ctx["dhcp_version"], 4)
        self.assertIn("options", ctx)

    def test_valid_version_passed_through(self):
        from netbox_kea.templatetags.kea_options import kea_option_datalist

        self.assertEqual(kea_option_datalist(6)["dhcp_version"], 6)
