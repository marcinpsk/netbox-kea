# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The one rule that turns a Snapshot into a notice for the operator (ADR 0003, Presentation).

A view gets a :class:`Notice` from :func:`notice` or :func:`load_snapshot` and hands it to a channel:
:func:`show_notices` on a full page, the Notice itself in a template, or :class:`ServerNotices` on a combined page.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

import requests
from django.contrib import messages
from django.http import HttpRequest

from ..kea import KeaException
from ..leases import LeaseDiagnostic, LeaseSnapshot
from ..models import Server
from ..reservations import ReservationDiagnostic, ReservationSnapshot
from ..server_configuration import Diagnostic, ServerConfigurationSnapshot
from ..subnet_catalogue import CatalogueSnapshot
from ..utilities import diagnostic_reasons

logger = logging.getLogger(__name__)

SnapshotKind = Literal["catalogue", "configuration", "reservation", "lease"]
RecordKind = Literal["reservation", "lease"]
Snapshot = CatalogueSnapshot | ServerConfigurationSnapshot | ReservationSnapshot | LeaseSnapshot
RecordSnapshot = TypeVar("RecordSnapshot", ReservationSnapshot, LeaseSnapshot)

#: The first line of an unavailable Notice, one per Snapshot kind.
HEADLINES: dict[SnapshotKind, str] = {
    "catalogue": "Failed to load subnet configuration from Kea.",
    "configuration": "Failed to load the Server Configuration from Kea.",
    "reservation": "Failed to load Reservations from Kea.",
    "lease": "Failed to query server",
}
_RECORD_KINDS: frozenset[SnapshotKind] = frozenset({"reservation", "lease"})


@dataclass(frozen=True)
class Notice:
    """The level of one Snapshot and its typed diagnostics. A channel formats them."""

    kind: SnapshotKind
    level: int
    diagnostics: tuple[Diagnostic | ReservationDiagnostic | LeaseDiagnostic, ...] = ()
    #: A raised read failed because Kea does not know the command, so its hook library is not loaded.
    unsupported_command: bool = False

    @property
    def unavailable(self) -> bool:
        """Return whether the Snapshot gave no usable facts."""
        return self.level == messages.ERROR

    @property
    def headline(self) -> str:
        """Return the headline of an unavailable Notice, else an empty string."""
        return HEADLINES[self.kind] if self.unavailable else ""

    @property
    def css(self) -> str:
        """Return the Bootstrap alert colour of the level."""
        return "danger" if self.unavailable else "warning"

    @property
    def lines(self) -> tuple[str, ...]:
        """Return the headline, then each distinct message. A record list shows Reservation and Lease diagnostics."""
        texts = () if self.kind in _RECORD_KINDS else tuple(diagnostic.message for diagnostic in self.diagnostics)
        return tuple(dict.fromkeys(text for text in (self.headline, *texts) if text))

    @property
    def reasons(self) -> str:
        """Return each distinct diagnostic message once, in order."""
        return diagnostic_reasons(self.diagnostics)


def _kind(snapshot: Snapshot) -> SnapshotKind:
    if isinstance(snapshot, CatalogueSnapshot):
        return "catalogue"
    if isinstance(snapshot, ServerConfigurationSnapshot):
        return "configuration"
    if isinstance(snapshot, ReservationSnapshot):
        return "reservation"
    if isinstance(snapshot, LeaseSnapshot):
        return "lease"
    raise TypeError(f"{type(snapshot).__name__} is not a Snapshot.")


def notice(snapshot: Snapshot) -> Notice | None:
    """Return an error for an unavailable Snapshot, a warning for a usable one with diagnostics, else None.

    A Snapshot that is incomplete only because more pages remain has no diagnostics, so it gives no Notice.
    """
    kind = _kind(snapshot)
    if isinstance(snapshot, (CatalogueSnapshot, ServerConfigurationSnapshot)) and snapshot.unavailable:
        return Notice(kind, messages.ERROR, snapshot.diagnostics)
    if snapshot.diagnostics:
        return Notice(kind, messages.WARNING, snapshot.diagnostics)
    return None


def load_snapshot(server: Server, kind: RecordKind, read: Callable[[], RecordSnapshot]) -> RecordSnapshot | Notice:
    """Run a Reservation or Lease read, and return an unavailable Notice when Kea, the transport or the reply fails.

    A refused Lease query (``LeaseQueryGuardError``) and a ``ValueError`` are outside the rule and still raise.
    """
    try:
        return read()
    except KeaException as exc:
        if exc.unsupported_command:
            logger.info("Server %s does not provide the %s read: its hook library is not loaded", server.pk, kind)
        else:
            logger.exception("Kea refused the %s read of server %s", kind, server.pk)
        return Notice(kind, messages.ERROR, unsupported_command=exc.unsupported_command)
    # MalformedReply and MalformedLeaseResponse are RuntimeErrors.
    except (requests.RequestException, RuntimeError):
        logger.exception("Failed to read %s from server %s", kind, server.pk)
        return Notice(kind, messages.ERROR)


def _worst_levels(notices: tuple[Notice | None, ...]) -> dict[str, int]:
    """Return each distinct line of *notices* once, in order, at the worst level that reported it."""
    levels: dict[str, int] = {}
    for item in notices:
        if item is None:
            continue
        for line in item.lines:
            levels[line] = max(levels.get(line, item.level), item.level)
    return levels


def show_notices(request: HttpRequest, *notices: Notice | None) -> None:
    """Show the Notices of one Server's Snapshots as Django messages."""
    for line, level in _worst_levels(notices).items():
        messages.add_message(request, level, line)


@dataclass
class ServerNotices:
    """The per-Server lists of a combined page. Each Server keeps its own lines, also when two Servers agree."""

    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    #: The Notices with record diagnostics, for the page's record list.
    records: list[tuple[str, Notice]] = field(default_factory=list)

    def add(self, server: Server, *notices: Notice | None) -> None:
        """Add the Notices of the Snapshots of one Server."""
        for line, level in _worst_levels(notices).items():
            (self.errors if level == messages.ERROR else self.warnings).append((server.name, line))
        self.records.extend(
            (server.name, item)
            for item in notices
            if item is not None and item.kind in _RECORD_KINDS and item.diagnostics
        )

    @property
    def record_diagnostics(self) -> list[tuple[str, Diagnostic | ReservationDiagnostic | LeaseDiagnostic]]:
        """Return each record diagnostic with the name of its Server, for a record list."""
        return [(name, diagnostic) for name, item in self.records for diagnostic in item.diagnostics]
