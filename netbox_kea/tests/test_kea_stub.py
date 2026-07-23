# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the command-aware Kea HTTP stub itself (``kea_stub``).

The stub is test infrastructure, so these verify its dispatch contract directly:
a plain ``list`` is a verbatim multi-service payload, ``queued()`` sequences
responses, and both request recording and queue dispatch are thread-safe (the
enrichment views POST from cloned clients on worker threads).
"""

import threading

import pytest
import requests

from netbox_kea.tests.kea_stub import KeaHttpStub, queued


def _call(stub, command="x"):
    return stub("https://kea.example.com/api/v1/", json={"command": command}).json()


def test_dict_payload_is_wrapped_in_single_entry_list():
    """A dict value is the single ``.json()`` entry (Kea returns a list)."""
    stub = KeaHttpStub({"x": {"result": 0}})
    assert _call(stub) == [{"result": 0}]


def test_plain_multi_entry_list_returned_verbatim():
    """A real multi-service response is a list and must NOT be treated as a queue."""
    multi = [{"result": 0}, {"result": 3}]
    stub = KeaHttpStub({"x": multi})
    # Every call returns the full list, unchanged — not truncated to one entry.
    assert _call(stub) == multi
    assert _call(stub) == multi


def test_empty_list_payload_returned_not_indexerror():
    """An empty list is a valid empty payload, not an IndexError."""
    stub = KeaHttpStub({"x": []})
    assert _call(stub) == []


def test_queued_dispatches_in_order_then_repeats_last():
    stub = KeaHttpStub({"x": queued({"result": 0}, {"result": 3})})
    assert _call(stub) == [{"result": 0}]
    assert _call(stub) == [{"result": 3}]
    assert _call(stub) == [{"result": 3}]  # last response repeats once exhausted


def test_queued_requires_at_least_one_response():
    with pytest.raises(ValueError):
        queued()


def test_unregistered_command_raises_assertion_error():
    stub = KeaHttpStub({})
    with pytest.raises(AssertionError):
        _call(stub, "not-registered")


def test_callable_resolves_against_request_body():
    stub = KeaHttpStub({"x": lambda body: {"echo": body["command"]}})
    assert _call(stub) == [{"echo": "x"}]


def test_urls_records_endpoints_in_order():
    """``urls()`` records each POST endpoint in call order (for dual-URL routing asserts)."""
    stub = KeaHttpStub({"x": {"result": 0}})
    stub("http://v4:1", json={"command": "x"})
    stub("http://v6:2", json={"command": "x"})
    assert stub.urls() == ["http://v4:1", "http://v6:2"]


def test_shared_response_builders_shape():
    """Lock the shape of the shared reservation builders so callers can't drift."""
    from netbox_kea.tests.kea_stub import _res_get, _res_page, _subnet_get, _subnet_list

    host = {"ip-address": "10.0.0.1", "subnet-id": 1}
    # _res_page: hosts snapshot + pagination cursor (both 0 == source exhausted).
    assert _res_page([host]) == {
        "result": 0,
        "arguments": {"hosts": [host], "next": {"from": 0, "source-index": 0}},
    }
    assert _res_page([], next_from=2, next_source=1)["arguments"]["next"] == {"from": 2, "source-index": 1}
    # _res_get: host fields returned directly under arguments.
    assert _res_get(host) == {"result": 0, "arguments": host}
    # _subnet_get: subnet{v} list carrying id + pools for the pool-overlap probe.
    assert _subnet_get(4, pools=["10.0.0.10-10.0.0.20"], subnet_id=7) == {
        "result": 0,
        "arguments": {"subnet4": [{"id": 7, "pools": [{"pool": "10.0.0.10-10.0.0.20"}]}]},
    }
    assert _subnet_get(6)["arguments"]["subnet6"][0]["pools"] == []
    # _subnet_list: the candidate-subnet list reservation_get_by_ip scans.
    subnets = [{"id": 1, "subnet": "10.0.0.0/24"}]
    assert _subnet_list(4, subnets) == {"result": 0, "arguments": {"subnets": subnets}}
    assert _subnet_list(6, []) == {"result": 0, "arguments": {"subnets": []}}


def test_exception_value_is_raised():
    stub = KeaHttpStub({"x": requests.ConnectionError("down")})
    with pytest.raises(requests.ConnectionError):
        _call(stub)


def test_callable_returning_exception_is_raised():
    stub = KeaHttpStub({"x": lambda body: ValueError("bad json")})
    with pytest.raises(ValueError):
        _call(stub)


def test_concurrent_requests_are_all_recorded():
    """Request recording under the lock loses no entries across worker threads."""
    stub = KeaHttpStub({"x": {"result": 0}})
    threads = [threading.Thread(target=_call, args=(stub,)) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert stub.commands().count("x") == 50


def test_queue_dispatch_is_thread_safe():
    """Each queued response is consumed exactly once across concurrent callers.

    Without the dispatch lock, the non-atomic length-check-then-pop would let two
    threads pop the same entry or skip one, so this would flake.
    """
    n = 20
    stub = KeaHttpStub({"x": queued(*[{"n": i} for i in range(n)])})
    seen: list[int] = []
    seen_lock = threading.Lock()

    def worker():
        result = _call(stub)
        with seen_lock:
            seen.append(result[0]["n"])

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == list(range(n))
