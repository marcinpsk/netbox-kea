# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Typed Lease values and observations, driven by lease replies recorded from a real Kea 3.2.

The recordings come from scripts/record_kea_config_get.py. Reads go through the real
KeaClient with only the HTTP boundary stubbed.
"""

from __future__ import annotations

import copy
import ipaddress
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from netbox_kea import constants, leases
from netbox_kea.constants import Family
from netbox_kea.kea import LEASE_GET, LEASE_GET_ALL, LEASE_GET_PAGE
from netbox_kea.leases import (
    INFINITE_LIFETIME,
    DHCPv4AddressLease,
    DHCPv4Binding,
    DHCPv4LeaseRequest,
    DHCPv6AddressLease,
    DHCPv6LeaseRequest,
    DHCPv6PrefixLease,
    LeaseAbsent,
    LeaseEdit,
    LeaseFound,
    LeaseIdentity,
    LeaseLookupFailed,
    LeaseQuery,
    LeaseSnapshot,
    MalformedLeaseResponse,
    is_current,
    lease_edit,
    lease_edit_conflicts,
    lease_record_data,
    read_exact_lease,
    read_lease_collection,
    read_lease_page,
    shown_lease,
)
from netbox_kea.tests.kea_stub import kea_client, queued, stub_kea

_RECORDINGS = Path(__file__).with_name("kea_recordings")


def _recorded(family: int) -> dict[str, Any]:
    return json.loads((_RECORDINGS / f"dhcp{family}.json").read_text())["leases"]


# Ten minutes after Kea added the recorded leases, so each finite one is still in its lifetime.
_AT = datetime.fromtimestamp(_recorded(4)["lease4-get-all"]["arguments"]["leases"][0]["cltt"] + 600, tz=timezone.utc)


def _raw_leases(family: int) -> list[dict[str, Any]]:
    return copy.deepcopy(_recorded(family)[f"lease{family}-get-all"]["arguments"]["leases"])


def _raw(family: int, address: str) -> dict[str, Any]:
    return next(lease for lease in _raw_leases(family) if lease["ip-address"] == address)


def _reply(*records: Any, result: int = 0) -> dict[str, Any]:
    return {"result": result, "text": "recorded shape", "arguments": {"leases": list(records)}}


def _collection(family: Family, reply: dict[str, Any]) -> leases.LeaseRead:
    with stub_kea({f"lease{family}-get-all": reply}):
        response = kea_client("http://kea.example.com").command(LEASE_GET_ALL[family], family, check=(0, 3))
    return read_lease_collection(response, family=family)


def _one(family: Family, raw: Any) -> leases.LeaseRead:
    return _collection(family, _reply(raw))


def _parsed(family: Family, address: str) -> leases.Lease:
    (record,) = _one(family, _raw(family, address)).records
    return record


def _diagnostics(family: Family, raw: Any) -> list[tuple[str, str]]:
    read = _one(family, raw)
    assert read.records == ()
    return [(diagnostic.code, diagnostic.field) for diagnostic in read.diagnostics]


def _snapshot(
    read: leases.LeaseRead,
    *,
    coverage: leases.LeaseCoverage,
    query: LeaseQuery | None = None,
    family: Family = 4,
) -> LeaseSnapshot:
    return LeaseSnapshot(
        server_id=1,
        family=family,
        query=query or LeaseQuery(family=family, selector=leases.ALL_LEASES),
        read_started=_AT,
        read_finished=_AT + timedelta(seconds=1),
        records=read.records,
        diagnostics=read.diagnostics,
        coverage=coverage,
        next_cursor=read.next_cursor,
    )


# --- recorded real replies ---


def test_recorded_collections_validate_every_state_kind_and_infinite_lifetime():
    v4 = _collection(4, _recorded(4)["lease4-get-all"])
    v6 = _collection(6, _recorded(6)["lease6-get-all"])
    assert v4.diagnostics == () and v6.diagnostics == ()
    assert (len(v4.records), len(v6.records)) == (6, 10)
    assert {lease.state for lease in v4.records} == {"assigned", "declined", "expired-reclaimed", "released"}
    addresses = [lease for lease in v6.records if isinstance(lease, DHCPv6AddressLease)]
    prefixes = [lease for lease in v6.records if isinstance(lease, DHCPv6PrefixLease)]
    assert {lease.state for lease in addresses} == {
        "assigned",
        "declined",
        "expired-reclaimed",
        "released",
        "registered",
    }
    assert {lease.state for lease in prefixes} == {"assigned", "registered", "released"}
    assert {lease.kind for lease in v6.records} == {"address", "delegated-prefix"}
    assert sum(lease.infinite for lease in (*v4.records, *v6.records)) == 3


def test_recorded_values_keep_their_facts():
    assigned = _parsed(4, "192.0.2.10")
    assert isinstance(assigned, DHCPv4AddressLease)
    assert assigned.binding == DHCPv4Binding(hw_address="aa:bb:cc:00:00:10", client_id="01:aa:bb:cc:00:00:10")
    assert (assigned.subnet_id, assigned.pool_id, assigned.hostname) == (10, 1, "host10.example.org")
    assert (assigned.fqdn_forward, assigned.fqdn_reverse, assigned.valid_lifetime) == (True, True, 3600)
    assert assigned.identity == LeaseIdentity(family=4, kind="address", address=ipaddress.IPv4Address("192.0.2.10"))
    # Kea keeps an empty hardware address on a declined lease, and a client ID can stand in for one.
    declined = _parsed(4, "192.0.2.12")
    assert (declined.state, declined.binding.hw_address, declined.pool_id) == ("declined", None, None)
    assert _parsed(4, "192.0.2.15").binding == DHCPv4Binding(hw_address=None, client_id="ff:00:00:00:15:00:02")
    # A declined DHCPv6 lease carries Kea's empty DUID, which is no client identity.
    assert _parsed(6, "2001:db8:1::12").binding.duid is None
    prefix = _parsed(6, "2001:db8:100:100::")
    assert isinstance(prefix, DHCPv6PrefixLease)
    assert prefix.prefix == ipaddress.IPv6Network("2001:db8:100:100::/56")
    assert (prefix.prefix_length, prefix.subnet_id, prefix.binding.iaid) == (56, 10, 6)
    # Kea delegates from a PD pool outside the Subnet's address CIDR.
    assert prefix.prefix.network_address not in ipaddress.IPv6Network("2001:db8:1::/64")
    assert _parsed(6, "2001:db8:1::10").hw_address == "aa:bb:cc:00:00:01"


def test_recorded_pages_continue_from_the_last_raw_address():
    for family in (4, 6):
        pages = _recorded(family)[f"lease{family}-get-page"]
        limit = len(pages[0]["arguments"]["leases"])
        reads = []
        after = None
        with stub_kea({f"lease{family}-get-page": queued(*pages)}):
            client = kea_client("http://kea.example.com")
            for _ in pages:
                response = client.command(LEASE_GET_PAGE[family], family, check=(0, 3))
                reads.append(read_lease_page(response, family=family, limit=limit, after=after))
                after = reads[-1].next_cursor
        assert all(read.diagnostics == () for read in reads)
        assert [read.raw_count for read in reads] == [page["arguments"]["count"] for page in pages]
        assert reads[-1].next_cursor is None
        assert all(read.next_cursor is not None for read in reads[:-1])
        assert sum(len(read.records) for read in reads) == len(_raw_leases(family))


def test_recorded_exact_replies_give_found_or_confirmed_absence():
    cases = [
        (4, "present", LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.10")), LeaseFound),
        (4, "absent", LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.99")), LeaseAbsent),
        (
            6,
            "present",
            LeaseIdentity(family=6, kind="address", address=ipaddress.ip_address("2001:db8:1::10")),
            LeaseFound,
        ),
        (
            6,
            "delegated-prefix",
            LeaseIdentity(family=6, kind="delegated-prefix", address=ipaddress.ip_address("2001:db8:100:100::")),
            LeaseFound,
        ),
    ]
    for family, name, identity, outcome in cases:
        reply = _recorded(family)[f"lease{family}-get"][name]
        with stub_kea({f"lease{family}-get": reply}) as kea:
            client = kea_client("http://kea.example.com")
            response = client.command(
                LEASE_GET[family], family, arguments=leases.lookup_arguments(identity), check=(0, 3)
            )
        result = read_exact_lease(response, identity)
        assert isinstance(result, outcome), (family, name, result)
        if isinstance(result, LeaseFound):
            assert result.lease.identity == identity
    # Kea answers an exact DHCPv6 get without the IA_PD type as not found, so the kind travels with the lookup.
    assert kea.bodies("lease6-get")[0]["arguments"] == {"ip-address": "2001:db8:100:100::", "type": "IA_PD"}
    address = LeaseIdentity(family=6, kind="address", address=ipaddress.ip_address("2001:db8:1::10"))
    assert leases.lookup_arguments(address) == {"ip-address": "2001:db8:1::10"}


def test_recorded_refusals_ground_the_value_rules():
    refused = {**_recorded(4)["refused"], **_recorded(6)["refused"]}
    assert all(reply["result"] == 1 for reply in refused.values())
    assert refused["registered-state"]["text"] == "DHCPv4 leases do not support registered state"
    assert _diagnostics(4, {**_raw(4, "192.0.2.10"), "state": 4}) == [("unsupported-state", "state")]
    assert refused["temporary-address-kind"]["text"].startswith("Incorrect lease type: IA_TA")
    assert _diagnostics(6, {**_raw(6, "2001:db8:1::10"), "type": "IA_TA"}) == [("unsupported-kind", "type")]
    assert refused["declined-delegated-prefix"]["text"] == "Invalid declined state for PD prefix."
    assert _diagnostics(6, {**_raw(6, "2001:db8:100:100::"), "state": 1}) == [("unsupported-state", "state")]
    assert refused["non-canonical-delegated-prefix"]["text"].startswith("Prefix address: 2001:db8:100:501::")
    assert _diagnostics(6, {**_raw(6, "2001:db8:100:100::"), "ip-address": "2001:db8:100:101::"}) == [
        ("invalid-prefix", "ip-address")
    ]
    assert refused["missing-hw-address"]["text"].startswith("missing parameter 'hw-address'")
    with pytest.raises(ValidationError):
        DHCPv4LeaseRequest(address=ipaddress.IPv4Address("192.0.2.20"))


# --- malformed records ---


@pytest.mark.parametrize(
    ("family", "address", "change", "expected"),
    [
        (4, "192.0.2.10", {"valid-lft": True}, ("invalid-type", "valid-lft")),
        (4, "192.0.2.10", {"subnet-id": "10"}, ("invalid-type", "subnet-id")),
        (4, "192.0.2.10", {"cltt": "1791204538"}, ("invalid-type", "cltt")),
        (4, "192.0.2.10", {"state": True}, ("invalid-type", "state")),
        (4, "192.0.2.10", {"state": "0"}, ("invalid-type", "state")),
        (4, "192.0.2.10", {"state": 5}, ("unknown-state", "state")),
        (4, "192.0.2.10", {"fqdn-fwd": 1}, ("invalid-type", "fqdn-fwd")),
        (4, "192.0.2.10", {"hostname": None}, ("invalid-type", "hostname")),
        (4, "192.0.2.10", {"valid-lft": -1}, ("out-of-range", "valid-lft")),
        (4, "192.0.2.10", {"valid-lft": INFINITE_LIFETIME + 1}, ("out-of-range", "valid-lft")),
        (4, "192.0.2.10", {"subnet-id": 0}, ("out-of-range", "subnet-id")),
        # Kea's largest Subnet ID is one less than the uint32 maximum.
        (4, "192.0.2.10", {"subnet-id": 4_294_967_295}, ("out-of-range", "subnet-id")),
        (4, "192.0.2.10", {"pool-id": 0}, ("out-of-range", "pool-id")),
        (4, "192.0.2.10", {"cltt": 0}, ("out-of-range", "cltt")),
        (4, "192.0.2.10", {"cltt": 253402300799, "valid-lft": 1}, ("invalid-lifetime", "valid-lft")),
        (4, "192.0.2.10", {"ip-address": "192.0.2.300"}, ("invalid-address", "ip-address")),
        (4, "192.0.2.10", {"ip-address": "2001:db8::1"}, ("wrong-family", "ip-address")),
        (4, "192.0.2.10", {"hw-address": "zz:00"}, ("invalid-identifier", "hw-address")),
        (4, "192.0.2.10", {"client-id": "01"}, ("invalid-identifier", "client-id")),
        (4, "192.0.2.11", {"hw-address": ""}, ("invalid-identifier", "hw-address")),
        (6, "2001:db8:1::10", {"iaid": True}, ("invalid-type", "iaid")),
        (6, "2001:db8:1::10", {"preferred-lft": "1800"}, ("invalid-type", "preferred-lft")),
        (6, "2001:db8:1::10", {"type": 1}, ("invalid-type", "type")),
        (6, "2001:db8:1::10", {"duid": "00:00:00"}, ("invalid-identifier", "duid")),
        (6, "2001:db8:1::10", {"ip-address": "192.0.2.1"}, ("wrong-family", "ip-address")),
        (6, "2001:db8:100:100::", {"prefix-len": 0}, ("out-of-range", "prefix-len")),
        (6, "2001:db8:100:100::", {"prefix-len": 129}, ("out-of-range", "prefix-len")),
        (6, "2001:db8:100:100::", {"prefix-len": "56"}, ("invalid-type", "prefix-len")),
    ],
)
def test_a_malformed_field_gives_one_stable_diagnostic(family, address, change, expected):
    assert _diagnostics(family, {**_raw(family, address), **change}) == [expected]


@pytest.mark.parametrize(
    ("family", "address", "key"),
    [
        (4, "192.0.2.10", "cltt"),
        (4, "192.0.2.10", "valid-lft"),
        (4, "192.0.2.10", "state"),
        (4, "192.0.2.10", "hw-address"),
        (4, "192.0.2.10", "hostname"),
        (6, "2001:db8:1::10", "duid"),
        (6, "2001:db8:1::10", "iaid"),
        (6, "2001:db8:1::10", "preferred-lft"),
        (6, "2001:db8:1::10", "type"),
        (6, "2001:db8:100:100::", "prefix-len"),
    ],
)
def test_a_missing_mandatory_field_is_a_diagnostic(family, address, key):
    raw = _raw(family, address)
    del raw[key]
    assert _diagnostics(family, raw) == [("missing-field", key)]


def test_optional_kea_fields_may_be_absent():
    v4 = _raw(4, "192.0.2.10")
    for key in ("client-id", "pool-id", "user-context"):
        del v4[key]
    v6 = _raw(6, "2001:db8:1::10")
    for key in ("hw-address", "pool-id", "user-context"):
        del v6[key]
    assert _one(4, v4).diagnostics == () and _one(6, v6).diagnostics == ()


def test_diagnostics_name_both_kinds_when_the_kind_is_unknown():
    (diagnostic,) = _one(6, {**_raw(6, "2001:db8:1::10"), "type": "IA_TA"}).diagnostics
    assert diagnostic.kinds == ("address", "delegated-prefix")
    (diagnostic,) = _one(6, {**_raw(6, "2001:db8:100:100::"), "iaid": -1}).diagnostics
    assert diagnostic.kinds == ("delegated-prefix",)
    (diagnostic,) = _one(4, "not a record").diagnostics
    assert (diagnostic.code, diagnostic.kinds) == ("invalid-record", ("address",))


def test_every_diagnostic_code_has_a_fixed_message():
    assert set(get_args(leases.LeaseDiagnosticCode)) == set(leases._MESSAGES)


def test_diagnostics_hold_no_rejected_value_or_exception_text():
    secret = "do-not-echo-this-value"
    read = _one(4, {**_raw(4, "192.0.2.10"), "hostname": 7, "hw-address": secret, "ip-address": secret})
    assert read.records == ()
    for diagnostic in read.diagnostics:
        assert diagnostic.source_position == "leases[0]"
        assert secret not in repr(diagnostic) and secret not in diagnostic.message
        assert set(diagnostic.model_dump()) == {"code", "field", "source_position", "kinds"}
    assert {diagnostic.code for diagnostic in read.diagnostics} == {
        "invalid-type",
        "invalid-address",
        "invalid-identifier",
    }


def test_a_mixed_collection_keeps_good_siblings_and_cannot_attest_absence():
    good = _raw(4, "192.0.2.10")
    read = _collection(4, _reply(good, {**_raw(4, "192.0.2.11"), "valid-lft": "3600"}, _raw(4, "192.0.2.13")))
    assert [str(lease.identity.address) for lease in read.records] == ["192.0.2.10", "192.0.2.13"]
    assert [(d.code, d.source_position) for d in read.diagnostics] == [("invalid-type", "leases[1]")]
    snapshot = _snapshot(read, coverage="exhaustive")
    assert not snapshot.complete
    absent = LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.11"))
    assert not snapshot.attests_absence(absent)


def test_duplicate_identities_quarantine_every_copy():
    first = _raw(4, "192.0.2.10")
    read = _collection(4, _reply(first, {**first, "hostname": "other"}, _raw(4, "192.0.2.11")))
    assert [str(lease.identity.address) for lease in read.records] == ["192.0.2.11"]
    assert [(d.code, d.source_position) for d in read.diagnostics] == [
        ("duplicate-lease", "leases[0]"),
        ("duplicate-lease", "leases[1]"),
    ]


# --- envelopes, pages and coverage ---


def _page(family: int, *records: Any, count: int | None = None) -> dict[str, Any]:
    return {
        "result": 0,
        "text": "page",
        "arguments": {"leases": list(records), "count": len(records) if count is None else count},
    }


def _read_page(family: Family, reply: Any, *, limit: int, after=None) -> leases.LeaseRead:
    with stub_kea({f"lease{family}-get-page": reply}):
        response = kea_client("http://kea.example.com").command(LEASE_GET_PAGE[family], family, check=(0, 3))
    return read_lease_page(response, family=family, limit=limit, after=after)


def test_a_fully_quarantined_page_still_continues_from_a_valid_raw_cursor():
    broken = [{**_raw(4, address), "valid-lft": True} for address in ("192.0.2.10", "192.0.2.11")]
    read = _read_page(4, _page(4, *broken), limit=2)
    assert read.records == ()
    assert len(read.diagnostics) == 2
    assert (read.raw_count, read.next_cursor) == (2, ipaddress.IPv4Address("192.0.2.11"))


@pytest.mark.parametrize(
    "reply",
    [
        _page(4, {**_raw(4, "192.0.2.10"), "ip-address": "bad"}, _raw(4, "192.0.2.11"), {"ip-address": 7}),
        _page(4, _raw(4, "192.0.2.10"), count=2),
        {"result": 0, "text": "page", "arguments": {"leases": {}, "count": 0}},
        {"result": 0, "text": "page", "arguments": {"leases": [], "count": True}},
        {"result": 0, "text": "page", "arguments": []},
        {"result": 0, "text": "page"},
        [],
    ],
)
def test_an_unusable_page_envelope_or_cursor_fails_the_read(reply):
    with pytest.raises(MalformedLeaseResponse):
        _read_page(4, reply, limit=3)


def test_a_page_that_does_not_advance_fails_the_read():
    page = _page(4, _raw(4, "192.0.2.10"), _raw(4, "192.0.2.11"))
    with pytest.raises(MalformedLeaseResponse):
        _read_page(4, page, limit=2, after=ipaddress.IPv4Address("192.0.2.11"))


def test_an_empty_final_page_ends_the_scope():
    final = _recorded(4)["lease4-get-page"][-1]
    assert final["result"] == 3
    read = _read_page(4, final, limit=3, after=ipaddress.IPv4Address("192.0.2.15"))
    assert (read.records, read.raw_count, read.next_cursor) == ((), 0, None)


def test_a_valid_page_is_distinct_from_exhaustive_coverage():
    first = _recorded(4)["lease4-get-page"][0]
    read = _read_page(4, first, limit=3)
    page = _snapshot(read, coverage="page")
    assert read.diagnostics == () and not page.complete
    assert not page.attests_absence(LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.99")))
    whole = _snapshot(_collection(4, _recorded(4)["lease4-get-all"]), coverage="exhaustive")
    assert whole.complete
    assert whole.attests_absence(LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.99")))
    assert not whole.attests_absence(
        LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.10"))
    )
    with pytest.raises(ValidationError):
        _snapshot(read, coverage="exhaustive")


def test_a_filtered_query_never_attests_absence_for_the_whole_family():
    read = _collection(4, _recorded(4)["lease4-get-all"])
    absent = LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.99"))
    for query in (
        LeaseQuery(family=4, selector=constants.BY_SUBNET_ID, value=10),
        LeaseQuery(family=4, selector=constants.BY_SUBNET_ID, value=10, state="assigned"),
        LeaseQuery(family=4, selector=constants.BY_SUBNET, value="192.0.2.0/24", state="declined"),
        LeaseQuery(family=4, selector=constants.BY_HW_ADDRESS, value="aa:bb:cc:00:00:10"),
    ):
        snapshot = _snapshot(read, coverage="exhaustive", query=query)
        assert snapshot.complete and not snapshot.attests_absence(absent)


def test_a_snapshot_requires_an_aware_read_interval_and_its_own_family():
    read = _collection(4, _recorded(4)["lease4-get-all"])
    with pytest.raises(ValidationError):
        LeaseSnapshot(
            server_id=1,
            family=4,
            query=LeaseQuery(family=4, selector=leases.ALL_LEASES),
            read_started=_AT.replace(tzinfo=None),
            read_finished=_AT,
            records=read.records,
            diagnostics=(),
            coverage="exhaustive",
            next_cursor=None,
        )
    with pytest.raises(ValidationError):
        _snapshot(read, coverage="exhaustive", family=6)
    with pytest.raises(ValidationError):
        LeaseQuery(family=4, selector=constants.BY_SUBNET_ID, value=True)


# --- exact lookups ---


def _exact(family: Family, reply: Any, identity: LeaseIdentity):
    with stub_kea({f"lease{family}-get": reply}):
        response = kea_client("http://kea.example.com").command(
            LEASE_GET[family], family, arguments=leases.lookup_arguments(identity), check=(0, 3)
        )
    return read_exact_lease(response, identity)


def test_a_malformed_exact_result_is_a_failed_observation_not_absence():
    present = copy.deepcopy(_recorded(4)["lease4-get"]["present"])
    present["arguments"]["valid-lft"] = "3600"
    identity = LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.10"))
    result = _exact(4, present, identity)
    assert isinstance(result, LeaseLookupFailed)
    assert [(d.code, d.field, d.source_position) for d in result.diagnostics] == [
        ("invalid-type", "valid-lft", "arguments")
    ]


def test_an_exact_result_for_another_target_is_a_failed_observation():
    present = _recorded(4)["lease4-get"]["present"]
    other = LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.11"))
    assert [d.code for d in _exact(4, present, other).diagnostics] == ["target-mismatch"]
    prefix = _recorded(6)["lease6-get"]["delegated-prefix"]
    as_address = LeaseIdentity(family=6, kind="address", address=ipaddress.ip_address("2001:db8:100:100::"))
    assert [d.code for d in _exact(6, prefix, as_address).diagnostics] == ["target-mismatch"]


@pytest.mark.parametrize("arguments", [None, [], "lease"])
def test_an_exact_reply_without_a_record_object_fails_the_read(arguments):
    identity = LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.10"))
    with pytest.raises(MalformedLeaseResponse):
        _exact(4, {"result": 0, "text": "IPv4 lease found.", "arguments": arguments}, identity)


# --- immutability and private extensions ---


def test_nested_extensions_stay_out_of_the_lease_and_its_projection():
    raw = _raw(4, "192.0.2.10")
    lease = _parsed(4, "192.0.2.10")
    raw["user-context"]["site"]["rack"] = "changed"
    assert lease == _one(4, _raw(4, "192.0.2.10")).records[0]
    assert "rack" not in repr(lease) and "user" not in repr(lease)
    projection = lease_record_data(lease, evaluated_at=_AT)
    assert "rack" not in json.dumps(projection) and "context" not in json.dumps(projection)
    assert not any(isinstance(value, (dict, list)) for value in lease.model_dump().values())
    with pytest.raises(ValidationError):
        lease.hostname = "changed"  # type: ignore[misc]


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def test_the_public_projection_names_kind_coverage_facts_and_expiration():
    prefix = lease_record_data(_parsed(6, "2001:db8:100:200::"), evaluated_at=_AT)
    assert prefix == {
        "family": 6,
        "kind": "delegated-prefix",
        "address": "2001:db8:100:200::",
        "prefix_length": 56,
        "subnet_id": 10,
        "state": "assigned",
        "current": True,
        "binding": {"duid": "00:01:00:01:2c:4f:00:01:aa:bb:cc:00:00:01", "iaid": 7},
        "hostname": "",
        "valid_lifetime": INFINITE_LIFETIME,
        "last_transaction": _iso(_raw(6, "2001:db8:100:200::")["cltt"]),
        "expiration": {"infinite": True, "expires_at": None},
    }
    assigned = lease_record_data(_parsed(4, "192.0.2.10"), evaluated_at=_AT)
    assert assigned["prefix_length"] is None
    assert assigned["binding"] == {"hw_address": "aa:bb:cc:00:00:10", "client_id": "01:aa:bb:cc:00:00:10"}
    expires_at = _iso(_raw(4, "192.0.2.10")["cltt"] + 3600)
    assert assigned["expiration"] == {"infinite": False, "expires_at": expires_at}


# --- observed values and creation requests ---


def test_observed_values_have_no_defaults_for_identity_state_or_lifetime():
    with pytest.raises(ValidationError) as raised:
        DHCPv4AddressLease(address=ipaddress.IPv4Address("192.0.2.10"), hw_address="aa:bb:cc:00:00:10")
    missing = {error["loc"][0] for error in raised.value.errors() if error["type"] == "missing"}
    assert {"subnet_id", "state", "cltt", "valid_lifetime", "hostname", "fqdn_forward", "fqdn_reverse"} <= missing


def test_creation_requests_send_only_the_facts_the_operator_supplied():
    v4 = DHCPv4LeaseRequest(address=ipaddress.IPv4Address("192.0.2.20"), hw_address="AA:BB:CC:00:00:20")
    assert leases._creation_arguments(v4) == {"ip-address": "192.0.2.20", "hw-address": "aa:bb:cc:00:00:20"}
    v6 = DHCPv6LeaseRequest(
        address=ipaddress.IPv6Address("2001:db8:1::20"),
        duid="00:01:00:01:aa:bb",
        iaid=20,
        subnet_id=10,
        valid_lifetime=600,
        hostname="new.example.org",
    )
    assert leases._creation_arguments(v6) == {
        "ip-address": "2001:db8:1::20",
        "duid": "00:01:00:01:aa:bb",
        "iaid": 20,
        "subnet-id": 10,
        "valid-lft": 600,
        "hostname": "new.example.org",
    }
    assert not hasattr(v6, "state") and not hasattr(v6, "cltt")


@pytest.mark.parametrize(
    "values",
    [
        {"duid": "00:01:00:01:aa:bb"},
        {"duid": "00:00:00", "iaid": 1},
        {"duid": "00:01:00:01:aa:bb", "iaid": True},
        {"duid": "00:01:00:01:aa:bb", "iaid": 1, "subnet_id": 0},
    ],
)
def test_a_creation_request_refuses_missing_or_invalid_facts(values):
    with pytest.raises(ValidationError):
        DHCPv6LeaseRequest(address=ipaddress.IPv6Address("2001:db8:1::20"), **values)


# --- current use ---


def _at(lease: leases.Lease, seconds: float) -> datetime:
    return datetime.fromtimestamp(lease.cltt + lease.valid_lifetime + seconds, tz=timezone.utc)


def test_a_finite_lease_expires_only_after_its_last_second():
    lease = _parsed(4, "192.0.2.10")
    assert is_current(lease, _at(lease, 0))
    assert is_current(lease, _at(lease, 0.5))
    assert not is_current(lease, _at(lease, 1))


def test_an_infinite_lifetime_never_expires():
    lease = _parsed(4, "192.0.2.11")
    assert lease.expires_at is None
    assert is_current(lease, datetime(9999, 12, 31, tzinfo=timezone.utc))


def test_registered_applies_to_unexpired_addresses_only():
    registered = _parsed(6, "2001:db8:1::11")
    assert is_current(registered, _AT)
    assert not is_current(registered, _at(registered, 1))
    prefix = _parsed(6, "2001:db8:100:300::")
    assert prefix.state == "registered" and not is_current(prefix, _AT)
    assert is_current(_parsed(6, "2001:db8:100:100::"), _AT)


def test_inactive_states_are_never_current():
    for family, address in ((4, "192.0.2.12"), (4, "192.0.2.13"), (4, "192.0.2.14"), (6, "2001:db8:100:400::")):
        assert not is_current(_parsed(family, address), _AT)


def test_current_use_needs_an_aware_evaluation_time():
    with pytest.raises(ValueError, match="aware"):
        is_current(_parsed(4, "192.0.2.10"), datetime(2026, 10, 5))  # noqa: DTZ001 - the naive value under test


def test_a_snapshot_evaluates_current_use_at_the_end_of_its_read():
    read = _collection(6, _recorded(6)["lease6-get-all"])
    snapshot = _snapshot(read, coverage="exhaustive", family=6)
    assert snapshot.evaluated_at == snapshot.read_finished
    assert sorted(str(lease.identity.address) for lease in snapshot.current_records) == [
        "2001:db8:100:100::",
        "2001:db8:100:200::",
        "2001:db8:1::10",
        "2001:db8:1::11",
        "2001:db8:1::15",
    ]


# --- shown values and explicit edit intent ---


def test_an_unchanged_prefilled_form_writes_nothing():
    shown = shown_lease(_parsed(4, "192.0.2.10"))
    edit = lease_edit(shown, hostname="host10.example.org", client_identifier="AA:BB:CC:00:00:10", valid_lifetime=3600)
    assert edit == LeaseEdit() and edit.written == ()


def test_blank_hostname_clears_and_blank_identifier_or_lifetime_keeps():
    shown = shown_lease(_parsed(6, "2001:db8:1::10"))
    edit = lease_edit(shown, hostname="", client_identifier="", valid_lifetime=None)
    assert edit == LeaseEdit(hostname="") and edit.written == ("hostname",)
    changed = lease_edit(shown, hostname="new", client_identifier="00:01:00:01:aa:bb", valid_lifetime=60)
    assert changed.written == ("hostname", "client_identifier", "valid_lifetime")
    with pytest.raises(ValueError):
        lease_edit(shown, hostname="", client_identifier="00:00:00", valid_lifetime=None)


def test_renewal_alone_is_no_conflict_but_shown_facts_are():
    lease = _parsed(6, "2001:db8:1::10")
    shown = shown_lease(lease)
    edit = LeaseEdit(hostname="new")
    renewed = lease.model_copy(update={"cltt": lease.cltt + 60})
    assert lease_edit_conflicts(shown, renewed, edit) == ()
    cases = {
        "subnet_id": {"subnet_id": 20},
        "binding": {"iaid": 99},
        "hostname": {"hostname": "changed elsewhere"},
    }
    for name, update in cases.items():
        assert lease_edit_conflicts(shown, lease.model_copy(update=update), edit) == (name,)
    assert lease_edit_conflicts(shown, lease.model_copy(update={"hostname": "x"}), LeaseEdit(valid_lifetime=5)) == ()
    prefix = _parsed(6, "2001:db8:100:100::")
    assert lease_edit_conflicts(shown_lease(prefix), lease, edit) == (
        "identity",
        "prefix_length",
        "binding",
        "hostname",
    )


def test_an_edit_changes_only_written_fields_in_the_fresh_body():
    raw = _raw(4, "192.0.2.10")
    fresh = _one(4, raw).records[0]
    body = leases._edited_arguments(raw, fresh, LeaseEdit(hostname=""))
    assert body["user-context"] == raw["user-context"] and body["user-context"] is not raw["user-context"]
    assert (body["hostname"], body["fqdn-fwd"], body["fqdn-rev"]) == ("", False, False)
    assert body["expire"] == raw["cltt"] + raw["valid-lft"]
    assert {key: value for key, value in body.items() if key not in {"hostname", "fqdn-fwd", "fqdn-rev", "expire"}} == {
        key: value for key, value in raw.items() if key not in {"hostname", "fqdn-fwd", "fqdn-rev"}
    }
    lifetime = leases._edited_arguments(raw, fresh, LeaseEdit(valid_lifetime=60, client_identifier="aa:bb:cc:00:00:99"))
    assert (lifetime["valid-lft"], lifetime["hw-address"]) == (60, "aa:bb:cc:00:00:99")
    # A written lifetime counts from the update, so the body sends no expire.
    assert "expire" not in lifetime
    with pytest.raises(ValueError):
        leases._edited_arguments({**raw, "hostname": "other"}, fresh, LeaseEdit())


def test_the_recorded_update_keeps_the_transaction_time_only_with_expire():
    recorded = _recorded(4)["lease4-update"]
    before = recorded["before"]["arguments"]
    fresh = _one(4, before).records[0]
    body = leases._edited_arguments(before, fresh, LeaseEdit())
    assert body["expire"] == before["cltt"] + before["valid-lft"]
    assert recorded["after-update-with-expire"]["arguments"]["cltt"] == before["cltt"]
    assert recorded["after-update-without-expire"]["arguments"]["cltt"] != before["cltt"]
    # A shorter written lifetime must not end before the update: Kea starts it at the update time.
    lifetime = leases._edited_arguments(before, fresh, LeaseEdit(valid_lifetime=60))
    assert "expire" not in lifetime and lifetime["valid-lft"] == 60


# --- review findings ---


def test_a_not_found_reply_that_carries_a_record_fails_the_read():
    record = _recorded(4)["lease4-get"]["present"]["arguments"]
    identity = LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.10"))
    with pytest.raises(MalformedLeaseResponse):
        _exact(4, {"result": 3, "text": "Lease not found.", "arguments": record}, identity)


@pytest.mark.parametrize("result", [0.0, 3.0])
def test_a_reply_result_must_be_an_integer(result):
    identity = LeaseIdentity(family=4, kind="address", address=ipaddress.ip_address("192.0.2.10"))
    reply = {**_recorded(4)["lease4-get"]["present"], "result": result}
    with pytest.raises(MalformedLeaseResponse):
        read_exact_lease([reply], identity)
    with pytest.raises(MalformedLeaseResponse):
        read_lease_collection([{**_recorded(4)["lease4-get-all"], "result": result}], family=4)


@pytest.mark.parametrize(
    ("addresses", "after"),
    [
        (("192.0.2.11", "192.0.2.10"), None),
        (("192.0.2.10", "192.0.2.10"), None),
        (("192.0.2.12", "192.0.2.13"), "192.0.2.12"),
        (("192.0.2.13",), "192.0.2.13"),
    ],
)
def test_a_page_out_of_order_or_not_after_its_cursor_fails_the_read(addresses, after):
    records = [_raw(4, address) for address in addresses]
    cursor = None if after is None else ipaddress.IPv4Address(after)
    with pytest.raises(MalformedLeaseResponse):
        _read_page(4, _page(4, *records), limit=3, after=cursor)


def test_a_short_page_that_repeats_the_previous_page_fails_the_read():
    pages = _recorded(4)["lease4-get-page"]
    repeated = _page(4, pages[0]["arguments"]["leases"][-1])
    with pytest.raises(MalformedLeaseResponse):
        _read_page(4, repeated, limit=3, after=ipaddress.IPv4Address("192.0.2.12"))


def test_query_selectors_and_state_filters_match_lease_search():
    client = kea_client("http://kea.example.com")
    selectors = (
        constants.BY_IP,
        constants.BY_HW_ADDRESS,
        constants.BY_HOSTNAME,
        constants.BY_CLIENT_ID,
        constants.BY_SUBNET,
        constants.BY_SUBNET_ID,
        constants.BY_DUID,
    )
    values = {constants.BY_SUBNET_ID: 10, constants.BY_SUBNET: "192.0.2.0/24"}
    for family in (4, 6):
        for selector in selectors:
            value = values.get(selector, "x")
            try:
                LeaseQuery(family=family, selector=selector, value=value)
            except ValidationError:
                with pytest.raises(ValueError, match="not supported"):
                    client.lease_search(family, selector, value, server_id=1)
            else:
                assert selector in leases._QUERY_SELECTORS[family]
    assert leases._QUERY_SELECTORS[4] - {leases.ALL_LEASES} == {
        constants.BY_IP,
        constants.BY_HW_ADDRESS,
        constants.BY_HOSTNAME,
        constants.BY_CLIENT_ID,
        constants.BY_SUBNET,
        constants.BY_SUBNET_ID,
    }
    for state in ("expired-reclaimed", "released", "registered"):
        with pytest.raises(ValidationError):
            LeaseQuery(family=4, selector=constants.BY_SUBNET_ID, value=10, state=state)
    with pytest.raises(ValidationError):
        LeaseQuery(family=6, selector=constants.BY_HW_ADDRESS, value="aa:bb:cc:00:00:10")
    with pytest.raises(ValidationError):
        LeaseQuery(family=4, selector=constants.BY_IP, value="192.0.2.10", state="assigned")


def test_a_snapshot_query_belongs_to_the_snapshot_family():
    read = _collection(4, _recorded(4)["lease4-get-all"])
    with pytest.raises(ValidationError):
        _snapshot(read, coverage="exhaustive", query=LeaseQuery(family=6, selector=leases.ALL_LEASES))


def test_client_identifiers_accept_the_separator_forms_that_reservations_accept():
    shown = shown_lease(_parsed(4, "192.0.2.10"))
    for form in ("AA-BB-CC-00-00-10", "aabb.cc00.0010", "aabbcc000010"):
        assert (
            lease_edit(shown, hostname="host10.example.org", client_identifier=form, valid_lifetime=None) == LeaseEdit()
        )
    request = DHCPv6LeaseRequest(address=ipaddress.IPv6Address("2001:db8:1::20"), duid="00-01-00-01-AA-BB", iaid=1)
    assert request.duid == "00:01:00:01:aa:bb"
