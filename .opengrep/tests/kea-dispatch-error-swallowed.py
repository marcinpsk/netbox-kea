# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from netbox_kea import event_scope


def broad(obj):
    try:
        # ruleid: kea-dispatch-error-swallowed
        with event_scope.atomic():
            obj.save()
    except ValueError:
        pass
    except Exception:
        pass


def broad_in_a_loop(rows):
    try:
        for row in rows:
            # ruleid: kea-dispatch-error-swallowed
            with event_scope.atomic():
                row.save()
    except Exception as exc:
        print(type(exc))


def bare(obj):
    try:
        # ruleid: kea-dispatch-error-swallowed
        with event_scope.atomic():
            obj.save()
    except:  # noqa: E722
        pass


def base(obj):
    try:
        # ruleid: kea-dispatch-error-swallowed
        with event_scope.atomic():
            obj.save()
    except BaseException:
        pass


def reraised(obj):
    try:
        # ok: kea-dispatch-error-swallowed
        with event_scope.atomic():
            obj.save()
    except event_scope.EventDispatchError:
        raise
    except Exception as exc:
        print(type(exc))


def narrow(obj):
    try:
        # ok: kea-dispatch-error-swallowed
        with event_scope.atomic():
            obj.save()
    except (LookupError, ValueError):
        pass


def broad_with_else(obj):
    try:
        # ruleid: kea-dispatch-error-swallowed
        with event_scope.atomic():
            obj.save()
    except Exception as exc:
        print(type(exc))
    else:
        return obj
    return None


def reraised_with_else(obj):
    try:
        # ok: kea-dispatch-error-swallowed
        with event_scope.atomic():
            obj.save()
    except event_scope.EventDispatchError:
        raise
    except Exception as exc:
        print(type(exc))
    else:
        return obj
    return None
