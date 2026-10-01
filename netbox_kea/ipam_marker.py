# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The IPAM sync marker (ADR 0006): the block at the start of a description that the sync writes.

A marked description starts with ``[kea-sync: <kind>]``, followed by free operator text. The sync rewrites only the
block and keeps the text after it byte for byte. A description that starts with the legacy phrase ``Synced from Kea
DHCP`` is marked too, and its next rewrite replaces the phrase with the block. Every reader and writer of the marker
goes through this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from django.db.models import Q

MarkerKind = Literal["lease", "reservation", "lease + reservation", "subnet", "delegated prefix", "pool"]
MARKER_KINDS: tuple[MarkerKind, ...] = get_args(MarkerKind)

#: The length limit of ``description`` on NetBox IP addresses, Prefixes and IP Ranges.
DESCRIPTION_MAX_LENGTH = 200

_LEGACY_PHRASE = "Synced from Kea DHCP"
# The legacy kinds, longest first, so " lease + reservation" matches before " lease".
_LEGACY_KINDS: tuple[MarkerKind, ...] = tuple(sorted(MARKER_KINDS, key=len, reverse=True))
_STATUS_KINDS: dict[str, MarkerKind] = {"dhcp": "lease", "reserved": "reservation", "active": "lease + reservation"}


def _block(kind: MarkerKind) -> str:
    return f"[kea-sync: {kind}]"


@dataclass(frozen=True)
class Marker:
    """The marker at the start of a description: its kind and the text after it.

    ``rest`` is the text after the block or the legacy phrase, byte for byte. ``kind`` is ``None`` only for a
    legacy phrase that no known kind follows.
    """

    kind: MarkerKind | None
    rest: str
    legacy: bool

    @property
    def note(self) -> str:
        """Return the operator note: the rest without the one space that separates it from the marker."""
        return self.rest.removeprefix(" ")


def parse_marker(description: str) -> Marker | None:
    """Return the marker at the start of *description*, or ``None`` when it does not start with one."""
    for kind in MARKER_KINDS:
        block = _block(kind)
        if description.startswith(block):
            return Marker(kind, description[len(block) :], legacy=False)
    if not description.startswith(_LEGACY_PHRASE):
        return None
    rest = description[len(_LEGACY_PHRASE) :]
    for kind in _LEGACY_KINDS:
        if rest.startswith(f" {kind}"):
            after = rest[len(kind) + 1 :]
            if after and (after[0].isalnum() or after[0] in "-_"):
                # The longest matching kind continues as a word, as in "leases": no kind is named.
                break
            return Marker(kind, after, legacy=True)
    return Marker(None, rest, legacy=True)


def render_marker(kind: MarkerKind) -> str:
    """Return the description of a new object: the block of *kind* and no note."""
    return _block(kind)


def rewrite_marker(marker: Marker, kind: MarkerKind) -> str | None:
    """Return the description with the block of *kind* in place of *marker*, and the text after it unchanged.

    Return ``None`` when the result does not fit in :data:`DESCRIPTION_MAX_LENGTH`. The sync then leaves the object
    unchanged and does not cut the text (ADR 0006).
    """
    description = _block(kind) + marker.rest
    return description if len(description) <= DESCRIPTION_MAX_LENGTH else None


def status_kind(status: str) -> MarkerKind:
    """Return the marker kind of an IP address status that the sync writes."""
    return _STATUS_KINDS[status]


def marked_description_q() -> Q:
    """Return a filter on ``description`` that matches the same rows as :func:`parse_marker`."""
    query = Q(description__startswith=_LEGACY_PHRASE)
    for kind in MARKER_KINDS:
        query |= Q(description__startswith=_block(kind))
    return query
