# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from netbox_kea import event_scope


def refusal_after_save(obj, bad):
    try:
        # The RuntimeError handler also swallows EventDispatchError.
        # ruleid: kea-dispatch-error-swallowed
        with event_scope.atomic():
            obj.save()
            if bad:
                # ruleid: kea-refusal-after-event-write
                raise RuntimeError("refused")
    except RuntimeError:
        pass


def refusal_before_save(obj, bad):
    try:
        # The RuntimeError handler also swallows EventDispatchError.
        # ruleid: kea-dispatch-error-swallowed
        with event_scope.atomic():
            if bad:
                # ok: kea-refusal-after-event-write
                raise RuntimeError("refused")
            obj.save()
    except RuntimeError:
        pass


def refusal_after_get_or_create(model, address):
    try:
        with event_scope.atomic():
            row, _ = model.objects.get_or_create(address=address)
            # ruleid: kea-refusal-after-event-write
            raise LookupError(row)
    except LookupError:
        pass


def refusal_after_create_in_a_loop(model, rows):
    try:
        with event_scope.atomic():
            model.objects.create(address=rows[0])
            for row in rows:
                if not row:
                    # ruleid: kea-refusal-after-event-write
                    raise LookupError(row)
    except LookupError:
        pass


def refusal_after_m2m_write(obj, tags):
    try:
        with event_scope.atomic():
            obj.tags.set(tags)
            # ruleid: kea-refusal-after-event-write
            raise ValueError
    except ValueError:
        pass


def uncaught_refusal(obj):
    with event_scope.atomic():
        obj.save()
        # ok: kea-refusal-after-event-write
        raise RuntimeError("the caller sees the exception, and the block rolls back with its events")


def handler_reraise(obj):
    try:
        with event_scope.atomic():
            obj.save()
    except ValueError:
        # ok: kea-refusal-after-event-write
        raise
