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


class KeaHttpStub:
    """Dispatch Kea commands by name and record the request bodies sent.

    ``responses`` maps a command name to what that command should return. A value
    may be:

    * a dict/list payload — used for every call of that command;
    * a ``list`` — consumed as a FIFO queue (the last item repeats);
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
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = {k: (list(v) if isinstance(v, list) else v) for k, v in responses.items()}
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> MagicMock:
        body = kwargs.get("json") or {}
        self.requests.append(body)
        cmd = body.get("command")
        if cmd not in self._responses:
            raise AssertionError(f"KeaHttpStub: no response registered for command {cmd!r} (url={url})")
        spec = self._responses[cmd]
        if isinstance(spec, list):
            spec = spec.pop(0) if len(spec) > 1 else spec[0]
        # A callable (but not an exception class) is resolved against the body.
        if callable(spec) and not _is_exc(spec):
            spec = spec(body)
        if _is_exc(spec):
            raise spec() if isinstance(spec, type) else spec
        return _http_response(spec if isinstance(spec, list) else [spec])

    # --- assertion helpers ---
    def commands(self) -> list[str]:
        """Ordered list of command names sent."""
        return [r.get("command") for r in self.requests]

    def bodies(self, command: str) -> list[dict[str, Any]]:
        """Every request body sent for *command* (for asserting args / absence of ``service``)."""
        return [r for r in self.requests if r.get("command") == command]


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
