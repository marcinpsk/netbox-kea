# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for netbox-ipam-get-or-create-non-unique-key. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
from dcim.models import MACAddress
from ipam.models import IPAddress, IPRange, Prefix


def bad_prefix(cidr, vrf):
    # ruleid: netbox-ipam-get-or-create-non-unique-key
    return Prefix.objects.get_or_create(prefix=cidr, vrf=vrf, defaults={"status": "active"})


def bad_ip_range(start, end, vrf):
    # ruleid: netbox-ipam-get-or-create-non-unique-key
    return IPRange.objects.get_or_create(start_address__net_host=start, end_address__net_host=end, vrf=vrf)


def bad_ip_address(address):
    # ruleid: netbox-ipam-get-or-create-non-unique-key
    return IPAddress.objects.get_or_create(address=address)


def bad_prefix_chained(cidr, vrf):
    # ruleid: netbox-ipam-get-or-create-non-unique-key
    return Prefix.objects.filter(vrf=vrf).get_or_create(prefix=cidr)


def bad_ip_range_chained(user, start, end):
    # ruleid: netbox-ipam-get-or-create-non-unique-key
    return IPRange.objects.restrict(user, "view").filter(vrf=None).get_or_create(start_address=start, end_address=end)


def bad_ip_address_chained(address):
    # ruleid: netbox-ipam-get-or-create-non-unique-key
    return IPAddress.objects.all().get_or_create(address=address)


def ok_mac_address_chained(mac):
    # ok: netbox-ipam-get-or-create-non-unique-key
    return MACAddress.objects.filter(mac_address=mac).get_or_create(mac_address=mac)


def ok_prefix_filter(cidr, vrf):
    # ok: netbox-ipam-get-or-create-non-unique-key
    return list(Prefix.objects.filter(prefix=cidr, vrf=vrf).order_by("pk"))


def ok_create(cidr):
    # ok: netbox-ipam-get-or-create-non-unique-key
    return Prefix.objects.create(prefix=cidr)


def ok_mac_address(mac):
    # ok: netbox-ipam-get-or-create-non-unique-key
    return MACAddress.objects.get_or_create(mac_address=mac)
