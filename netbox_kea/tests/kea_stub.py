# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Command-aware HTTP stub for de-mocked view tests.

Instead of patching ``netbox_kea.models.KeaClient`` with a ``MagicMock`` (which
never builds or inspects the real request payload), these helpers let the view
use a **real** ``KeaClient`` while stubbing only the HTTP boundary —
``requests.Session.post`` — so the actual JSON sent to Kea is exercised and can
be asserted on. This is what lets a payload regression (e.g. a stray/missing
``service`` key) actually fail a test.

Patched at the **class** level (``requests.Session.post``) so it also covers
``KeaClient.clone()``, which builds a fresh ``requests.Session`` for the worker
threads used by the reservation/lease-enrichment views.
"""

from __future__ import annotations

import ipaddress
import threading
from collections import deque
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import MagicMock, patch

import requests

from netbox_kea.reservations import (
    GlobalReservationScope,
    IdentifierType,
    InSubnetReservationScope,
    IPv4Reservation,
    IPv6Reservation,
    Reservation,
    ReservationIdentity,
    ReservationScope,
)
from netbox_kea.subnet_catalogue import SubnetIdentity


def _http_response(payload: Any, status: int = 200) -> MagicMock:
    """Build a spec'd ``requests.Response`` returning *payload* from ``.json()``."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = payload
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def _is_exc(obj: Any) -> bool:
    """True if *obj* is an exception instance or an exception class."""
    return isinstance(obj, BaseException) or (isinstance(obj, type) and issubclass(obj, BaseException))


class ResponseQueue:
    """An explicit FIFO of sequential responses for one command.

    Each call consumes the next response; once a single response remains it
    repeats (so callers can register ``queued(page, end)`` and let ``end`` answer
    every subsequent call). Kept distinct from a plain ``list`` so an ordinary
    multi-service Kea response — itself a list — is never mistaken for a queue.
    """

    def __init__(self, responses: Any) -> None:
        self._items: deque = deque(responses)
        if not self._items:
            raise ValueError("queued() requires at least one response")

    def next(self) -> Any:
        """Pop the next response (the last one repeats). Caller holds the stub lock."""
        return self._items.popleft() if len(self._items) > 1 else self._items[0]


def queued(*responses: Any) -> ResponseQueue:
    """Register a sequence of responses answered in order for one command.

    ``stub_kea({"lease4-get-page": queued(page1, page2, end)})`` returns ``page1``
    on the first call, ``page2`` on the second, then ``end`` for every call after.
    """
    return ResponseQueue(responses)


class KeaHttpStub:
    """Dispatch Kea commands by name and record the request bodies sent.

    ``responses`` maps a command name to what that command should return. A value
    may be:

    * a ``dict`` payload — the single ``.json()`` entry, used for every call;
    * a ``list`` payload — returned **verbatim** as the ``.json()`` body (Kea
      returns one entry per targeted service, so a real multi-service response is
      a list);
    * a :class:`ResponseQueue` from :func:`queued` — sequential responses, one per
      call (the last repeats), for pagination / partial-failure paths;
    * a callable ``(body) -> payload`` — for argument-dependent responses;
    * an exception instance or class — **raised** when the command is called, or
      returned by a callable, to simulate a transport error (e.g.
      ``requests.ConnectionError``) at the HTTP boundary. This lets error-path
      tests drive the real ``KeaClient`` error handling instead of mocking
      ``command.side_effect``. A KeaException-style failure is instead modelled by
      returning a payload with a non-accepted ``result`` code, which the real
      ``KeaClient.command()`` turns into a ``KeaException``.

    ``KeaClient`` expects a JSON list (one entry per targeted service); a payload
    that is not already a list is wrapped in a single-element list.

    Request recording and queue dispatch are guarded by a lock because the
    reservation/lease-enrichment views ``clone()`` the client and POST from worker
    threads.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = dict(responses)
        self.requests: list[dict[str, Any]] = []
        self._urls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, **kwargs: Any) -> MagicMock:
        body = kwargs.get("json") or {}
        with self._lock:
            self.requests.append(body)
            self._urls.append(url)
            cmd = body.get("command")
            if cmd not in self._responses:
                raise AssertionError(f"KeaHttpStub: no response registered for command {cmd!r} (url={url})")
            spec = self._responses[cmd]
            if isinstance(spec, ResponseQueue):
                spec = spec.next()
        # Callables/exceptions are resolved outside the lock (they may be slow or raise).
        if callable(spec) and not _is_exc(spec):
            spec = spec(body)
        if _is_exc(spec):
            raise spec() if isinstance(spec, type) else spec
        return _http_response(spec if isinstance(spec, list) else [spec])

    # --- assertion helpers ---
    def commands(self) -> list[str]:
        """Ordered list of command names sent."""
        with self._lock:
            commands = [request.get("command") for request in self.requests]
        if not all(isinstance(command, str) for command in commands):
            raise AssertionError(f"Recorded a Kea request without a command: {commands!r}")
        return cast(list[str], commands)

    def bodies(self, command: str) -> list[dict[str, Any]]:
        """Every request body sent for *command* (for asserting args / absence of ``service``)."""
        with self._lock:
            return [r for r in self.requests if r.get("command") == command]

    def urls(self) -> list[str]:
        """Ordered list of endpoint URLs POSTed to (parallel to :meth:`commands`).

        Lets dual-URL tests assert that a per-version client hit the protocol-specific
        endpoint (``dhcp4_url``/``dhcp6_url``) rather than the shared CA URL.
        """
        with self._lock:
            return list(self._urls)


# --- shared Kea response builders (kept next to stub_kea so their shape can't
#     drift across the test modules that register them) ---


def _reservation_family(host: dict[str, Any]) -> int:
    """Return the DHCP family one legacy wire reservation fixture describes.

    Delegated prefixes are DHCPv6 only, so a prefix-only fixture is v6 even when it
    carries no address at all.
    """
    raw_addresses = host.get("ip-addresses")
    if isinstance(raw_addresses, list):
        addresses = [address for address in raw_addresses if isinstance(address, str)]
    else:
        singular = host.get("ip-address")
        addresses = [singular] if isinstance(singular, str) else []
    if "ip-addresses" in host or host.get("prefixes"):
        return 6
    return 6 if any(":" in address for address in addresses if address) else 4


def _typed_reservation(raw: dict[str, Any], *, prefix_length: int | None = None) -> Reservation:
    """Convert one legacy wire reservation fixture into the domain value."""
    family = _reservation_family(raw)
    address_values = raw.get("ip-addresses") or ([raw["ip-address"]] if raw.get("ip-address") else [])
    addresses = tuple(ipaddress.ip_address(address) for address in address_values)
    identity_types: tuple[IdentifierType, ...] = ("hw-address", "duid", "circuit-id", "client-id", "flex-id")
    identity_type = next(
        (key for key in identity_types if raw.get(key)),
        "duid" if family == 6 else "flex-id",
    )
    identity_value = cast(str, raw.get(identity_type) or ("00:01" if family == 6 else "test-reservation"))
    if addresses:
        default_prefix = 64 if family == 6 else 24
        network = ipaddress.ip_network(f"{addresses[0]}/{prefix_length or default_prefix}", strict=False)
    else:
        network = ipaddress.ip_network("2001:db8::/64" if family == 6 else "198.18.0.0/24")
    subnet_id = int(raw.get("subnet-id", 1))
    scope: ReservationScope = (
        GlobalReservationScope()
        if subnet_id == 0
        else InSubnetReservationScope(SubnetIdentity(subnet_id=subnet_id, network=network))
    )
    common = {
        "scope": scope,
        "identity": ReservationIdentity(identity_type, identity_value),
        "addresses": addresses,
        "hostname": raw.get("hostname", ""),
    }
    if family == 4:
        return IPv4Reservation(**common)
    return IPv6Reservation(
        **common,
        delegated_prefixes=tuple(ipaddress.IPv6Network(prefix) for prefix in raw.get("prefixes", [])),
    )


def _reservation_mutation_commands() -> dict[str, Any]:
    """A ``list-commands`` payload that confirms every Reservation mutation command."""
    return {
        "result": 0,
        "arguments": ["reservation-get", "reservation-add", "reservation-update", "reservation-del"],
    }


def _res_page(hosts: Any, *, next_from: int = 0, next_source: int = 0) -> dict[str, Any]:
    """A ``reservation-get-page`` payload: *hosts* plus Kea's pagination cursor.

    ``next_from`` and ``next_source`` both 0 mark the Snapshot source exhausted.
    """
    return {"result": 0, "arguments": {"hosts": list(hosts), "next": {"from": next_from, "source-index": next_source}}}


def _res_get(reservation: dict[str, Any]) -> dict[str, Any]:
    """A ``reservation-get`` payload: the host fields Kea returns directly inside ``arguments``."""
    return {"result": 0, "arguments": dict(reservation)}


def _subnet_get(
    version: int, pools: list[str] | None = None, subnet_id: int = 1, subnet_cidr: str | None = None
) -> dict[str, Any]:
    """A ``subnet{v}-get`` payload for the reservation-add pool-overlap probe and CIDR display.

    *pools* is a list of pool range strings; the probe warns only when the
    reservation IP falls inside one of them. *subnet_cidr*, when given, is the
    ``subnet`` field ``KeaClient.get_subnet_cidr`` reads to display the CIDR on the
    reservation edit views — omit it only for callers that never reach that lookup.
    """
    subnet: dict[str, Any] = {"id": subnet_id, "pools": [{"pool": p} for p in (pools or [])]}
    if subnet_cidr is not None:
        subnet["subnet"] = subnet_cidr
    return {"result": 0, "arguments": {f"subnet{version}": [subnet]}}


def _leases_per_subnet(leases_by_subnet: dict[Any, list[dict[str, Any]]]):
    """A Subnet lease responder that answers only for the Subnet it was asked about.

    Kea scopes ``get-all`` with ``subnets`` and ``get-by-state`` with ``subnet-id``.
    A stub that always returns the same leases cannot show whether the caller keeps per-Subnet state.
    Subnets with no leases get Kea's empty-result code 3.
    """

    def _respond(body: dict[str, Any]) -> dict[str, Any]:
        arguments = body.get("arguments", {})
        requested = arguments.get("subnets") or [arguments.get("subnet-id")]
        leases = [lease for sid in requested for lease in leases_by_subnet.get(sid, [])]
        if not leases:
            return {"result": 3}
        return {"result": 0, "arguments": {"leases": leases}}

    return _respond


def _subnet_list(version: int, subnets: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: ARG001 - version kept for call-site symmetry with _subnet_get
    """A ``subnet{v}-list`` payload, the ``subnet_cmds`` source every subnet lookup reads.

    Used by Subnet catalogue and Reservation form tests. *subnets* is the list of
    subnet dicts (each
    ``{"id": …, "subnet": <cidr>}``) Kea reports.
    """
    return {"result": 0, "arguments": {"subnets": list(subnets)}}


def _subnet_stats(
    version: int,
    subnet_id: int,
    *,
    assigned: int = 1,
    declined: int = 0,
    assigned_pds: int = 0,
) -> dict[str, Any]:
    """A ``stat-lease{v}-get`` payload, the only measurement the lease-query guard reads.

    The client rejects a ``result-set`` that omits a required column, so the column set
    lives here once: DHCPv4 counts addresses, DHCPv6 also counts prefix delegations.
    """
    if version == 4:
        columns = ["subnet-id", "assigned-addresses", "declined-addresses"]
        row = [subnet_id, assigned, declined]
    else:
        columns = ["subnet-id", "assigned-nas", "declined-addresses", "assigned-pds"]
        row = [subnet_id, assigned, declined, assigned_pds]
    return {"result": 0, "arguments": {"result-set": {"columns": columns, "rows": [row]}}}


def _catalogue_responses(
    version: int,
    subnet_id: int,
    cidr: str,
    *,
    config_hash: str = "shared-catalogue",
) -> dict[str, Any]:
    """Every response one Subnet Catalogue read of a single subnet needs.

    Registers ``list-commands`` too, because the Reservation pages probe mutation
    capabilities from the same server. A registered response is only ever returned
    when the code under test actually issues that command, so the extra entry cannot
    change what :meth:`KeaHttpStub.commands` records.
    """
    return _catalogue_responses_for_subnets(version, [{"id": subnet_id, "subnet": cidr}], config_hash=config_hash)


def _catalogue_responses_for_subnets(
    version: int,
    subnets: list[dict[str, Any]],
    *,
    config_hash: str = "shared-catalogue",
) -> dict[str, Any]:
    """The same Catalogue responses for an explicit *subnets* list.

    Callers that already carry their own ``subnet{v}-list`` reach the Catalogue shape
    through this entry point, so it stays defined once.
    """
    subnets = list(subnets)
    return {
        f"subnet{version}-list": _subnet_list(version, subnets),
        "list-commands": _reservation_mutation_commands(),
        "config-get": {
            "result": 0,
            "arguments": {
                f"Dhcp{version}": {f"subnet{version}": subnets, "shared-networks": []},
                "hash": config_hash,
            },
        },
    }


@contextmanager
def stub_kea(responses: dict[str, Any]):
    """Exercise a view against a real ``KeaClient`` with the HTTP boundary stubbed.

    Yields a :class:`KeaHttpStub` so tests can assert on the real request bodies::

        with stub_kea({"lease4-del": {"result": 0, "text": "Success"}}) as kea:
            resp = self.client.post(url, ...)
        assert "lease4-del" in kea.commands()
    """
    stub = KeaHttpStub(responses)

    def _post(self, url, **kwargs):  # noqa: ANN001 - mirrors requests.Session.post(self, url, ...)
        return stub(url, **kwargs)

    with patch("netbox_kea.kea.requests.Session.post", new=_post):
        yield stub
