# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# ruleid: kea-no-event-queue-internals
from extras.events import EventContext, enqueue_event
from netbox.context import current_request
# ruleid: kea-no-event-queue-internals
from netbox.context import events_queue as queue
from netbox.context_managers import event_tracking

from netbox import context


def restore(saved):
    # ruleid: kea-no-event-queue-internals
    context.events_queue.set(saved)
    queue.set(saved)


def enqueue(instance, request):
    # ruleid: kea-no-event-queue-internals
    enqueue_event({}, instance, request, "object_created")
    # ruleid: kea-no-event-queue-internals
    return EventContext()


def track(request):
    # ok: kea-no-event-queue-internals
    with event_tracking(current_request.get() or request):
        pass
