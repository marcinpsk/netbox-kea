# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Open every plugin transaction, so that NetBox dispatches no event for a write that a unit rolled back.

At the top level of a tracked request (no connection holds a transaction) the block is a unit inside its own
``event_tracking``: its events dispatch after its COMMIT, or not at all. Elsewhere it is a plain transaction or
savepoint. See docs/design/savepoint-event-queue.md, sections 22, 24 and 26.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from . import branching


class EventDispatchError(RuntimeError):
    """NetBox's event tracking raised at exit after the unit committed, normally in the flush to the pipeline."""


def _nested_tracking() -> bool:
    # On exit, NetBox 4.3 to 4.6 set the request to None and the queue to empty instead of the caller's values.
    major, minor = settings.RELEASE.version.split("-", 1)[0].split(".")[:2]
    return (int(major), int(minor)) >= (4, 7)


_NESTED_TRACKING = _nested_tracking()


def in_transaction(*aliases: str) -> bool:
    """Return whether a connection holds a transaction, or would open one; never open a connection to decide."""
    for alias in {*branching.connection_aliases(), *aliases}:
        connection = connections[alias]
        if connection.in_atomic_block:
            return True
        if connection.connection is None:
            if not connection.settings_dict["AUTOCOMMIT"]:
                return True
        elif not connection.get_autocommit():
            return True
    return False


@contextmanager
def atomic(using: str | None = None) -> Iterator[None]:
    """Run the block in a transaction, as a unit at the top level of a tracked request, else as a plain block."""
    from netbox.context import current_request

    request = current_request.get()
    if request is None or not _NESTED_TRACKING or in_transaction(using or DEFAULT_DB_ALIAS):
        with transaction.atomic(using=using):
            yield
        return
    from netbox.context_managers import event_tracking

    committed = False
    try:
        with event_tracking(request):
            with transaction.atomic(using=using):
                yield
            committed = True
    except Exception as error:
        if committed:
            raise EventDispatchError("NetBox could not dispatch the events of a committed transaction") from error
        raise
