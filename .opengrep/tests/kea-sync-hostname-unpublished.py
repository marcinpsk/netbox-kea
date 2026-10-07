# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0


def _lease_report(lease, prefix_length):
    # ruleid: kea-sync-hostname-unpublished
    facts = _Facts(lease.hostname, prefix_length)
    # ok: kea-sync-hostname-unpublished
    hostname = lease_published_name(lease.hostname)
    return facts, hostname


def _reservation_rows(reservation, catalogue, hardware):
    # ruleid: kea-sync-hostname-unpublished
    mac_addresses = ((hardware, reservation.hostname),)
    # ok: kea-sync-hostname-unpublished
    hostname = reservation_published_name(reservation, catalogue)
    return mac_addresses, hostname


def _claim_reports(record, hardware):
    # ruleid: kea-sync-hostname-unpublished
    return _resolve_mac(hardware, record.hostname)


def _merge(facts, other):
    # ok: kea-sync-hostname-unpublished
    return facts.hostname or other.hostname


def _reservation_mirror(obj, reservation):
    # ok: kea-sync-hostname-unpublished
    if reservation.hostname:
        # ok: kea-sync-hostname-unpublished
        obj.hostname = reservation.hostname or None
