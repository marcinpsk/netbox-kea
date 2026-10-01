# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IPAM Reconciliation (ADR 0006): the one owner of IPAM Ownership links, stale cleanup and per-row savepoints.

``reconcile`` runs the lease and Reservation phases of one Server and family. It links every owned object that a
phase reports, and a complete phase removes its own stale links. Each row runs in its own transaction, or in a
savepoint when the caller holds a transaction, under a transaction-level advisory lock on the object identity.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar, cast

import requests
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from ipam.models import IPAddress

from .constants import Family, StaleCleanupMode
from .integrations import dhcp_plugin
from .ipam_marker import DESCRIPTION_MAX_LENGTH, parse_marker, render_marker, status_kind
from .kea import KeaException, lease_fields
from .models import IPAMOwnershipLink, IPAMOwnershipSource, next_confirmation_number
from .reservations import InSubnetReservationScope, Reservation
from .subnet_catalogue import CatalogueUnavailable
from .sync import (
    _apply_ip_fields,
    _apply_ip_mask,
    _get_stale_cleanup_mode,
    _ip_description,
    _record_hostname,
    _resolve_prefix_length,
    _sync_mac_address,
)

if TYPE_CHECKING:
    from .models import Server
    from .subnet_catalogue import CompleteCatalogueSnapshot

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

# The type check sees a TextChoices member as its (value, label) tuple; at runtime it is the str value.
LEASE = cast("str", IPAMOwnershipSource.LEASE)
RESERVATION = cast("str", IPAMOwnershipSource.RESERVATION)

# The status of an IP address from the sources of its live links.
_STATUSES: dict[frozenset[str], str] = {
    frozenset({LEASE, RESERVATION}): "active",
    frozenset({LEASE}): "dhcp",
    frozenset({RESERVATION}): "reserved",
}

# How many row failures of one reconcile call the log names; the error count stays exact.
_ROW_ERROR_LOG_LIMIT = 10


def _int4(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big", signed=True)


# The first key of the two-key advisory lock form: config_write locks use another class, NetBox's one-key locks none.
_LOCK_CLASS = _int4("netbox_kea.ipam_reconciliation")


@dataclass(frozen=True)
class LeasePhase:
    """The lease phase of one reconcile call: the Server's lease snapshot of the family."""

    max_leases: int | None
    subnet_prefix_lengths: dict[int, int]
    source: ClassVar[str] = LEASE


@dataclass(frozen=True)
class ReservationPhase:
    """The Reservation phase of one reconcile call: the Server's Reservation snapshot of the family.

    The snapshot reads the Reservations against *catalogue*. ``None`` means that the Subnet Catalogue is unavailable,
    so the phase has no snapshot.
    """

    catalogue: CompleteCatalogueSnapshot | None
    source: ClassVar[str] = RESERVATION


Phase = LeasePhase | ReservationPhase


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
    # The sources of the phases that are not complete: the snapshot is partial or failed, or a row failed.
    incomplete: set[str] = field(default_factory=set)
    # The valid records of the snapshots: the old stale cleanup reads them until #214.
    lease_records: list[dict[str, Any]] = field(default_factory=list)
    reservation_records: list[Reservation] = field(default_factory=list)
    # Global and addressless Reservations, which write no IPAM row: the job counts them as skipped.
    skipped_reservations: list[Reservation] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Return whether every phase of the call is complete."""
        return not self.incomplete

    def fail_snapshot(self, source: str, what: str, exc: BaseException) -> None:
        """Count one snapshot that could not be read, which makes its phase incomplete, and log it."""
        self.errors += 1
        self.incomplete.add(source)
        logger.warning("%s failed: %s", what, exc, exc_info=exc)

    def fail_row(self, source: str, what: str, exc: BaseException) -> None:
        """Count one failed row, which makes its phase incomplete, and log the first failures."""
        self.errors += 1
        self.incomplete.add(source)
        if self.errors <= _ROW_ERROR_LOG_LIMIT:
            logger.warning("IPAM reconciliation of %s failed: %s", what, exc, exc_info=exc)
        elif self.errors == _ROW_ERROR_LOG_LIMIT + 1:
            logger.warning("Further row failures of this reconciliation are not logged; see the error count.")


@dataclass(frozen=True)
class _Facts:
    hostname: str
    prefix_length: int

    def stored(self) -> dict[str, Any]:
        return {"hostname": self.hostname, "prefix_length": self.prefix_length}


@dataclass(frozen=True)
class _Report:
    """Everything one phase reports for one address.

    Without facts, the phase does not apply the report: it reported the address twice with different facts
    (``disagreement``), or only Global Reservations report the address.
    """

    address: str
    facts: _Facts | None
    # The (hardware address, hostname) pairs of the records, for the DCIM MAC address sync.
    mac_addresses: tuple[tuple[str, str], ...] = ()
    disagreement: bool = False


_Outcome = Literal["created", "updated", "unchanged", "conflict", "disagreement"]


class _RowRefused(Exception):
    """The row does not identify one object, so the run cannot decide it: two rows, or an address that moved."""


def reconcile(server: Server, family: Family, phases: Sequence[Phase]) -> SyncReport:
    """Run *phases* for one Server and family: link what they report, then remove the stale links of each complete one.

    The claims of all phases run before any link is removed. The last link of the Server to an IP address goes only
    when the call runs a complete lease phase and a complete Reservation phase. Writes go to main only: the job refuses
    to run in a branch.
    """
    if len({phase.source for phase in phases}) != len(phases):
        raise ValueError("reconcile takes at most one phase of each source")
    mode = _get_stale_cleanup_mode()
    report = SyncReport()
    cutoffs = {phase.source: _run_phase(server, family, phase, report) for phase in phases}
    complete = {source for source, cutoff in cutoffs.items() if cutoff is not None}
    last_links_go = complete >= {LEASE, RESERVATION}
    for source, cutoff in cutoffs.items():
        if cutoff is not None:
            _remove_stale_links(server, family, source, cutoff, mode, last_links_go, report)
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
    rows: Iterable[T], report: SyncReport, source: str, work: Callable[[T], R], name: Callable[[T], str]
) -> Iterator[tuple[T, R]]:
    """Run *work* for each row in its own transaction or savepoint; a database error fails only that row."""
    for row in rows:
        try:
            with transaction.atomic():
                outcome = work(row)
        except (DatabaseError, _RowRefused) as exc:
            report.fail_row(source, name(row), exc)
            continue
        yield row, outcome


def _run_phase(server: Server, family: Family, phase: Phase, report: SyncReport) -> int | None:
    """Link everything that *phase* reports. Return its cutoff number when the phase is complete, else None.

    The cutoff number comes before the snapshot request, so a claim that confirms a link after it keeps the link.
    """
    cutoff = next_confirmation_number()
    if isinstance(phase, LeasePhase):
        reports = _lease_reports(server, family, phase, report)
    else:
        reports = _reservation_reports(server, family, phase, report)
    rows = _each_row(
        reports.values(),
        report,
        phase.source,
        lambda row: _claim(server, family, phase.source, row),
        lambda row: f"{phase.source} {row.address} of Server {server.name}",
    )
    for row, outcome in rows:
        _count(report, row.address, outcome)
        if outcome != "conflict":
            for hw_address, hostname in row.mac_addresses:
                _sync_mac_address(hw_address, hostname)
    return None if phase.source in report.incomplete else cutoff


def _lease_reports(server: Server, family: Family, phase: LeasePhase, report: SyncReport) -> dict[str, _Report]:
    """Read the lease snapshot and group its valid records by canonical address."""
    try:
        client = server.get_client(version=family)
        collection = client.lease_get_all(version=family, max_leases=phase.max_leases)
    except (KeaException, requests.RequestException, ValueError, RuntimeError) as exc:
        report.fail_snapshot(LEASE, f"Server {server.name} (v{family}): the lease snapshot", exc)
        return {}
    logger.info("Server %s (v%s): fetched %d leases", server.name, family, len(collection.leases))
    if collection.truncated:
        logger.warning(
            "Server %s (v%s): lease fetch truncated at %d: increase sync_max_leases_per_server",
            server.name,
            family,
            phase.max_leases,
        )
        report.incomplete.add(LEASE)

    reports: dict[str, _Report] = {}
    for lease in collection.leases:
        fields = lease_fields(lease)
        # The snapshot already validated each address.
        address = str(ipaddress.ip_address(fields.address))
        try:
            hostname = _record_hostname(lease)
        except RuntimeError as exc:
            report.fail_row(LEASE, f"lease {address}", exc)
            continue
        report.lease_records.append(lease)
        facts = _Facts(hostname, _resolve_prefix_length(address, fields.subnet_id, phase.subnet_prefix_lengths))
        mac_addresses = ((fields.hw_address, hostname),) if fields.hw_address else ()
        _add_report(reports, _Report(address, facts, mac_addresses))
    return reports


def _reservation_reports(
    server: Server, family: Family, phase: ReservationPhase, report: SyncReport
) -> dict[str, _Report]:
    """Read the Reservation snapshot and group the allocation addresses of its Reservations by address.

    A Global Reservation reports its addresses without facts: the job writes no IPAM row for it (ADR 0002).
    """
    what = f"Server {server.name} (v{family}): the Reservation snapshot"
    if phase.catalogue is None:
        report.fail_snapshot(RESERVATION, what, CatalogueUnavailable("The Subnet Catalogue is unavailable."))
        return {}
    try:
        client = server.get_client(version=family)
        snapshot = client.reservation_snapshot(family, phase.catalogue)
    except KeaException as exc:
        if not exc.unsupported_command:
            report.fail_snapshot(RESERVATION, what, exc)
            return {}
        # Without host_cmds the Server has no Reservation to read. Nothing failed, but the phase is not complete.
        logger.warning(
            "Server %s (v%s): host_cmds is unavailable; the Reservation phase is skipped", server.name, family
        )
        report.incomplete.add(RESERVATION)
        return {}
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        report.fail_snapshot(RESERVATION, what, exc)
        return {}
    if snapshot.diagnostics or not snapshot.complete:
        # Each diagnostic is a Reservation or a page that the snapshot could not read.
        report.errors += len(snapshot.diagnostics)
        report.incomplete.add(RESERVATION)
        logger.warning("%s is incomplete: %d diagnostic(s)", what, len(snapshot.diagnostics))

    reports: dict[str, _Report] = {}
    for reservation in snapshot.records:
        facts: _Facts | None = None
        mac_addresses: tuple[tuple[str, str], ...] = ()
        if isinstance(reservation.scope, InSubnetReservationScope) and reservation.addresses:
            report.reservation_records.append(reservation)
            facts = _Facts(reservation.hostname, reservation.scope.subnet.network.prefixlen)
            if (hw_address := reservation.identity.hardware_address) is not None:
                mac_addresses = ((hw_address, reservation.hostname),)
        else:
            report.skipped_reservations.append(reservation)
        for address in reservation.addresses:
            _add_report(reports, _Report(str(address), facts, mac_addresses))
    logger.info(
        "Server %s (v%s): fetched %d Reservations; %d of them are Global or reserve no address",
        server.name,
        family,
        len(snapshot.records),
        len(report.skipped_reservations),
    )
    return reports


def _add_report(reports: dict[str, _Report], new: _Report) -> None:
    """Add one record's report of an address. Records with different facts make the report a disagreement."""
    earlier = reports.get(new.address)
    if earlier is None:
        reports[new.address] = new
        return
    mac_addresses = earlier.mac_addresses + new.mac_addresses
    if earlier.facts is None and not earlier.disagreement:
        reports[new.address] = replace(new, mac_addresses=mac_addresses)
    elif new.facts is None or new.facts == earlier.facts:
        reports[new.address] = replace(earlier, mac_addresses=mac_addresses)
    else:
        reports[new.address] = _Report(new.address, None, mac_addresses, disagreement=True)


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
    """Return the canonical host text of a NetBox address, the same text as a phase report's address."""
    return str(ipaddress.ip_address(str(address.ip)))


def _is_owned_description(description: str) -> bool:
    return parse_marker(description) is not None


def _claim(server: Server, family: Family, source: str, report: _Report) -> _Outcome:
    """Link one reported address under its identity lock, and apply the report when no owner disagrees."""
    vrf_id = server.sync_vrf_id
    _lock_identity(vrf_id, report.address)
    rows = list(
        IPAddress.objects.select_for_update().filter(vrf_id=vrf_id, address__net_host=report.address).order_by("pk")[:2]
    )
    if len(rows) > 1:
        raise _RowRefused(f"more than one IP address {report.address} in the sync VRF")
    facts = report.facts
    if not rows:
        if facts is None:
            # A phase that disagrees with itself, or a Global Reservation, creates no object.
            return "disagreement" if report.disagreement else "unchanged"
        status = _status({source})
        ip = IPAddress(
            address=f"{report.address}/{facts.prefix_length}",
            vrf_id=vrf_id,
            status=status,
            dns_name=facts.hostname,
            description=render_marker(status_kind(status)),
        )
        ip.save()
        _store_link(None, server, family, source, ip, facts.stored(), stale_mark=None)
        return "created"

    ip = rows[0]
    links = list(IPAMOwnershipLink.objects.filter(ip_address=ip))
    if not _is_owned_description(ip.description):
        # A blank or curated description: the object is not owned, and an operator edit released it.
        IPAMOwnershipLink.objects.filter(pk__in=[link.pk for link in links]).delete()
        # A Global Reservation does not want to change the object, so an object that it never linked is no conflict.
        return "unchanged" if facts is None and not report.disagreement and not links else "conflict"
    own = next((link for link in links if _is_own_link(link, server, family, source)), None)
    others = _drop_superseded_stale_links([link for link in links if link is not own])
    if report.disagreement:
        _store_link(own, server, family, source, ip, own.facts if own else None, stale_mark=_kept_mark(own))
        return "disagreement"
    if facts is None:
        # A Global Reservation links the object without facts and does not change it (ADR 0002).
        _store_link(own, server, family, source, ip, None, stale_mark=_kept_mark(own))
        return "unchanged"
    applied = _applied_facts(source, facts, others)
    if applied is None:
        _store_link(own, server, family, source, ip, facts.stored(), stale_mark=_kept_mark(own))
        return "disagreement"

    status = _status(_live_sources(others) | {source})
    if len(_ip_description(ip.description, status, claim=False)) > DESCRIPTION_MAX_LENGTH:
        # The new marker and the operator note do not fit: the object stays as it is, and the owner keeps its link.
        _store_link(own, server, family, source, ip, facts.stored(), stale_mark=_kept_mark(own))
        return "conflict"
    changed = _apply_ip_fields(ip, status=status, hostname=applied.hostname)
    changed = _apply_ip_mask(ip, report.address, applied.prefix_length) or changed
    if changed:
        ip.save()
    _store_link(own, server, family, source, ip, facts.stored(), stale_mark=None)
    return "updated" if changed else "unchanged"


def _applied_facts(source: str, facts: _Facts, others: Iterable[IPAMOwnershipLink]) -> _Facts | None:
    """Return the facts that a report of *source* applies to the object, or None when a live link disagrees.

    All owners compare the prefix length. Only owners of the same source compare the hostname, and an empty hostname
    makes no claim: a lease and a Reservation of one address often name the host differently. The hostname of a live
    Reservation link wins, so a lease does not change the DNS name then.
    """
    hostname = facts.hostname
    for link in others:
        if not _is_live(link):
            continue
        theirs = _Facts(**link.facts)
        if theirs.prefix_length != facts.prefix_length:
            return None
        if link.source == source:
            if facts.hostname and theirs.hostname and theirs.hostname != facts.hostname:
                return None
        elif link.source == RESERVATION and theirs.hostname:
            hostname = ""
    return replace(facts, hostname=hostname)


def _is_live(link: IPAMOwnershipLink) -> bool:
    """Return whether the link takes part in the fact comparison and the status: it has facts and no stale mark."""
    return link.facts is not None and link.stale_mark is None


def _live_sources(links: Iterable[IPAMOwnershipLink]) -> set[str]:
    return {link.source for link in links if _is_live(link)}


def _status(sources: Collection[str]) -> str:
    """Return the IP address status for the sources of its live links; the caller passes at least one."""
    return _STATUSES[frozenset(sources)]


def _restatus(ip: IPAddress, links: Iterable[IPAMOwnershipLink]) -> _Outcome:
    """Give an owned object the status of its live *links*. Without a live link, the object keeps its status.

    When the new marker and the operator note do not fit, the object stays as it is and the result is a conflict.
    """
    sources = _live_sources(links)
    if not sources:
        return "unchanged"
    status = _status(sources)
    if len(_ip_description(ip.description, status, claim=False)) > DESCRIPTION_MAX_LENGTH:
        return "conflict"
    if not _apply_ip_fields(ip, status=status, hostname=""):
        return "unchanged"
    ip.save()
    return "updated"


def _is_own_link(link: IPAMOwnershipLink, server: Server, family: Family, source: str) -> bool:
    return link.server_id == server.pk and link.family == family and link.source == source


def _kept_mark(link: IPAMOwnershipLink | None) -> int | None:
    return link.stale_mark if link is not None else None


def _marked_and_unconfirmed(link: IPAMOwnershipLink) -> bool:
    """Return whether the link is marked stale and its owner has not confirmed it since the mark."""
    return link.stale_mark is not None and link.confirmation <= link.stale_mark


def _drop_superseded_stale_links(others: list[IPAMOwnershipLink]) -> list[IPAMOwnershipLink]:
    """Another owner links the object, so a stale link goes unless its own owner confirmed it after the mark.

    Return the links that stay.
    """
    superseded = [link for link in others if _marked_and_unconfirmed(link)]
    if superseded:
        IPAMOwnershipLink.objects.filter(pk__in=[link.pk for link in superseded]).delete()
    return [link for link in others if link not in superseded]


def _store_link(
    link: IPAMOwnershipLink | None,
    server: Server,
    family: Family,
    source: str,
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
            source=source,
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


def _remove_stale_links(
    server: Server,
    family: Family,
    source: str,
    cutoff: int,
    mode: StaleCleanupMode,
    last_links_go: bool,
    report: SyncReport,
) -> None:
    """Remove the links of a complete phase that no run confirmed since *cutoff*.

    The last link of the Server to an object goes only when *last_links_go*. The last link of an object follows *mode*.
    """
    candidates = [
        _StaleLink(pk, ip_pk, vrf_id, _host(address))
        for pk, ip_pk, vrf_id, address in IPAMOwnershipLink.objects.filter(
            server=server, family=family, source=source, confirmation__lt=cutoff
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
        source,
        lambda stale: _remove_stale_link(stale, cutoff, mode, last_links_go, referenced),
        lambda stale: f"stale {source} link of {stale.address} of Server {server.name}",
    )
    for stale, outcome in rows:
        if outcome == "removed":
            report.removed += 1
        elif outcome == "deprecated":
            report.deprecated += 1
        elif outcome == "updated":
            report.updated += 1
        elif outcome == "conflict":
            report.conflicts.add(stale.address)


def _remove_stale_link(
    stale: _StaleLink, cutoff: int, mode: StaleCleanupMode, last_links_go: bool, referenced: set[int]
) -> str:
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
    others = list(IPAMOwnershipLink.objects.filter(ip_address=ip).exclude(pk=link.pk))
    if not last_links_go and not any(other.server_id == link.server_id for other in others):
        # The last link of its Server: a later call with a complete lease and Reservation phase decides.
        return "kept"
    if others:
        outcome = _restatus(ip, others)
        if outcome != "conflict":
            # On a conflict the link stays, so the status still matches the links and the next run tries again.
            link.delete()
        return outcome
    if mode == "none" or ip.pk in referenced:
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
