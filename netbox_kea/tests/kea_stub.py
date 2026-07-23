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

import threading
from collections import deque
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import requests


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
            return [r.get("command") for r in self.requests]

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


def _res_page(hosts: Any, *, next_from: int = 0, next_source: int = 0) -> dict[str, Any]:
    """A ``reservation-get-page`` payload: *hosts* plus Kea's pagination cursor.

    ``next_from``/``next_source`` both 0 marks the source exhausted, so
    ``iter_reservations`` stops after this page.
    """
    return {"result": 0, "arguments": {"hosts": list(hosts), "next": {"from": next_from, "source-index": next_source}}}


def _res_get(reservation: dict[str, Any]) -> dict[str, Any]:
    """A ``reservation-get`` payload: the host fields Kea returns directly inside ``arguments``."""
    return {"result": 0, "arguments": dict(reservation)}


def _subnet_get(version: int, pools: list[str] | None = None, subnet_id: int = 1) -> dict[str, Any]:
    """A ``subnet{v}-get`` payload for the reservation-add pool-overlap probe.

    *pools* is a list of pool range strings; the probe warns only when the
    reservation IP falls inside one of them.
    """
    return {
        "result": 0,
        "arguments": {f"subnet{version}": [{"id": subnet_id, "pools": [{"pool": p} for p in (pools or [])]}]},
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
