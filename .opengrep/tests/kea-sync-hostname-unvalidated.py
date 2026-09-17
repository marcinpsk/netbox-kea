# SPDX-FileCopyrightText: 2026 Marcin Zieba
# SPDX-License-Identifier: Apache-2.0


def sync_lease_to_netbox(lease):
    # ruleid: kea-sync-hostname-unvalidated
    hostname = lease.get("hostname", "")
    # ruleid: kea-sync-hostname-unvalidated
    hostname = lease.get("hostname")
    # ruleid: kea-sync-hostname-unvalidated
    hostname = lease["hostname"]
    # ok: kea-sync-hostname-unvalidated
    hostname = _record_hostname(lease)


def _record_hostname_and_addresses(record):
    # ruleid: kea-sync-hostname-unvalidated
    hostname = record.get("hostname", "")
    # ruleid: kea-sync-hostname-unvalidated
    hostname = record["hostname"]
    # ok: kea-sync-hostname-unvalidated
    return _record_hostname(record), set()


def _record_hostname(record):
    # ok: kea-sync-hostname-unvalidated
    hostname = record.get("hostname")
    if hostname is None:
        return ""
    if not isinstance(hostname, str):
        raise RuntimeError("Kea record hostname must be a string or null.")
    return hostname
