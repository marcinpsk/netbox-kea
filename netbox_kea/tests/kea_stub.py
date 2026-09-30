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
import json
import socket
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import requests

from netbox_kea import branching
from netbox_kea.constants import Family
from netbox_kea.kea import KeaClient
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
from netbox_kea.tests.kea_wire_discipline import WIRE_COMMANDS


def kea_client(url: str, **options: Any) -> KeaClient:
    """Build a KeaClient with the write guard that ``Server.get_client()`` passes: the branch binding."""
    return KeaClient(url, write_guard=branching.bind(), **options)


def _http_response(payload: Any, status: int = 200, url: str = "") -> requests.Response:
    """Build a concrete ``requests.Response`` with a JSON body."""
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode(response.encoding)
    return response


def _is_exc(obj: Any) -> bool:
    """True if *obj* is an exception instance or an exception class."""
    return isinstance(obj, BaseException) or (isinstance(obj, type) and issubclass(obj, BaseException))


def _decoded(response: requests.Response) -> Any:
    """Return the JSON body of a successful *response*, or None for an error or a body that is not JSON."""
    if not response.ok:
        return None
    try:
        return response.json()
    except ValueError:
        return None


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


_RECORDINGS = Path(__file__).with_name("kea_recordings")


@cache
def _accepted_keys(family: int) -> dict[str, frozenset[str]]:
    """Return the keys Kea's config-test and config-set accept on a Shared Network and on a Subnet."""
    accepted = json.loads((_RECORDINGS / "accepted-keys.json").read_text())[f"dhcp{family}"]
    return {kind: frozenset(keys) for kind, keys in accepted.items()}


def _assert_kea_would_accept(body: dict[str, Any]) -> None:
    """Fail on a Shared Network or Subnet key that Kea's keyword tables do not accept.

    Kea rejects unknown keys, so a config-test or config-set that sends one fails
    against a real Kea even when a hand-written stub answers it.
    """
    arguments = body.get("arguments")
    for family in (4, 6):
        daemon = arguments.get(f"Dhcp{family}") if isinstance(arguments, dict) else None
        if not isinstance(daemon, dict):
            continue
        known = _accepted_keys(family)
        networks = [n for n in daemon.get("shared-networks") or [] if isinstance(n, dict)]
        subnets = [
            *(daemon.get(f"subnet{family}") or []),
            *(s for n in networks for s in n.get(f"subnet{family}") or []),
        ]
        for kind, entries in (("shared-networks", networks), (f"subnet{family}", subnets)):
            unknown = {key for entry in entries if isinstance(entry, dict) for key in entry} - known[kind]
            if unknown:
                raise AssertionError(
                    f"KeaHttpStub: {body.get('command')} sends {sorted(unknown)} in {kind}, which the "
                    f"DHCPv{family} Kea keyword tables in kea_recordings/accepted-keys.json do not accept."
                )


def _assert_kea_has(commands: Any, verb: str) -> None:
    """Fail on a command name that the harness Kea's list-commands reply does not contain."""
    unknown = sorted(str(command) for command in set(commands) - WIRE_COMMANDS)
    if unknown:
        raise AssertionError(
            f"KeaHttpStub: {verb} {unknown}, which the harness Kea does not have. Use a command from the "
            "list-commands reply in kea_recordings/dhcp4.json or dhcp6.json."
        )


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
        _assert_kea_has(responses, "registers")
        self._responses = dict(responses)
        self.requests: list[dict[str, Any]] = []
        self._urls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, **kwargs: Any) -> requests.Response:
        body = kwargs.get("json") or {}
        with self._lock:
            self.requests.append(body)
            self._urls.append(url)
            cmd = body.get("command")
            _assert_kea_has([cmd], "receives")
            if cmd in ("config-test", "config-set"):
                _assert_kea_would_accept(body)
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
        if cmd == "list-commands":
            payload = _decoded(spec) if isinstance(spec, requests.Response) else spec
            for entry in payload if isinstance(payload, list) else [payload]:
                if isinstance(entry, dict) and isinstance(entry.get("arguments"), list):
                    _assert_kea_has(entry["arguments"], "advertises")
        if isinstance(spec, requests.Response):
            return spec
        return _http_response(spec if isinstance(spec, list) else [spec], url=url)

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
    if "ip-addresses" in host or host.get("prefixes"):
        return 6
    singular = host.get("ip-address")
    return 6 if isinstance(singular, str) and ":" in singular else 4


def _typed_reservation(raw: dict[str, Any], *, prefix_length: int | None = None) -> Reservation:
    """Convert one legacy wire reservation fixture into the domain value."""
    family = _reservation_family(raw)
    address_values = raw.get("ip-addresses") or ([raw["ip-address"]] if raw.get("ip-address") else [])
    addresses = tuple(ipaddress.ip_address(address) for address in address_values)
    identity_types: tuple[IdentifierType, ...] = ("hw-address", "duid", "circuit-id", "client-id", "flex-id")
    identity_type = next((key for key in identity_types if raw.get(key)), None)
    if identity_type is None:
        raise AssertionError(f"Reservation fixture carries no supported identifier: {raw!r}")
    identity_value = cast(str, raw[identity_type])
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


def _network_present(version: int, name: str) -> dict[str, Any]:
    """A ``network{v}-get`` payload for a Shared Network that exists, in the shape Kea 3.2.0 returns."""
    return {
        "result": 0,
        "text": f"Info about IPv{version} shared network '{name}' returned",
        "arguments": {"shared-networks": [{"name": name, f"subnet{version}": []}]},
    }


def _network_absent(name: str) -> dict[str, Any]:
    """A ``network{v}-get`` payload for a Shared Network that does not exist, in the shape Kea 3.2.0 returns."""
    return {"result": 3, "text": f"No '{name}' shared network found"}


def _refused_connection() -> requests.ConnectionError:
    """Return the error that requests raises for a real refused connection.

    Call it outside ``stub_kea``, because the stub replaces ``Session.post``.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    session = requests.Session()
    session.trust_env = False
    try:
        session.post(f"http://127.0.0.1:{port}/", json={}, timeout=5)
    except requests.ConnectionError as exc:
        return exc
    finally:
        session.close()
    raise AssertionError(f"A connection to the closed port {port} did not fail.")


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


def _subnet_list(version: int, subnets: list[dict[str, Any]]) -> dict[str, Any]:
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
    global_options: tuple[dict[str, Any], ...] = (),
    option_definitions: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Every response one Subnet Catalogue read of a single subnet needs.

    Registers ``list-commands`` too, because the Reservation pages probe mutation
    capabilities from the same server. A registered response is only ever returned
    when the code under test actually issues that command, so the extra entry cannot
    change what :meth:`KeaHttpStub.commands` records.
    """
    return _catalogue_responses_for_subnets(
        version,
        [{"id": subnet_id, "subnet": cidr}],
        config_hash=config_hash,
        global_options=global_options,
        option_definitions=option_definitions,
    )


def _catalogue_responses_for_subnets(
    version: int,
    subnets: list[dict[str, Any]],
    *,
    config_hash: str = "shared-catalogue",
    shared_networks: Sequence[Any] = (),
    members: Mapping[int, str] | None = None,
    global_options: tuple[dict[str, Any], ...] = (),
    option_definitions: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """The same Catalogue responses for an explicit *subnets* list.

    Callers that already carry their own ``subnet{v}-list`` reach the Catalogue shape
    through this entry point, so it stays defined once. *members* maps a Subnet ID to
    the name of its Shared Network: the list then names the network of each Subnet, and
    ``config-get`` nests each member Subnet in its network.
    """
    subnets = list(subnets)
    listed = subnets
    networks = list(shared_networks)
    if members is not None:
        key = f"subnet{version}"
        names = {network["name"] for network in networks}
        orphans = {sid: name for sid, name in members.items() if name not in names}
        if orphans:
            raise AssertionError(f"Subnets {orphans} name a Shared Network that the stub does not hold")
        listed = [{**subnet, "shared-network-name": members.get(subnet["id"])} for subnet in subnets]
        networks = [
            {**network, key: [s for s in subnets if members.get(s["id"]) == network["name"]]} for network in networks
        ]
        subnets = [subnet for subnet in subnets if subnet["id"] not in members]
    configuration: dict[str, Any] = {f"subnet{version}": subnets, "shared-networks": networks}
    if global_options:
        configuration["option-data"] = list(global_options)
    if option_definitions:
        configuration["option-def"] = list(option_definitions)
    return {
        f"subnet{version}-list": _subnet_list(version, listed),
        "list-commands": _reservation_mutation_commands(),
        "config-get": {
            "result": 0,
            "arguments": {
                f"Dhcp{version}": configuration,
                "hash": config_hash,
            },
        },
    }


class Run(Enum):
    """The ``SubnetDaemon`` script entry that runs the command."""

    RUN = "run"


RUN = Run.RUN


@dataclass(frozen=True)
class Applied:
    """A scripted answer of ``SubnetDaemon``: run the command, then answer with *answer* (a payload or an exception)."""

    answer: Any


class SubnetDaemon:
    """One Kea daemon that holds Subnets and Shared Networks and answers the Subnet commands like Kea 3.2.0.

    ``script`` sets how the next calls of a command end, one entry per call. ``RUN`` runs the command. A payload or
    an exception answers without running it. ``Applied(answer)`` runs it and then answers with *answer*. After the
    script, each call runs. ``before`` queues a change of another writer that runs just before the next call.
    """

    def __init__(
        self,
        family: Family,
        subnets: Sequence[dict[str, Any]] = (),
        networks: Sequence[str] = (),
        members: Mapping[int, str] | None = None,
    ) -> None:
        self.family = family
        self.subnets = [dict(subnet) for subnet in subnets]
        self.networks = list(networks)
        # Subnet ID to the name of its Shared Network.
        self.members: dict[int, str] = dict(members or {})
        self._scripts: dict[str, deque] = {}
        self._writers: dict[str, deque[Callable[[SubnetDaemon], None]]] = {}
        self._changes = 0

    def script(self, command: str, *answers: Any) -> None:
        """Queue how the next calls of *command* end."""
        self._scripts.setdefault(command, deque()).extend(answers)

    def before(self, command: str, change: Callable[[SubnetDaemon], None]) -> None:
        """Run *change* (a callable that takes this daemon) just before the next call of *command*."""
        self._writers.setdefault(command, deque()).append(change)

    def add(self, subnet: dict[str, Any], network: str | None = None) -> None:
        """Add *subnet*, as another writer would."""
        self.subnets.append(dict(subnet))
        if network is not None:
            self.members[subnet["id"]] = network
        self._changes += 1

    def remove(self, subnet_id: int) -> None:
        """Delete the Subnet with *subnet_id*, as another writer would."""
        self.subnets = [subnet for subnet in self.subnets if subnet["id"] != subnet_id]
        self.members.pop(subnet_id, None)
        self._changes += 1

    def ids(self) -> list[int]:
        """Return the ID of each Subnet in order."""
        return [subnet["id"] for subnet in self.subnets]

    def subnet(self, subnet_id: int) -> dict[str, Any] | None:
        """Return the Subnet with *subnet_id*, or None."""
        return next((subnet for subnet in self.subnets if subnet["id"] == subnet_id), None)

    def responses(self) -> dict[str, Any]:
        """Return the ``stub_kea`` responses of this daemon."""
        v = self.family
        handlers = {
            f"subnet{v}-list": self._list,
            "config-get": self._config_get,
            f"network{v}-get": self._network_get,
            f"subnet{v}-get": self._subnet_get,
            f"subnet{v}-add": self._subnet_add,
            f"subnet{v}-update": self._subnet_update,
            f"network{v}-subnet-add": self._network_subnet_add,
            f"network{v}-subnet-del": self._network_subnet_del,
            f"subnet{v}-del": self._subnet_del,
            "config-test": lambda _body: {"result": 0, "text": "Configuration seems sane."},
            "config-write": lambda _body: {"result": 0, "text": "Configuration written."},
        }
        return {command: self._answering(command, handler) for command, handler in handlers.items()}

    def _answering(
        self, command: str, run: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> Callable[[dict[str, Any]], Any]:
        def answer(body: dict[str, Any]) -> Any:
            writers = self._writers.get(command)
            if writers:
                writers.popleft()(self)
            scripts = self._scripts.get(command)
            scripted = scripts.popleft() if scripts else RUN
            if isinstance(scripted, Applied):
                run(body)
                return scripted.answer
            if scripted is Run.RUN:
                return run(body)
            return scripted

        return answer

    def _catalogue(self) -> dict[str, Any]:
        return _catalogue_responses_for_subnets(
            self.family,
            self.subnets,
            config_hash=f"change-{self._changes}",
            shared_networks=[{"name": name} for name in self.networks],
            members=self.members,
        )

    def _list(self, _body: dict[str, Any]) -> dict[str, Any]:
        return self._catalogue()[f"subnet{self.family}-list"]

    def _config_get(self, _body: dict[str, Any]) -> dict[str, Any]:
        return self._catalogue()["config-get"]

    def _network_get(self, body: dict[str, Any]) -> dict[str, Any]:
        name = body["arguments"]["name"]
        return _network_present(self.family, name) if name in self.networks else _network_absent(name)

    def _subnet_add(self, body: dict[str, Any]) -> dict[str, Any]:
        (subnet,) = body["arguments"][f"subnet{self.family}"]
        if subnet["id"] in self.ids():
            return {"result": 1, "text": f"ID of the new IPv{self.family} subnet '{subnet['id']}' is already in use"}
        if any(existing["subnet"] == subnet["subnet"] for existing in self.subnets):
            return {"result": 1, "text": f"subnet with the prefix of '{subnet['subnet']}' already exists"}
        self.add(subnet)
        return {"result": 0, "text": f"IPv{self.family} subnet added", "arguments": {"subnets": [dict(subnet)]}}

    def _subnet_get(self, body: dict[str, Any]) -> dict[str, Any]:
        subnet_id = body["arguments"]["id"]
        subnet = self.subnet(subnet_id)
        if subnet is None:
            return {"result": 3, "text": f"No subnet with id {subnet_id} found"}
        return {
            "result": 0,
            "text": f"Info about IPv{self.family} subnet {subnet['subnet']} (id {subnet_id}) returned",
            "arguments": {f"subnet{self.family}": [dict(subnet)]},
        }

    def _subnet_update(self, body: dict[str, Any]) -> dict[str, Any]:
        # Kea replaces the Subnet and keeps its Shared Network membership.
        (subnet,) = body["arguments"][f"subnet{self.family}"]
        if self.subnet(subnet["id"]) is None:
            return {"result": 1, "text": f"Can't find subnet '{subnet['id']}' to update"}
        self.subnets = [dict(subnet) if existing["id"] == subnet["id"] else existing for existing in self.subnets]
        self._changes += 1
        return {
            "result": 0,
            "text": f"IPv{self.family} subnet updated",
            "arguments": {"subnets": [{"id": subnet["id"], "subnet": subnet["subnet"]}]},
        }

    def _network_subnet_add(self, body: dict[str, Any]) -> dict[str, Any]:
        name, subnet_id = body["arguments"]["name"], body["arguments"]["id"]
        subnet = self.subnet(subnet_id)
        if name not in self.networks:
            return {"result": 3, "text": f"no IPv{self.family} shared network with name '{name}' found"}
        if subnet is None:
            return {"result": 3, "text": f"no IPv{self.family} subnet with id '{subnet_id}' found"}
        if subnet_id in self.members:
            return {
                "result": 1,
                "text": f"subnet {subnet_id} being added to a shared network already belongs to a shared network",
            }
        self.members[subnet_id] = name
        self._changes += 1
        return {
            "result": 0,
            "text": (
                f"IPv{self.family} subnet {subnet['subnet']} (id {subnet_id}) is now part of shared network '{name}'"
            ),
        }

    def _network_subnet_del(self, body: dict[str, Any]) -> dict[str, Any]:
        name, subnet_id = body["arguments"]["name"], body["arguments"]["id"]
        subnet = self.subnet(subnet_id)
        if name not in self.networks:
            return {"result": 3, "text": f"no IPv{self.family} shared network with name '{name}' found"}
        if subnet is None or self.members.get(subnet_id) != name:
            return {
                "result": 3,
                "text": (
                    f"The IPv{self.family} subnet with id {subnet_id} is not part of the shared network with name "
                    f"'{name}' found"
                ),
            }
        del self.members[subnet_id]
        self._changes += 1
        return {
            "result": 0,
            "text": (
                f"IPv{self.family} subnet {subnet['subnet']} (id {subnet_id}) is now removed from shared network "
                f"'{name}'"
            ),
        }

    def _subnet_del(self, body: dict[str, Any]) -> dict[str, Any]:
        subnet_id = body["arguments"]["id"]
        if subnet_id not in self.ids():
            return {"result": 3, "text": f"no subnet with id {subnet_id}"}
        self.remove(subnet_id)
        return {"result": 0, "text": f"IPv{self.family} subnet {subnet_id} deleted"}


@contextmanager
def stub_kea(responses: dict[str, Any]):
    """Exercise a view against a real ``KeaClient`` with the HTTP boundary stubbed.

    Yields a :class:`KeaHttpStub` so tests can assert on the real request bodies::

        with stub_kea({"lease4-del": {"result": 0, "text": "Success"}}) as kea:
            resp = self.client.post(url, ...)
        assert "lease4-del" in kea.commands()
    """
    stub = KeaHttpStub(responses)

    def _post(self, url, **kwargs):  # mirrors requests.Session.post(self, url, ...)
        return stub(url, **kwargs)

    with patch("netbox_kea.kea.requests.Session.post", new=_post):
        yield stub
