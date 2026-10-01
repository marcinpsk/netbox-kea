# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IPAM Reconciliation (ADR 0006): the one owner of IPAM Ownership links, stale cleanup and per-row savepoints.

``reconcile`` runs the complete phases of one Server and family. It links every owned object that a phase
reports, and a complete phase removes its own stale links. Each row runs in its own transaction, or in a
savepoint when the caller holds a transaction, under a transaction-level advisory lock on the object identity.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import requests
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from ipam.models import IPAddress

from .constants import Family, StaleCleanupMode
from .integrations import dhcp_plugin
from .ipam_marker import DESCRIPTION_MAX_LENGTH, parse_marker, render_marker, status_kind
from .kea import KeaException, lease_fields
from .models import IPAMOwnershipLink, IPAMOwnershipSource, next_confirmation_number
from .sync import (
    _apply_ip_fields,
    _apply_ip_mask,
    _compute_ip_status,
    _get_stale_cleanup_mode,
    _ip_description,
    _record_hostname,
    _resolve_prefix_length,
    _sync_mac_address,
)

if TYPE_CHECKING:
    from .models import Server

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

# How many row failures of one reconcile call the log names; the error count stays exact.
_ROW_ERROR_LOG_LIMIT = 10


def _int4(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big", signed=True)


# The first key of the two-key advisory lock form: config_write locks use another class, NetBox's one-key locks none.
_LOCK_CLASS = _int4("netbox_kea.ipam_reconciliation")


@dataclass(frozen=True)
class LeasePhase:
    """The lease phase of one reconcile call: the Server's complete lease snapshot of the family.

    ``reservation_addresses`` is the complete Reservation snapshot of the same run, or ``None`` when it is not
    complete. The lease status is ``active`` for these addresses (bridge until the Reservation phase moves, #209).
    """

    max_leases: int | None
    subnet_prefix_lengths: dict[int, int]
    reservation_addresses: frozenset[str] | None


@dataclass
class SyncReport:
    """What one reconcile call did. Conflicts and owner disagreements are canonical addresses."""

    created: int = 0
    updated: int = 0
    removed: int = 0
    deprecated: int = 0
    errors: int = 0
    conflicts: set[str] = field(default_factory=set)
    disagreements: set[str] = field(default_factory=set)
    complete: bool = True
    # The valid lease records of the snapshot and their canonical addresses: the old Reservation phase and stale
    # cleanup read them until #214.
    lease_records: list[dict[str, Any]] = field(default_factory=list)
    lease_addresses: set[str] = field(default_factory=set)

    def fail_snapshot(self, what: str, exc: BaseException) -> None:
        """Count one snapshot that could not be read, which makes the phase incomplete, and log it."""
        self.errors += 1
        self.complete = False
        logger.warning("%s failed: %s", what, exc)

    def fail_row(self, what: str, exc: BaseException) -> None:
        """Count one failed row, which makes the phase incomplete, and log the first failures."""
        self.errors += 1
        self.complete = False
        if self.errors <= _ROW_ERROR_LOG_LIMIT:
            logger.warning("IPAM reconciliation of %s failed: %s", what, exc, exc_info=exc)
        elif self.errors == _ROW_ERROR_LOG_LIMIT + 1:
            logger.warning("Further row failures of this reconciliation are not logged; see the error count.")


@dataclass(frozen=True)
class _LeaseFacts:
    hostname: str
    prefix_length: int

    def stored(self) -> dict[str, Any]:
        return {"hostname": self.hostname, "prefix_length": self.prefix_length}


@dataclass(frozen=True)
class _LeaseReport:
    """Everything one lease phase reports for one address. No facts: it reported the address twice, differently."""

    address: str
    facts: _LeaseFacts | None
    records: tuple[dict[str, Any], ...]


_Outcome = Literal["created", "updated", "unchanged", "conflict", "disagreement"]


class _RowRefused(Exception):
    """The row does not identify one object, so the run cannot decide it: two rows, or an address that moved."""


def reconcile(server: Server, family: Family, phases: Sequence[LeasePhase]) -> SyncReport:
    """Run *phases* for one Server and family: link what they report, then remove the stale links of each complete one.

    The claims of all phases run before any link is removed. Writes go to main only: the job refuses to run in a
    branch.
    """
    mode = _get_stale_cleanup_mode()
    report = SyncReport()
    cutoffs = [_claim_leases(server, family, phase, report) for phase in phases]
    for cutoff in cutoffs:
        if cutoff is not None and report.complete:
            _remove_stale_lease_links(server, family, cutoff, mode, report)
    logger.info(
        "Server %s (v%s): IPAM reconciliation created=%d updated=%d removed=%d deprecated=%d conflicts=%d"
        " disagreements=%d errors=%d complete=%s",
        server.name,
        family,
        report.created,
        report.updated,
        report.removed,
        report.deprecated,
        len(report.conflicts),
        len(report.disagreements),
        report.errors,
        report.complete,
    )
    return report


def _lock_identity(vrf_id: int | None, address: str) -> None:
    """Hold the advisory lock of one IP address identity, the VRF and the address, until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_CLASS, _int4(f"ip-address {vrf_id} {address}")])


def _each_row(
    rows: Iterable[T], report: SyncReport, work: Callable[[T], R], name: Callable[[T], str]
) -> Iterator[tuple[T, R]]:
    """Run *work* for each row in its own transaction or savepoint; a database error fails only that row."""
    for row in rows:
        try:
            with transaction.atomic():
                outcome = work(row)
        except (DatabaseError, _RowRefused) as exc:
            report.fail_row(name(row), exc)
            continue
        yield row, outcome


def _claim_leases(server: Server, family: Family, phase: LeasePhase, report: SyncReport) -> int | None:
    """Link every lease that the snapshot reports. Return the phase's cutoff number, or None without a snapshot."""
    cutoff = next_confirmation_number()
    try:
        client = server.get_client(version=family)
        collection = client.lease_get_all(version=family, max_leases=phase.max_leases)
    except (KeaException, requests.RequestException, ValueError, RuntimeError) as exc:
        report.fail_snapshot(f"Server {server.name} (v{family}): the lease snapshot", exc)
        return None
    logger.info("Server %s (v%s): fetched %d leases", server.name, family, len(collection.leases))
    if collection.truncated:
        logger.warning(
            "Server %s (v%s): lease fetch truncated at %d: increase sync_max_leases_per_server",
            server.name,
            family,
            phase.max_leases,
        )
        report.complete = False

    reports = _lease_reports(collection.leases, phase, report)
    rows = _each_row(
        reports.values(),
        report,
        lambda lease: _claim_lease(server, family, lease, phase),
        lambda lease: f"lease {lease.address} of Server {server.name}",
    )
    for lease, outcome in rows:
        _count(report, lease.address, outcome)
        if outcome != "conflict":
            for record in lease.records:
                if hw_address := lease_fields(record).hw_address:
                    _sync_mac_address(hw_address, _record_hostname(record))
    return cutoff


def _lease_reports(leases: list[dict[str, Any]], phase: LeasePhase, report: SyncReport) -> dict[str, _LeaseReport]:
    """Group the valid lease records by canonical address. The snapshot already validated each address."""
    reports: dict[str, _LeaseReport] = {}
    for lease in leases:
        fields = lease_fields(lease)
        address = str(ipaddress.ip_address(fields.address))
        try:
            hostname = _record_hostname(lease)
        except RuntimeError as exc:
            report.fail_row(f"lease {address}", exc)
            continue
        report.lease_records.append(lease)
        report.lease_addresses.add(address)
        prefix_length = _resolve_prefix_length(address, fields.subnet_id, phase.subnet_prefix_lengths)
        facts = _LeaseFacts(hostname, prefix_length)
        earlier = reports.get(address)
        if earlier is None:
            reports[address] = _LeaseReport(address, facts, (lease,))
        else:
            same = earlier.facts == facts
            reports[address] = replace(earlier, facts=facts if same else None, records=(*earlier.records, lease))
    return reports


def _count(report: SyncReport, address: str, outcome: _Outcome) -> None:
    if outcome == "created":
        report.created += 1
    elif outcome == "updated":
        report.updated += 1
    elif outcome == "conflict":
        report.conflicts.add(address)
    elif outcome == "disagreement":
        report.disagreements.add(address)


def _host(address: Any) -> str:
    """Return the canonical host text of a NetBox address, the same text as a lease report's address."""
    return str(ipaddress.ip_address(str(address.ip)))


def _is_owned_description(description: str) -> bool:
    return parse_marker(description) is not None


def _claim_lease(server: Server, family: Family, lease: _LeaseReport, phase: LeasePhase) -> _Outcome:
    """Link one reported address under its identity lock, and apply the report when no owner disagrees."""
    vrf_id = server.sync_vrf_id
    _lock_identity(vrf_id, lease.address)
    rows = list(
        IPAddress.objects.select_for_update().filter(vrf_id=vrf_id, address__net_host=lease.address).order_by("pk")[:2]
    )
    if len(rows) > 1:
        raise _RowRefused(f"more than one IP address {lease.address} in the sync VRF")
    facts = lease.facts
    if not rows:
        if facts is None:
            return "disagreement"
        status = _compute_ip_status("lease", None, ip_str=lease.address, other_source_ips=phase.reservation_addresses)
        ip = IPAddress(
            address=f"{lease.address}/{facts.prefix_length}",
            vrf_id=vrf_id,
            status=status,
            dns_name=facts.hostname,
            description=render_marker(status_kind(status)),
        )
        ip.save()
        _store_link(None, server, family, ip, facts.stored(), stale_mark=None)
        return "created"

    ip = rows[0]
    links = list(IPAMOwnershipLink.objects.filter(ip_address=ip))
    if not _is_owned_description(ip.description):
        # A blank or curated description: the object is not owned, and an operator edit released it.
        IPAMOwnershipLink.objects.filter(pk__in=[link.pk for link in links]).delete()
        return "conflict"
    own = next((link for link in links if _is_own_lease_link(link, server, family)), None)
    others = [link for link in links if link is not own]
    _drop_superseded_stale_links(others)
    if facts is None:
        _store_link(own, server, family, ip, own.facts if own else None, stale_mark=_kept_mark(own))
        return "disagreement"
    if any(link.facts != facts.stored() for link in others if link.stale_mark is None and link.facts is not None):
        _store_link(own, server, family, ip, facts.stored(), stale_mark=_kept_mark(own))
        return "disagreement"

    status = _compute_ip_status("lease", ip.status, ip_str=lease.address, other_source_ips=phase.reservation_addresses)
    if len(_ip_description(ip.description, status, claim=False)) > DESCRIPTION_MAX_LENGTH:
        # The new marker and the operator note do not fit: the object stays as it is, and the owner keeps its link.
        _store_link(own, server, family, ip, facts.stored(), stale_mark=_kept_mark(own))
        return "conflict"
    changed = _apply_ip_fields(ip, status=status, hostname=facts.hostname)
    changed = _apply_ip_mask(ip, lease.address, facts.prefix_length) or changed
    if changed:
        ip.save()
    _store_link(own, server, family, ip, facts.stored(), stale_mark=None)
    return "updated" if changed else "unchanged"


def _is_own_lease_link(link: IPAMOwnershipLink, server: Server, family: Family) -> bool:
    return link.server_id == server.pk and link.family == family and link.source == IPAMOwnershipSource.LEASE


def _kept_mark(link: IPAMOwnershipLink | None) -> int | None:
    return link.stale_mark if link is not None else None


def _marked_and_unconfirmed(link: IPAMOwnershipLink) -> bool:
    """Return whether the link is marked stale and its owner has not confirmed it since the mark."""
    return link.stale_mark is not None and link.confirmation <= link.stale_mark


def _drop_superseded_stale_links(others: list[IPAMOwnershipLink]) -> None:
    """Another owner links the object, so a stale link goes unless its own owner confirmed it after the mark."""
    superseded = [link.pk for link in others if _marked_and_unconfirmed(link)]
    if superseded:
        IPAMOwnershipLink.objects.filter(pk__in=superseded).delete()


def _store_link(
    link: IPAMOwnershipLink | None,
    server: Server,
    family: Family,
    ip: IPAddress,
    facts: dict[str, Any] | None,
    *,
    stale_mark: int | None,
) -> None:
    """Confirm the owner's link with the next confirmation number, creating it when the owner has none."""
    confirmation = next_confirmation_number()
    if link is None:
        IPAMOwnershipLink.objects.create(
            server=server,
            family=family,
            source=IPAMOwnershipSource.LEASE,
            ip_address=ip,
            facts=facts,
            confirmation=confirmation,
            stale_mark=stale_mark,
        )
        return
    link.facts = facts
    link.confirmation = confirmation
    link.stale_mark = stale_mark
    link.save(update_fields=["facts", "confirmation", "stale_mark"])


@dataclass(frozen=True)
class _StaleLink:
    pk: int
    ip_pk: int
    vrf_id: int | None
    address: str


def _remove_stale_lease_links(
    server: Server, family: Family, cutoff: int, mode: StaleCleanupMode, report: SyncReport
) -> None:
    """Remove the links of a complete phase that no run confirmed since *cutoff*; the last link follows *mode*."""
    candidates = [
        _StaleLink(pk, ip_pk, vrf_id, _host(address))
        for pk, ip_pk, vrf_id, address in IPAMOwnershipLink.objects.filter(
            server=server, family=family, source=IPAMOwnershipSource.LEASE, confirmation__lt=cutoff
        )
        .exclude(stale_mark__isnull=False, confirmation__lte=F("stale_mark"))
        .values_list("pk", "ip_address", "ip_address__vrf", "ip_address__address")
    ]
    if not candidates:
        return
    referenced = dhcp_plugin.sys4_referenced_ip_ids()
    rows = _each_row(
        candidates,
        report,
        lambda stale: _remove_stale_link(stale, cutoff, mode, referenced),
        lambda stale: f"stale link of {stale.address} of Server {server.name}",
    )
    for stale, outcome in rows:
        if outcome == "removed":
            report.removed += 1
        elif outcome == "deprecated":
            report.deprecated += 1
        elif outcome == "conflict":
            report.conflicts.add(stale.address)


def _remove_stale_link(stale: _StaleLink, cutoff: int, mode: StaleCleanupMode, referenced: set[int]) -> str:
    """Decide one stale link under the identity lock and the row lock of its object."""
    _lock_identity(stale.vrf_id, stale.address)
    ip = IPAddress.objects.select_for_update().filter(pk=stale.ip_pk).first()
    link = IPAMOwnershipLink.objects.filter(pk=stale.pk).first()
    if ip is None or link is None or link.confirmation >= cutoff or _marked_and_unconfirmed(link):
        return "kept"
    if (ip.vrf_id, _host(ip.address)) != (stale.vrf_id, stale.address):
        raise _RowRefused(f"the address of IP address {ip.pk} changed while the cleanup read it")
    if not _is_owned_description(ip.description):
        IPAMOwnershipLink.objects.filter(ip_address=ip).delete()
        return "conflict"
    last = not IPAMOwnershipLink.objects.filter(ip_address=ip).exclude(pk=link.pk).exists()
    if not last or mode == "none" or ip.pk in referenced:
        link.delete()
        return "unlinked"
    if mode == "remove":
        ip.delete()
        return "removed"
    link.stale_mark = cutoff
    link.save(update_fields=["stale_mark"])
    if ip.status != "deprecated":
        ip.status = "deprecated"
        ip.save()
    return "deprecated"
