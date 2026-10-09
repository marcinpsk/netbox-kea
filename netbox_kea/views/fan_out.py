# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The one fan-out of a combined page: a per-Server read for each Server, in worker threads."""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from ..models import Server
from .notices import HEADLINES, ServerNotices, SnapshotKind

logger = logging.getLogger(__name__)

_MAX_WORKERS = 10

T = TypeVar("T")


@dataclass
class FanOut(Generic[T]):
    """The read of each Server, in Server order, and the errors of the Servers that could not be read."""

    results: list[tuple[Server, T]] = field(default_factory=list)
    notices: ServerNotices = field(default_factory=ServerNotices)


def fan_out(servers: Sequence[Server], kind: SnapshotKind, fetch: Callable[[Server], T]) -> FanOut[T]:
    """Run *fetch* for each Server in worker threads.

    *fetch* reads Kea only and gives a Kea, transport or reply failure as its own Notice. A ``ValueError``, a
    configuration or argument error outside the Notice rule, becomes the error headline of *kind* for its Server.
    """
    read: FanOut[T] = FanOut()
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = [(server, executor.submit(fetch, server)) for server in servers]
    for server, future in futures:
        try:
            read.results.append((server, future.result()))
        except ValueError:  # noqa: PERF203
            logger.exception("Failed to query server %s", server.name)
            read.notices.errors.append((server.name, HEADLINES[kind]))
    return read
