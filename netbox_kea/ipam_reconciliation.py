# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IPAM Reconciliation (ADR 0006): the one owner of IPAM Ownership links, stale cleanup and per-row savepoints.

``reconcile`` runs the lease, Reservation, Subnet and Pool phases of one Server and family. It links every owned object that a
phase reports, and a complete phase removes its own stale links. Each row runs in its own transaction, or in a
savepoint when the caller holds a transaction, under a transaction-level advisory lock on the object identity.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar, cast

from django.db import DatabaseError, connection, transaction
from ipam.models import IPAddress, IPRange, Prefix
from netaddr import IPNetwork

from . import subnet_catalogue
from .constants import IP_RANGE_MAX_SIZE, Family, IPNetworkValue, StaleCleanupMode
from .integrations import dhcp_plugin
from .ipam_marker import Marker, MarkerKind, parse_marker, render_marker, rewrite_marker, status_kind
from .kea import KeaException, lease_fields
from .models import IPAMOwnershipLink, IPAMOwnershipSource, next_confirmation_number
from .pools import Pool
from .reservations import TRAVERSAL_DIAGNOSTIC_CODES, InSubnetReservationScope, Reservation, ReservationSnapshot
from .subnet_catalogue import CatalogueUnavailable
from .sync import (
    DuplicateNetBoxRowsError,
    _apply_ip_fields,
    _apply_ip_mask,
    _get_stale_cleanup_mode,
    _ip_description,
    _record_hostname,
    _single_match,
    sync_mac_address,
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
    """The lease phase of one reconcile call: the Server's lease snapshot of the family.

    A mask mapping is the available Kea authority, even when empty. ``None`` selects the job's unavailable-catalogue
    fallback to Prefixes in the Server's VRF, then host masks.
    """

    max_leases: int | None
    subnet_prefix_lengths: Mapping[int, int] | None
    source: ClassVar[str] = LEASE


@dataclass(frozen=True)
class ReservationPhase:
    """The Reservation phase of one reconcile call: the Server's Reservation snapshot of the family.

    The snapshot reads the Reservations against *catalogue*. ``None`` means that the Subnet Catalogue is unavailable,
    so the phase has no snapshot.
    """

    catalogue: CompleteCatalogueSnapshot | None
    source: ClassVar[str] = RESERVATION


@dataclass(frozen=True)
class CatalogueObservation:
    """A live catalogue paired with the confirmation cutoff taken before its request."""

    catalogue: CompleteCatalogueSnapshot | None
    cutoff: int


def read_catalogue(server: Server, family: Family) -> CatalogueObservation:
    """Read the family's catalogue after taking the cutoff for its Subnet and Pool phases."""
    cutoff = next_confirmation_number()
    try:
        catalogue = subnet_catalogue.for_synchronization(server, family)
    except CatalogueUnavailable as exc:
        logger.warning("Server %s (v%s): Subnet Catalogue unavailable: %s", server.name, family, exc)
        catalogue = None
    return CatalogueObservation(catalogue, cutoff)


@dataclass(frozen=True)
class SubnetPhase:
    """The Subnets of one live catalogue observation."""

    observation: CatalogueObservation
    source: ClassVar[str] = "subnet"


@dataclass(frozen=True)
class PoolPhase:
    """The allocation Pools of one live catalogue observation."""

    observation: CatalogueObservation
    source: ClassVar[str] = "pool"


@dataclass(frozen=True)
class ReservationObservation:
    """A Reservation snapshot and the confirmation cutoff taken before its read."""

    snapshot: ReservationSnapshot
    cutoff: int


@dataclass(frozen=True)
class DelegatedPrefixPhase:
    """Successfully imported Reservations with their original read cutoff."""

    records: Sequence[Reservation]
    cutoff: int
    complete: bool
    source: ClassVar[str] = "delegated-prefix"


Phase = LeasePhase | ReservationPhase | SubnetPhase | PoolPhase | DelegatedPrefixPhase


@dataclass
class SyncReport:
    """What one reconcile call did. Conflicts and owner disagreements identify canonical IPAM objects."""

    created: int = 0
    updated: int = 0
    # IP address ownership links removed by stale cleanup, including the last link of a removed address.
    cleaned: int = 0
    removed: int = 0
    deprecated: int = 0
    errors: int = 0
    prefix_errors: int = 0
    duplicates: list[DuplicateNetBoxRowsError] = field(default_factory=list)
    conflicts: set[str] = field(default_factory=set)
    disagreements: set[str] = field(default_factory=set)
    # The sources of the phases that are not complete: the snapshot is partial or failed, or a row failed.
    incomplete: set[str] = field(default_factory=set)
    # The valid records of the snapshots: the old stale cleanup reads them until #214.
    lease_records: list[dict[str, Any]] = field(default_factory=list)
    reservation_records: list[Reservation] = field(default_factory=list)
    quarantined_reservations: int = 0
    reservation_traversal_truncated: bool = False
    # Global and addressless Reservations, which write no IPAM row: the job counts them as skipped.
    skipped_reservations: list[Reservation] = field(default_factory=list)
    prefixes: dict[str, PrefixClaim] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Return whether every phase of the call is complete."""
        return not self.incomplete

    def fail_snapshot(self, source: str, what: str, exc: BaseException) -> None:
        """Count one snapshot that could not be read, which makes its phase incomplete, and log it."""
        if source in {"subnet", "pool"}:
            self.prefix_errors += 1
        else:
            self.errors += 1
        self.incomplete.add(source)
        logger.warning("%s failed: %s", what, exc, exc_info=exc)

    def fail_row(self, source: str, what: str, exc: BaseException) -> None:
        """Count one failed row, which makes its phase incomplete, and log the first failures."""
        if source in {"subnet", "pool"}:
            self.prefix_errors += 1
        else:
            self.errors += 1
        self.incomplete.add(source)
        if isinstance(exc, DuplicateNetBoxRowsError):
            self.duplicates.append(exc)
        if self.errors + self.prefix_errors <= _ROW_ERROR_LOG_LIMIT:
            logger.warning("IPAM reconciliation of %s failed: %s", what, exc, exc_info=exc)
        elif self.errors + self.prefix_errors == _ROW_ERROR_LOG_LIMIT + 1:
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


@dataclass(frozen=True)
class AddressClaim:
    """The outcome and locked-row snapshot for one canonical address."""

    address: str
    outcome: _Outcome | Literal["error", "not-applicable"]
    ip: IPAddress | None = None

    @property
    def synchronized(self) -> bool:
        """Return whether the run applied this address's report."""
        return self.outcome in {"created", "updated", "unchanged"} and self.ip is not None


@dataclass(frozen=True)
class SubnetClaim:
    """A canonical Subnet network to claim without cleanup."""

    network: IPNetworkValue


@dataclass(frozen=True)
class PoolClaim:
    """An allocation Pool and the Subnet that supplies its address mask."""

    pool: Pool
    network: IPNetworkValue


@dataclass(frozen=True)
class PrefixClaim:
    """The ownership outcome and existing Prefix for one network."""

    outcome: _Outcome | Literal["error"]
    prefix: Prefix | None = None


@dataclass(frozen=True)
class RangeClaim:
    """The ownership outcome and existing IP Range for one allocation Pool."""

    outcome: _Outcome | Literal["error"]
    ip_range: IPRange | None = None


@dataclass(frozen=True)
class ClaimResult:
    """Typed outcomes from one source claim, including identities whose row failed."""

    addresses: dict[str, AddressClaim] = field(default_factory=dict)
    prefixes: dict[str, PrefixClaim] = field(default_factory=dict)
    ranges: dict[str, RangeClaim] = field(default_factory=dict)

    @property
    def primary(self) -> IPAddress | None:
        """Return the first successfully synchronized row for the aggregate badge."""
        return next((result.ip for result in self.addresses.values() if result.synchronized), None)

    @property
    def synchronized_addresses(self) -> frozenset[str]:
        """Return only the addresses whose reports were applied."""
        return frozenset(address for address, result in self.addresses.items() if result.synchronized)


def claim(
    server: Server,
    family: Family,
    records: Sequence[dict[str, Any]] | Sequence[Reservation] | Sequence[SubnetClaim] | Sequence[PoolClaim],
    force: bool,
) -> ClaimResult:
    """Claim one source's records in the Server's VRF, with per-address outcomes and no stale cleanup.

    A call contains one source: leases, Reservations, Subnets or Pools. All records are validated and grouped
    before any write. Lease masks come from the live Kea Subnet Catalogue. Reservations carry their verified
    Subnet mask. Network records carry their own canonical network and Pool bounds.
    """
    if family not in (4, 6) or isinstance(family, bool):
        raise ValueError("The address family must be 4 or 6")
    if not records:
        return ClaimResult({})
    if isinstance(records[0], (SubnetClaim, PoolClaim)):
        return _claim_network_records(server, family, records, force=force)
    if any(isinstance(record, (SubnetClaim, PoolClaim)) for record in records):
        raise ValueError("A claim takes one homogeneous source")
    address_records = cast("Sequence[dict[str, Any] | Reservation]", records)
    lease_records = isinstance(records[0], dict)
    if any(isinstance(record, dict) != lease_records for record in records):
        raise ValueError("A claim takes one source: leases or Reservations, not both")
    source = LEASE if lease_records else RESERVATION
    subnet_prefix_lengths = {}
    if lease_records:
        catalogue = subnet_catalogue.for_synchronization(server, family)
        subnet_prefix_lengths = {subnet.subnet_id: subnet.network.prefixlen for subnet in catalogue.subnets}
    reports = _claim_reports(server, family, address_records, subnet_prefix_lengths)
    outcomes = {address: AddressClaim(address, "error") for address in reports}

    def apply(row: _Report) -> AddressClaim:
        outcome = _claim(server, family, source, row, force=force)
        ip = IPAddress.objects.filter(vrf_id=server.sync_vrf_id, address__net_host=row.address).first()
        if outcome != "conflict":
            for hardware, hostname in row.mac_addresses:
                if sync_mac_address(hardware, hostname) is None and source == RESERVATION:
                    raise _RowRefused("The required hardware address could not be resolved")
        if row.facts is None and not row.disagreement and outcome == "unchanged":
            if ip is not None and not _is_owned_description(ip.description):
                return AddressClaim(row.address, "conflict", ip)
            return AddressClaim(row.address, "not-applicable", ip)
        return AddressClaim(row.address, outcome, ip)

    report = SyncReport()
    for row, result in _each_row(reports.values(), report, source, apply, lambda row: row.address):
        outcomes[row.address] = result
    return ClaimResult(outcomes)


def _claim_reports(
    server: Server,
    family: Family,
    records: Sequence[dict[str, Any] | Reservation],
    subnet_prefix_lengths: Mapping[int, int],
) -> dict[str, _Report]:
    """Validate and aggregate one call before acquiring locks or changing objects."""
    reports: dict[str, _Report] = {}
    for record in records:
        if isinstance(record, dict):
            _add_report(reports, _lease_report(server, family, record, subnet_prefix_lengths))
        else:
            if record.family != family:
                raise ValueError("The Reservation does not match the claim family")
            facts = None
            mac_addresses: tuple[tuple[str, str], ...] = ()
            if isinstance(record.scope, InSubnetReservationScope):
                facts = _Facts(record.hostname, record.scope.subnet.network.prefixlen)
                if (hardware := record.identity.hardware_address) is not None:
                    mac_addresses = ((hardware, record.hostname),)
            for address in record.addresses:
                _add_report(reports, _Report(str(address), facts, mac_addresses))
    return reports


def _lease_report(
    server: Server, family: Family, lease: dict[str, Any], subnet_prefix_lengths: Mapping[int, int] | None
) -> _Report:
    """Resolve lease facts from Kea, or the job's explicit unavailable-catalogue fallback."""
    fields = lease_fields(lease)
    address = ipaddress.ip_address(fields.address)
    if address.version != family:
        raise ValueError("The lease address does not match the claim family")
    subnet_id = fields.subnet_id
    if (
        isinstance(subnet_id, bool)
        or not isinstance(subnet_id, int)
        or not subnet_catalogue.MIN_SUBNET_ID <= subnet_id <= subnet_catalogue.MAX_SUBNET_ID
    ):
        raise ValueError("The lease Subnet ID must be an integer in the Kea Subnet ID range")
    hostname = _record_hostname(lease)
    if subnet_prefix_lengths is not None:
        if subnet_id not in subnet_prefix_lengths:
            raise ValueError("The lease Subnet ID is absent from the Subnet Catalogue")
        prefix_length = subnet_prefix_lengths[subnet_id]
    else:
        prefix = (
            Prefix.objects.filter(vrf_id=server.sync_vrf_id, prefix__net_contains_or_equals=str(address))
            .order_by("-prefix__net_mask_length")
            .first()
        )
        prefix_length = prefix.prefix.prefixlen if prefix is not None else address.max_prefixlen
    mac_addresses = ((fields.hw_address, hostname),) if fields.hw_address else ()
    return _Report(str(address), _Facts(hostname, prefix_length), mac_addresses)


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
        if cutoff is None:
            continue
        if source in {LEASE, RESERVATION}:
            _remove_stale_links(server, family, source, cutoff, mode, last_links_go, report)
        else:
            _remove_stale_network_links(server, family, source, cutoff, report)
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
        except (DatabaseError, _RowRefused, DuplicateNetBoxRowsError) as exc:
            report.fail_row(source, name(row), exc)
            continue
        yield row, outcome


def _run_phase(server: Server, family: Family, phase: Phase, report: SyncReport) -> int | None:
    """Link everything that *phase* reports. Return its cutoff number when the phase is complete, else None.

    The cutoff number comes before the snapshot request, so a claim that confirms a link after it keeps the link.
    """
    if isinstance(phase, DelegatedPrefixPhase):
        return _run_delegated_prefix_phase(server, family, phase, report)
    if isinstance(phase, (SubnetPhase, PoolPhase)):
        return _run_network_phase(server, family, phase, report)
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
                sync_mac_address(hw_address, hostname)
    return None if phase.source in report.incomplete else cutoff


def _lease_reports(server: Server, family: Family, phase: LeasePhase, report: SyncReport) -> dict[str, _Report]:
    """Read the lease snapshot and group its valid records by canonical address."""
    try:
        client = server.get_client(version=family)
        collection = client.lease_get_all(version=family, max_leases=phase.max_leases)
    # requests errors are OSError subclasses; a missing TLS file raises a plain OSError.
    except (KeaException, OSError, ValueError, RuntimeError) as exc:
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
            row = _lease_report(server, family, lease, phase.subnet_prefix_lengths)
        except (ValueError, RuntimeError) as exc:
            report.fail_row(LEASE, f"lease {address}", exc)
            continue
        report.lease_records.append(lease)
        _add_report(reports, row)
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
    except (OSError, ValueError, RuntimeError) as exc:
        report.fail_snapshot(RESERVATION, what, exc)
        return {}
    report.quarantined_reservations = sum(
        diagnostic.code not in TRAVERSAL_DIAGNOSTIC_CODES for diagnostic in snapshot.diagnostics
    )
    report.reservation_traversal_truncated = snapshot.traversal_truncated
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
    """Add one record's report of an address. Records whose facts do not merge make the report a disagreement."""
    earlier = reports.get(new.address)
    if earlier is None:
        reports[new.address] = new
        return
    mac_addresses = earlier.mac_addresses + new.mac_addresses
    if earlier.facts is None and not earlier.disagreement:
        reports[new.address] = replace(new, mac_addresses=mac_addresses)
    elif new.facts is None:
        reports[new.address] = replace(earlier, mac_addresses=mac_addresses)
    elif earlier.facts is not None and (merged := _merge(earlier.facts, new.facts)) is not None:
        reports[new.address] = replace(earlier, facts=merged, mac_addresses=mac_addresses)
    else:
        reports[new.address] = _Report(new.address, None, mac_addresses, disagreement=True)


def _merge(facts: _Facts, other: _Facts) -> _Facts | None:
    """Return the facts of two reports of one source, or None when they disagree.

    The prefix lengths must be equal. An empty hostname makes no claim, so the result takes the non-empty one.
    """
    if facts.prefix_length != other.prefix_length:
        return None
    if facts.hostname and other.hostname and facts.hostname != other.hostname:
        return None
    return replace(facts, hostname=facts.hostname or other.hostname)


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


def _claim(server: Server, family: Family, source: str, report: _Report, *, force: bool = False) -> _Outcome:
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
        if report.disagreement:
            return "disagreement"
        if not force or facts is None:
            # A Global Reservation does not change an object that it never linked.
            return "unchanged" if facts is None and not links else "conflict"
        links = []
    return _apply_claim(server, family, source, report, ip, links, force=force)


def _apply_claim(
    server: Server,
    family: Family,
    source: str,
    report: _Report,
    ip: IPAddress,
    links: list[IPAMOwnershipLink],
    *,
    force: bool,
) -> _Outcome:
    """Compare owners before applying facts or an explicit takeover to the locked row."""
    facts = report.facts
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
    description = _ip_description(ip.description, status, claim=force)
    if description is None:
        # The new marker and the operator note do not fit: the object stays as it is, and the owner keeps its link.
        _store_link(own, server, family, source, ip, facts.stored(), stale_mark=_kept_mark(own))
        return "conflict"
    changed = _apply_ip_fields(ip, status=status, hostname=applied.hostname, description=description)
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
            if _merge(facts, theirs) is None:
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


def _restatus(ip: IPAddress, links: Sequence[IPAMOwnershipLink]) -> _Outcome:
    """Give an owned object the status and the hostname of its live *links*. Without a live link, it stays as it is.

    When the new marker and the operator note do not fit, the object stays as it is and the result is a conflict.
    """
    sources = _live_sources(links)
    if not sources:
        return "unchanged"
    status = _status(sources)
    description = _ip_description(ip.description, status, claim=False)
    if description is None:
        return "conflict"
    if not _apply_ip_fields(ip, status=status, hostname=_implied_hostname(links), description=description):
        return "unchanged"
    ip.save()
    return "updated"


def _implied_hostname(links: Sequence[IPAMOwnershipLink]) -> str:
    """Return the hostname of the live *links* under the rule of :func:`_applied_facts`: a Reservation hostname wins.

    An empty result changes no DNS name: the live links of the winning source name no host, or different hosts.
    """
    for source in (RESERVATION, LEASE):
        names = {link.facts["hostname"] for link in links if _is_live(link) and link.source == source} - {""}
        if names:
            return names.pop() if len(names) == 1 else ""
    return ""


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
    ip: IPAddress | Prefix | IPRange,
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
            **{_object_field(ip): ip},
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
        ).values_list("pk", "ip_address", "ip_address__vrf", "ip_address__address")
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
        if outcome in {"removed", "unlinked", "updated", "unchanged"}:
            report.cleaned += 1
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
    if ip is None or link is None or link.confirmation >= cutoff:
        return "kept"
    if (ip.vrf_id, _host(ip.address)) != (stale.vrf_id, stale.address):
        raise _RowRefused(f"the address of IP address {ip.pk} changed while the cleanup read it")
    if not _is_owned_description(ip.description):
        IPAMOwnershipLink.objects.filter(ip_address=ip).delete()
        return "conflict"
    if _marked_and_unconfirmed(link):
        return "kept"
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


@dataclass(frozen=True)
class _NetworkReport:
    address: str
    prefix_length: int
    start: str = ""
    end: str = ""
    disagreement: bool = False

    def facts(self) -> dict[str, Any]:
        return {"prefix_length": self.prefix_length}


def _object_field(obj: IPAddress | Prefix | IPRange) -> str:
    if isinstance(obj, Prefix):
        return "prefix"
    if isinstance(obj, IPRange):
        return "ip_range"
    return "ip_address"


def _lock_network(vrf_id: int | None, source: str, address: str) -> None:
    kind = "ip-range" if source == "pool" else "prefix"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_CLASS, _int4(f"{kind} {vrf_id} {address}")])


def _run_network_phase(
    server: Server, family: Family, phase: SubnetPhase | PoolPhase, report: SyncReport
) -> int | None:
    catalogue = phase.observation.catalogue
    if catalogue is None:
        if not report.incomplete.intersection({"subnet", "pool"}):
            report.fail_snapshot(phase.source, "the Subnet Catalogue", CatalogueUnavailable("No complete catalogue."))
        report.incomplete.add(phase.source)
        return None
    reports: dict[str, _NetworkReport] = {}
    for subnet in catalogue.subnets:
        if isinstance(phase, SubnetPhase):
            reports[subnet.cidr] = _NetworkReport(subnet.cidr, subnet.network.prefixlen)
        else:
            if subnet.configuration is None:
                raise ValueError("A complete catalogue must include every Subnet configuration.")
            for pool in subnet.configuration.pools:
                if int(pool.end) - int(pool.start) + 1 > IP_RANGE_MAX_SIZE:
                    continue
                address = f"{pool.start} - {pool.end}"
                row = _NetworkReport(address, subnet.network.prefixlen, str(pool.start), str(pool.end))
                earlier = reports.get(address)
                if earlier is not None and (earlier.disagreement or earlier.prefix_length != row.prefix_length):
                    row = replace(earlier, disagreement=True)
                reports[address] = row
    for row, outcome in _each_row(
        reports.values(),
        report,
        phase.source,
        lambda row: _claim_network(server, family, phase.source, row),
        lambda row: row.address,
    ):
        _count(report, row.address, outcome)
    return None if phase.source in report.incomplete else phase.observation.cutoff


def _claim_network(
    server: Server, family: Family, source: str, row: _NetworkReport, *, force: bool = False
) -> _Outcome:
    _lock_network(server.sync_vrf_id, source, row.address)
    if source != "pool":
        query = Prefix.objects.select_for_update().filter(vrf_id=server.sync_vrf_id, prefix=row.address)
        filters = {"prefix": row.address, "vrf_id": server.sync_vrf_id or "null"}
        fields: dict[str, Any] = {"prefix": row.address}
    else:
        query = IPRange.objects.select_for_update().filter(
            vrf_id=server.sync_vrf_id, start_address__net_host=row.start, end_address__net_host=row.end
        )
        filters = {"start_address": row.start, "end_address": row.end, "vrf_id": server.sync_vrf_id or "null"}
        fields = {
            "start_address": IPNetwork(f"{row.start}/{row.prefix_length}"),
            "end_address": IPNetwork(f"{row.end}/{row.prefix_length}"),
        }
    obj = _single_match(query, f"{source} {row.address}", filters)
    kind: MarkerKind = "pool" if source == "pool" else ("subnet" if source == "subnet" else "delegated prefix")
    if obj is None:
        if row.disagreement:
            return "disagreement"
        obj = query.model.objects.create(
            **fields, vrf_id=server.sync_vrf_id, status="active", description=render_marker(kind)
        )
        _store_link(None, server, family, source, obj, row.facts(), stale_mark=None)
        return "created"
    links_query = IPAMOwnershipLink.objects.filter(**{_object_field(obj): obj})
    marker = parse_marker(obj.description)
    if marker is None:
        links_query.delete()
        if not force:
            return "conflict"
        marker = Marker(kind, f" {obj.description}" if obj.description else "", legacy=False)
    links = list(links_query)
    own = next((link for link in links if _is_own_link(link, server, family, source)), None)
    others = _drop_superseded_stale_links([link for link in links if link is not own])
    if row.disagreement:
        _store_link(own, server, family, source, obj, own.facts if own else None, stale_mark=_kept_mark(own))
        return "disagreement"
    if any(_is_live(link) and link.facts["prefix_length"] != row.prefix_length for link in others):
        _store_link(own, server, family, source, obj, row.facts(), stale_mark=_kept_mark(own))
        return "disagreement"
    description = rewrite_marker(marker, kind)
    if description is None:
        _store_link(own, server, family, source, obj, row.facts(), stale_mark=_kept_mark(own))
        return "conflict"
    fields.update(status="active", description=description)
    changed = any(str(getattr(obj, name)) != str(value) for name, value in fields.items())
    if changed:
        for name, value in fields.items():
            setattr(obj, name, value)
        obj.save()
    _store_link(own, server, family, source, obj, row.facts(), stale_mark=None)
    return "updated" if changed else "unchanged"


def _remove_stale_network_links(server: Server, family: Family, source: str, cutoff: int, report: SyncReport) -> None:
    """Drop the stale links of a complete Subnet or Pool phase. These objects are never removed."""
    field_name = "ip_range" if source == "pool" else "prefix"
    links = IPAMOwnershipLink.objects.filter(server=server, family=family, source=source, confirmation__lt=cutoff)
    # A marked link still needs the release check: an operator may have removed its object's marker.
    candidates = list(links.select_related(field_name))
    for link, outcome in _each_row(
        candidates,
        report,
        source,
        lambda link: _remove_stale_network_link(link, field_name, cutoff),
        lambda link: f"stale {source} link {link.pk}",
    ):
        if outcome == "deprecated":
            report.deprecated += 1
        elif outcome == "updated":
            report.updated += 1
        elif outcome == "conflict":
            obj = getattr(link, field_name)
            report.conflicts.add(
                str(obj.prefix) if field_name == "prefix" else f"{_host(obj.start_address)} - {_host(obj.end_address)}"
            )


def _remove_stale_network_link(candidate: IPAMOwnershipLink, field_name: str, cutoff: int) -> str:
    obj = getattr(candidate, field_name)
    address = str(obj.prefix) if field_name == "prefix" else f"{_host(obj.start_address)} - {_host(obj.end_address)}"
    _lock_network(obj.vrf_id, candidate.source, address)
    locked = type(obj).objects.select_for_update().filter(pk=obj.pk).first()
    link = IPAMOwnershipLink.objects.filter(pk=candidate.pk).first()
    if locked is None or link is None or link.confirmation >= cutoff:
        return "kept"
    current_address = (
        str(locked.prefix) if field_name == "prefix" else f"{_host(locked.start_address)} - {_host(locked.end_address)}"
    )
    if (locked.vrf_id, current_address) != (obj.vrf_id, address):
        raise _RowRefused(f"the identity of {field_name} {obj.pk} changed during cleanup")
    links = IPAMOwnershipLink.objects.filter(**{field_name: locked})
    marker = parse_marker(locked.description)
    if marker is None:
        links.delete()
        return "conflict"
    if _marked_and_unconfirmed(link):
        return "kept"
    others = list(links.exclude(pk=link.pk))
    if others:
        changed = False
        if _live_sources(others):
            sources = _live_sources(others)
            kind: MarkerKind = (
                "pool" if field_name == "ip_range" else ("subnet" if "subnet" in sources else "delegated prefix")
            )
            description = rewrite_marker(marker, kind)
            if description is None:
                return "conflict"
            changed = locked.status != "active" or locked.description != description
            if changed:
                locked.status = "active"
                locked.description = description
                locked.save()
        link.delete()
        return "updated" if changed else "unlinked"
    referenced = (
        dhcp_plugin.sys4_referenced_prefix_ids()
        if field_name == "prefix"
        else dhcp_plugin.sys4_referenced_iprange_ids()
    )
    if not link.server.sync_deprecate_prefixes_and_ranges or locked.pk in referenced:
        link.delete()
        return "unlinked"
    link.stale_mark = cutoff
    link.save(update_fields=["stale_mark"])
    if locked.status != "deprecated":
        locked.status = "deprecated"
        locked.save()
    return "deprecated"


def _network_result(server: Server, source: str, row: _NetworkReport, outcome: _Outcome) -> PrefixClaim | RangeClaim:
    """Return the object while its identity lock is held by the current savepoint."""
    if source == "pool":
        obj = IPRange.objects.filter(
            vrf_id=server.sync_vrf_id, start_address__net_host=row.start, end_address__net_host=row.end
        ).first()
        return RangeClaim(outcome, obj)
    obj = Prefix.objects.filter(vrf_id=server.sync_vrf_id, prefix=row.address).first()
    return PrefixClaim(outcome, obj)


def _claim_network_records(
    server: Server,
    family: Family,
    records: Sequence[dict[str, Any] | Reservation | SubnetClaim | PoolClaim],
    *,
    force: bool,
) -> ClaimResult:
    """Validate and group one typed network source before any ownership write."""
    source = "subnet" if isinstance(records[0], SubnetClaim) else "pool"
    expected = SubnetClaim if source == "subnet" else PoolClaim
    reports: dict[str, _NetworkReport] = {}
    for record in records:
        if not isinstance(record, (SubnetClaim, PoolClaim)) or not isinstance(record, expected):
            raise ValueError("A claim takes one homogeneous source")
        if record.network.version != family:
            raise ValueError("The network does not match the claim family")
        if isinstance(record, SubnetClaim):
            row = _NetworkReport(str(record.network), record.network.prefixlen)
        else:
            pool = record.pool
            if pool.start not in record.network or pool.end not in record.network or int(pool.end) < int(pool.start):
                raise ValueError("The Pool must be contained by its Subnet")
            if int(pool.end) - int(pool.start) + 1 > IP_RANGE_MAX_SIZE:
                continue
            row = _NetworkReport(f"{pool.start} - {pool.end}", record.network.prefixlen, str(pool.start), str(pool.end))
        earlier = reports.get(row.address)
        if earlier is not None and (earlier.disagreement or earlier.prefix_length != row.prefix_length):
            row = replace(earlier, disagreement=True)
        reports[row.address] = row
    return _claim_network_reports(server, family, source, reports, force=force)


def _claim_network_reports(
    server: Server, family: Family, source: str, reports: Mapping[str, _NetworkReport], *, force: bool = False
) -> ClaimResult:
    """Use the same network ownership policy as complete reconciliation phases."""
    result = ClaimResult()
    for address in reports:
        if source == "pool":
            result.ranges[address] = RangeClaim("error")
        else:
            result.prefixes[address] = PrefixClaim("error")

    def apply(row: _NetworkReport) -> PrefixClaim | RangeClaim:
        outcome = _claim_network(server, family, source, row, force=force)
        return _network_result(server, source, row, outcome)

    for row, outcome in _each_row(reports.values(), SyncReport(), source, apply, lambda row: row.address):
        if isinstance(outcome, PrefixClaim):
            result.prefixes[row.address] = outcome
        else:
            result.ranges[row.address] = outcome
    return result


def _run_delegated_prefix_phase(
    server: Server, family: Family, phase: DelegatedPrefixPhase, report: SyncReport
) -> int | None:
    """Claim successfully imported delegated Prefixes before optional stale cleanup."""
    reports: dict[str, _NetworkReport] = {}
    for reservation in phase.records:
        if reservation.family != family:
            raise ValueError("The Reservation does not match the phase family")
        for prefix in reservation.delegated_prefixes:
            reports[str(prefix)] = _NetworkReport(str(prefix), prefix.prefixlen)
    results = _claim_network_reports(server, family, phase.source, reports)
    report.prefixes.update(results.prefixes)
    for address, result in results.prefixes.items():
        if result.outcome == "error":
            report.fail_row(phase.source, address, _RowRefused("Delegated Prefix claim failed"))
        else:
            _count(report, address, result.outcome)
    if not phase.complete:
        report.incomplete.add(phase.source)
    return None if phase.source in report.incomplete else phase.cutoff
