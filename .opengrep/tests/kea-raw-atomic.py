# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
import django.db.transaction
from django.db import transaction
from django.db import transaction as tx

# ruleid: kea-raw-atomic
from django.db.transaction import atomic

from netbox_kea import event_scope


def caught():
    # ruleid: kea-raw-atomic
    with transaction.atomic():
        pass


# ruleid: kea-raw-atomic
@transaction.atomic
def decorated():
    pass


def aliased():
    # ruleid: kea-raw-atomic
    with tx.atomic(using="default"):
        pass
    # ruleid: kea-raw-atomic
    with atomic():
        pass
    # ruleid: kea-raw-atomic
    with django.db.transaction.atomic():
        pass


def owned(callback):
    # ok: kea-raw-atomic
    with event_scope.atomic():
        pass
    # ok: kea-raw-atomic
    transaction.on_commit(callback)
