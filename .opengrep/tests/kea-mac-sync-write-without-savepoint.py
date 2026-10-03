# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from dcim.models import MACAddress
from django.db import transaction


def sync_mac_address(hardware, hostname):
    # ruleid: kea-mac-sync-write-without-savepoint
    mac_obj, _ = MACAddress.objects.get_or_create(mac_address=hardware)
    # ruleid: kea-mac-sync-write-without-savepoint
    mac_obj.save()
    with transaction.atomic():
        # ok: kea-mac-sync-write-without-savepoint
        mac_obj, _ = MACAddress.objects.get_or_create(mac_address=hardware)
        # ok: kea-mac-sync-write-without-savepoint
        mac_obj.save()


def save_other_row(mac_obj):
    # ok: kea-mac-sync-write-without-savepoint
    mac_obj.save()
