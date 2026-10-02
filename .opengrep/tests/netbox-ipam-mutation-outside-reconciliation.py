# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Intentional rule violations and unrelated-model controls.
from django.db.models import QuerySet
from dcim.models import Device
from ipam.models import IPAddress, IPRange, Prefix
from ipam.models import IPAddress as Address
from ipam import models as ipam
from netbox_kea.models import Server
from core.models import Job


def direct_queries(pk):
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    IPAddress.objects.filter(pk=pk).delete()
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    Prefix.objects.all().filter(pk=pk).update(status="deprecated")
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    IPRange.objects.filter(pk=pk).delete()
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    Address.objects.filter(pk=pk).update(status="active")
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    ipam.Prefix.objects.filter(pk=pk).delete()
    # ok: netbox-ipam-mutation-outside-reconciliation
    IPAddress.objects.filter(pk=pk).update(dns_name="host.example")
    # ok: netbox-ipam-mutation-outside-reconciliation
    Device.objects.filter(pk=pk).delete()
    # ok: netbox-ipam-mutation-outside-reconciliation
    Server.objects.filter(pk=pk).delete()
    # ok: netbox-ipam-mutation-outside-reconciliation
    Job.objects.filter(pk=pk).update(status="completed")


def assigned_queries(pk):
    row = Address.objects.filter(pk=pk)
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    row.delete()
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    row.update(status="deprecated")
    obj = ipam.IPRange.objects.filter(pk=pk).first()
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    obj.status = "active"


def typed_objects(address: Address, prefix: Prefix, ip_range: IPRange):
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    address.status = "dhcp"
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    prefix.delete()
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    ip_range.status = "deprecated"
    # ok: netbox-ipam-mutation-outside-reconciliation
    address.description = "operator note"


def unrelated_objects(device: Device, server: Server, job: Job, rq_job):
    # ok: netbox-ipam-mutation-outside-reconciliation
    device.delete()
    # ok: netbox-ipam-mutation-outside-reconciliation
    server.delete()
    # ok: netbox-ipam-mutation-outside-reconciliation
    job.status = "completed"
    # ok: netbox-ipam-mutation-outside-reconciliation
    rq_job.delete()


def typed_queryset(rows: QuerySet[Address]):
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    rows.delete()
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    rows.update(status="deprecated")


def local_objects(pk):
    address: Address = Address.objects.get(pk=pk)
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    address.status = "dhcp"
    prefix = Prefix(prefix="198.18.0.0/24")
    # ruleid: netbox-ipam-mutation-outside-reconciliation
    prefix.status = "active"


def typed_other_queryset(rows: QuerySet[Device]):
    # ok: netbox-ipam-mutation-outside-reconciliation
    rows.delete()
