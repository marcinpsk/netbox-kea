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
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar, cast

from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from ipam.models import IPAddress, IPRange, Prefix
from netaddr import IPNetwork

from . import subnet_catalogue
from .constants import IP_RANGE_MAX_SIZE, Family, IPNetworkValue, StaleCleanupMode
from .integrations import dhcp_plugin
from .ipam_marker import (
    Marker,
    MarkerKind,
    marked_description_q,
    parse_marker,
    render_marker,
    rewrite_marker,
    status_kind,
)
from .kea import KeaException, lease_fields
from .models import IPAMOwnershipLink, IPAMOwnershipSource, Server, SyncConfig, next_confirmation_number
from .plugin_settings import plugin_setting
from .pools import Pool
from .reservations import TRAVERSAL_DIAGNOSTIC_CODES, InSubnetReservationScope, Reservation, ReservationSnapshot
from .subnet_catalogue import CatalogueUnavailable
from .sync import sync_mac_address

if TYPE_CHECKING:
    from dcim.models import MACAddress

    from .subnet_catalogue import CompleteCatalogueSnapshot

logger = logging.getLogger(__name__)


def _get_stale_cleanup_mode() -> StaleCleanupMode:
    """Return the configured stale IP cleanup mode."""
    return plugin_setting("stale_ip_cleanup")


def _ip_description(description: str, status: str) -> str | None:
    """Rewrite an owned marker or mark an explicitly claimed object.

    Keep the operator note after an existing marker. Return None when the new marker and note do not fit.
    Callers decide whether the object is eligible before they call this helper.
    """
    kind = status_kind(status)
    marker = parse_marker(description)
    return rewrite_marker(marker, kind) if marker is not None else render_marker(kind)


def _apply_ip_fields(ip_obj: IPAddress, status: str, hostname: str, description: str) -> bool:
    """Apply *status*, *hostname* (dns_name), and *description* to *ip_obj*.

    The caller gets *description* from :func:`_ip_description`, so the marker kind follows *status* and
    self-heals on every run: an IP first created from a lease but later (also) reserved no longer stays
    marked ``lease``.

    Returns ``True`` when any field was changed and the object should be saved.
    """
    changed = False

    if ip_obj.status != status:
        ip_obj.status = status
        changed = True

    # Only update dns_name when the caller provides a non-empty hostname;
    # this prevents overwriting a manually maintained dns_name.
    if hostname and ip_obj.dns_name != hostname:
        ip_obj.dns_name = hostname
        changed = True

    if ip_obj.description != description:
        ip_obj.description = description
        changed = True

    return changed


def _apply_ip_mask(ip_obj: IPAddress, ip_str: str, prefix_len: int) -> bool:
    """Apply the reported mask to an eligible IP address and return whether it changed."""
    desired = f"{ip_str}/{prefix_len}"
    if str(ip_obj.address) == desired:
        return False
    ip_obj.address = desired
    return True


def _record_hostname(record: dict) -> str:
    """Validate a raw Kea record's optional hostname before synchronization."""
    hostname = record.get("hostname")
    if hostname is None:
        return ""
    if not isinstance(hostname, str):
        raise RuntimeError("Kea record hostname must be a string or null.")
    return hostname


class DuplicateNetBoxRowsError(Exception):
    """More than one NetBox Prefix or IP Range matches the key of one Kea subnet or pool.

    ``str()`` names the row pks, so it is for the server log only. The job log uses
    ``kea_object`` and ``list_url``, and the list view applies the viewer's permissions.
    """

    def __init__(self, kea_object: str, rows: str, pks: list[int], list_url: str) -> None:
        self.kea_object = kea_object
        self.rows = rows
        self.pks = pks
        self.list_url = list_url
        super().__init__(f"Kea {kea_object} matches duplicate NetBox {rows}: pks {pks}")


def _single_match(queryset, kea_object: str, list_filter: dict[str, str]):
    """Return the one row in *queryset*, ``None`` when it is empty, or raise :class:`DuplicateNetBoxRowsError`.

    *list_filter* holds the NetBox list-view query parameters that select the same rows.
    """
    from urllib.parse import urlencode

    from django.urls import reverse

    matches = list(queryset.order_by("pk"))
    if len(matches) > 1:
        meta = queryset.model._meta
        list_url = f"{reverse(f'ipam:{meta.model_name}_list')}?{urlencode(list_filter)}"
        raise DuplicateNetBoxRowsError(kea_object, str(meta.verbose_name_plural), [m.pk for m in matches], list_url)
    return matches[0] if matches else None


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

    completed_sources: set[str] = field(default_factory=set)
    waiting_objects: set[tuple[str, int]] = field(default_factory=set)
    unowned_objects: set[tuple[str, int]] = field(default_factory=set)
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
    quarantined_reservations: int = 0
    reservation_traversal_truncated: bool = False
    # Global and addressless Reservations, which write no IPAM row: the job counts them as skipped.
    skipped_reservations: list[Reservation] = field(default_factory=list)
    prefixes: dict[str, PrefixClaim] = field(default_factory=dict)

    def merge(self, other: SyncReport) -> None:
        """Accumulate work counts and deduplicate canonical ownership identities."""
        self.created += other.created
        self.updated += other.updated
        self.cleaned += other.cleaned
        self.removed += other.removed
        self.deprecated += other.deprecated
        self.errors += other.errors
        self.prefix_errors += other.prefix_errors
        self.quarantined_reservations += other.quarantined_reservations
        self.reservation_traversal_truncated |= other.reservation_traversal_truncated
        self.completed_sources.update(other.completed_sources)
        self.waiting_objects.update(other.waiting_objects)
        self.unowned_objects.update(other.unowned_objects)
        self.conflicts.update(other.conflicts)
        self.disagreements.update(other.disagreements)
        self.incomplete.update(other.incomplete)
        self.duplicates.extend(other.duplicates)
        self.skipped_reservations.extend(other.skipped_reservations)
        self.prefixes.update(other.prefixes)

    @property
    def waiting(self) -> int:
        """Count distinct objects protected by the upgrade barrier."""
        return len(self.waiting_objects)

    @property
    def unowned(self) -> int:
        """Count distinct marker objects with no ownership link."""
        return len(self.unowned_objects)

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
        if source in {"subnet", "pool", "delegated-prefix"}:
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


Workflow = Literal["job", "import"]
SourceScope = frozenset[tuple[Family, str]]


def effective_sources(server: Server) -> dict[Workflow, SourceScope]:
    """Return current potential owners, shared by workflow completion and the upgrade barrier."""
    return _effective_sources(server, SyncConfig.get())


def _effective_sources(server: Server, config: SyncConfig) -> dict[Workflow, SourceScope]:
    families: tuple[Family, ...] = tuple(
        cast("Family", family) for family, enabled in ((4, server.dhcp4), (6, server.dhcp6)) if enabled
    )
    sources = {
        "lease": "sync_leases_enabled",
        "reservation": "sync_reservations_enabled",
        "subnet": "sync_prefixes_enabled",
        "pool": "sync_ip_ranges_enabled",
    }
    job = frozenset(
        (family, source)
        for family in families
        for source, flag in sources.items()
        if config.sync_enabled and server.sync_enabled and getattr(config, flag) and getattr(server, flag)
    )
    imported = frozenset(
        (family, source)
        for family in families
        for source in ("reservation", "subnet", "pool", "delegated-prefix")
        if server.sync_dhcp_plugin_enabled
        and dhcp_plugin.is_available()
        and (source != "delegated-prefix" or family == 6)
    )
    return {"job": job, "import": imported}


@dataclass(frozen=True)
class _ObservationReceipt:
    """One successful whole workflow; the serializer owns its persisted shape."""

    sources: SourceScope
    completed_at: datetime

    def stored(self) -> dict[str, Any]:
        return {"sources": [list(pair) for pair in sorted(self.sources)], "completed_at": self.completed_at.isoformat()}

    @classmethod
    def read(cls, value: Any) -> _ObservationReceipt:
        if not isinstance(value, dict) or set(value) != {"sources", "completed_at"}:
            raise ValueError("Malformed IPAM observation receipt")
        sources = value["sources"]
        if not isinstance(sources, list) or any(
            not isinstance(pair, list)
            or len(pair) != 2
            or type(pair[0]) is not int
            or pair[0] not in (4, 6)
            or pair[1] not in {"lease", "reservation", "subnet", "pool", "delegated-prefix"}
            for pair in sources
        ):
            raise ValueError("Malformed IPAM observation source scope")
        completed_at = datetime.fromisoformat(value["completed_at"])
        if timezone.is_naive(completed_at):
            raise ValueError("An IPAM observation timestamp must be timezone aware")
        return cls(frozenset((cast("Family", pair[0]), pair[1]) for pair in sources), completed_at)


def _receipts(server: Server) -> dict[Workflow, _ObservationReceipt]:
    value = server.ipam_initial_observations
    if not isinstance(value, dict) or set(value) - {"job", "import"}:
        raise ValueError("Malformed IPAM initial observations")
    return {cast("Workflow", key): _ObservationReceipt.read(receipt) for key, receipt in value.items()}


def complete_job_observation(server: Server, reports: Mapping[Family, SyncReport]) -> None:
    """Publish only a whole job whose actual successful phase coverage satisfies current configuration."""
    if not reports or any(not report.complete or report.errors or report.prefix_errors for report in reports.values()):
        return
    scope = frozenset((family, source) for family, report in reports.items() for source in report.completed_sources)
    _complete_observation(server, "job", scope)


def complete_import_observation(server: Server, reports: Mapping[Family, SyncReport]) -> None:
    """Publish a whole import after successful ownership work and committed delegated attachments."""
    if not reports or any(not report.complete or report.errors or report.prefix_errors for report in reports.values()):
        return
    scope = frozenset((family, source) for family, report in reports.items() for source in report.completed_sources)
    _complete_observation(server, "import", scope)


def _complete_observation(server: Server, workflow: Workflow, observed: SourceScope) -> None:
    with transaction.atomic():
        current = Server.objects.select_for_update(no_key=True).get(pk=server.pk)
        required = effective_sources(current)
        if not required[workflow] or not required[workflow] <= observed:
            return
        receipts = _receipts(current)
        receipts[workflow] = _ObservationReceipt(observed, timezone.now())
        current.snapshot()
        current.ipam_initial_observations = {key: receipt.stored() for key, receipt in receipts.items()}
        if current.ipam_first_complete_at is None and all(
            not scope or (key in receipts and scope <= receipts[key].sources) for key, scope in required.items()
        ):
            current.ipam_first_complete_at = timezone.now()
        current.save(update_fields=["ipam_initial_observations", "ipam_first_complete_at"])
        server.ipam_initial_observations = current.ipam_initial_observations
        server.ipam_first_complete_at = current.ipam_first_complete_at


def _pending_adoptions() -> set[tuple[Family, str]]:
    """Read potential owners for the current policy or one count snapshot."""
    pending: set[tuple[Family, str]] = set()
    fields = {
        "lease": "ip_address",
        "reservation": "ip_address",
        "subnet": "prefix",
        "delegated-prefix": "prefix",
        "pool": "ip_range",
    }
    config = SyncConfig.get()
    for server in Server.objects.all():
        receipts = _receipts(server)
        for workflow, scope in _effective_sources(server, config).items():
            observed = receipts[workflow].sources if workflow in receipts else frozenset()
            pending.update((family, fields[source]) for family, source in scope - observed)
    return pending


def _adoption_waits(link: IPAMOwnershipLink, object_field: str, pending: set[tuple[Family, str]]) -> bool:
    return link.adopted and (link.family, object_field) in pending


def _cleanup_adoption_waits(link: IPAMOwnershipLink, object_field: str) -> bool:
    """Freeze policy for a final adopted link whose row the cleanup already locked."""
    if not link.adopted:
        return False
    with connection.cursor() as cursor:
        # Table locks also cover new Servers and bulk configuration updates.
        cursor.execute("LOCK TABLE netbox_kea_server, netbox_kea_syncconfig IN SHARE MODE NOWAIT")
    return _adoption_waits(link, object_field, _pending_adoptions())


def _cleanup_links(obj: IPAddress | Prefix | IPRange) -> list[IPAMOwnershipLink]:
    """Refuse a busy owner row before cleanup can wait with retained policy locks."""
    return list(
        IPAMOwnershipLink.objects.select_for_update(nowait=True).filter(**{_object_field(obj): obj}).order_by("pk")
    )


def upgrade_counts() -> SyncReport:
    """Count distinct unowned marker objects and marked objects held by the current upgrade barrier."""
    report = SyncReport()
    pending = _pending_adoptions()
    for object_field, model in (("ip_address", IPAddress), ("prefix", Prefix), ("ip_range", IPRange)):
        for obj in model.objects.filter(marked_description_q(), kea_ownership_links__isnull=True).only(
            "pk", "description"
        ):
            if _is_owned_description(obj.description):
                report.unowned_objects.add((object_field, obj.pk))
        for link in IPAMOwnershipLink.objects.filter(adopted=True, **{f"{object_field}__isnull": False}).select_related(
            object_field
        ):
            obj = getattr(link, object_field)
            if _is_owned_description(obj.description) and _adoption_waits(link, object_field, pending):
                report.waiting_objects.add((object_field, obj.pk))
    return report


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
    resolved_macs: Mapping[tuple[str, str], MACAddress] = field(default_factory=dict)

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

    conflicts: set[str] = field(default_factory=set)
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
        return ClaimResult()
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

    conflicts: set[str] = set()

    def apply(row: _Report) -> AddressClaim:
        outcome = _claim(server, family, source, row, force=force, conflicts=conflicts)
        ip = IPAddress.objects.filter(vrf_id=server.sync_vrf_id, address__net_host=row.address).first()
        resolved_macs = {}
        if outcome != "conflict":
            for hardware, hostname in row.mac_addresses:
                mac = sync_mac_address(hardware, hostname)
                if mac is None and source == RESERVATION:
                    raise _RowRefused("The required hardware address could not be resolved")
                if mac is not None:
                    resolved_macs[hardware, hostname] = mac
        if row.facts is None and not row.disagreement and outcome == "unchanged":
            if ip is not None and not _is_owned_description(ip.description):
                return AddressClaim(row.address, "conflict", ip)
            return AddressClaim(row.address, "not-applicable", ip)
        return AddressClaim(row.address, outcome, ip, resolved_macs)

    report = SyncReport()
    for row, result in _each_row(reports.values(), report, source, apply, lambda row: row.address):
        outcomes[row.address] = result
    return ClaimResult(addresses=outcomes, conflicts=conflicts)


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
    when the lease phase is complete and the Reservation phase is complete or disabled on the Server.
    Writes go to main only: the job refuses to run in a branch.
    """
    if len({phase.source for phase in phases}) != len(phases):
        raise ValueError("reconcile takes at most one phase of each source")
    mode = _get_stale_cleanup_mode()
    report = SyncReport()
    cutoffs = {phase.source: _run_phase(server, family, phase, report) for phase in phases}
    complete = {source for source, cutoff in cutoffs.items() if cutoff is not None}
    report.completed_sources.update(complete)
    last_links_go = LEASE in complete and (RESERVATION in complete or not server.sync_reservations_enabled)
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


def _lock_identity(vrf_id: int | None, address: str, *, nowait: bool = False) -> None:
    """Hold the advisory lock of one IP address identity, the VRF and the address, until the transaction ends."""
    _lock_key(f"ip-address {vrf_id} {address}", nowait=nowait)


def _lock_key(identity: str, *, nowait: bool) -> None:
    with connection.cursor() as cursor:
        function = "pg_try_advisory_xact_lock" if nowait else "pg_advisory_xact_lock"
        cursor.execute(f"SELECT {function}(%s, %s)", [_LOCK_CLASS, _int4(identity)])
        if nowait and not cursor.fetchone()[0]:
            raise _RowRefused("The IPAM identity is busy; retry cleanup on the next sync")


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
        lambda row: _claim(server, family, phase.source, row, conflicts=report.conflicts),
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
        # Without host_cmds, config-file Reservations can still exist. The phase is not complete.
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


def _claim(
    server: Server,
    family: Family,
    source: str,
    report: _Report,
    *,
    force: bool = False,
    conflicts: set[str] | None = None,
) -> _Outcome:
    """Link one reported address under its identity lock, and apply the report when no owner disagrees."""
    vrf_id = server.sync_vrf_id
    _lock_identity(None, report.address)
    if vrf_id is not None:
        _lock_identity(vrf_id, report.address)
    rows = list(
        IPAddress.objects.select_for_update().filter(vrf_id=vrf_id, address__net_host=report.address).order_by("pk")[:2]
    )
    if len(rows) > 1:
        raise _RowRefused(f"more than one IP address {report.address} in the sync VRF")
    if vrf_id is not None:
        legacy = list(
            IPAddress.objects.select_for_update()
            .filter(vrf__isnull=True, address__net_host=report.address)
            .order_by("pk")[:2]
        )
        eligible = (
            len(legacy) == 1
            and _is_owned_description(legacy[0].description)
            and not IPAMOwnershipLink.objects.filter(ip_address=legacy[0]).exists()
            and not Server.objects.exclude(sync_vrf_id=vrf_id).exists()
        )
        if eligible and rows:
            if conflicts is not None:
                conflicts.add(report.address)
        elif eligible and report.facts is not None:
            legacy[0].snapshot()
            legacy[0].vrf_id = vrf_id
            legacy[0].save()
            rows = legacy
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
    return _apply_claim(server, family, source, report, ip, links)


def _apply_claim(
    server: Server,
    family: Family,
    source: str,
    report: _Report,
    ip: IPAddress,
    links: list[IPAMOwnershipLink],
) -> _Outcome:
    """Compare owners before applying facts or an explicit takeover to the locked row."""
    facts = report.facts
    adopted = (not links and _is_owned_description(ip.description)) or any(link.adopted for link in links)
    own = next((link for link in links if _is_own_link(link, server, family, source)), None)
    others = _drop_superseded_stale_links([link for link in links if link is not own])
    if report.disagreement:
        _store_link(
            own, server, family, source, ip, own.facts if own else None, stale_mark=_kept_mark(own), adopted=adopted
        )
        return "disagreement"
    if facts is None:
        # A Global Reservation links the object without facts and does not change it (ADR 0002).
        _store_link(own, server, family, source, ip, None, stale_mark=_kept_mark(own), adopted=adopted)
        return "unchanged"
    applied = _applied_facts(source, facts, others)
    if applied is None:
        _store_link(own, server, family, source, ip, facts.stored(), stale_mark=_kept_mark(own), adopted=adopted)
        return "disagreement"

    status = _status(_live_sources(others) | {source})
    description = _ip_description(ip.description, status)
    if description is None:
        # The new marker and the operator note do not fit: the object stays as it is, and the owner keeps its link.
        _store_link(own, server, family, source, ip, facts.stored(), stale_mark=_kept_mark(own), adopted=adopted)
        return "conflict"
    ip.snapshot()
    changed = _apply_ip_fields(ip, status=status, hostname=applied.hostname, description=description)
    changed = _apply_ip_mask(ip, report.address, applied.prefix_length) or changed
    if changed:
        ip.save()
    _store_link(own, server, family, source, ip, facts.stored(), stale_mark=None, adopted=adopted)
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
    description = _ip_description(ip.description, status)
    if description is None:
        return "conflict"
    ip.snapshot()
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
    adopted: bool = False,
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
            adopted=adopted,
        )
        return
    link.facts = facts
    link.confirmation = confirmation
    link.stale_mark = stale_mark
    link.adopted = link.adopted or adopted
    link.save(update_fields=["facts", "confirmation", "stale_mark", "adopted"])


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
    nowait = connection.in_atomic_block
    rows = _each_row(
        candidates,
        report,
        source,
        lambda stale: _remove_stale_link(stale, cutoff, mode, last_links_go, referenced, nowait=nowait),
        lambda stale: f"stale {source} link of {stale.address} of Server {server.name}",
    )
    for stale, outcome in rows:
        if outcome == "waiting":
            report.waiting_objects.add(("ip_address", stale.ip_pk))
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
    stale: _StaleLink,
    cutoff: int,
    mode: StaleCleanupMode,
    last_links_go: bool,
    referenced: set[int],
    *,
    nowait: bool,
) -> str:
    """Decide one stale link under the identity lock and the row lock of its object."""
    _lock_identity(stale.vrf_id, stale.address, nowait=nowait)
    ip = IPAddress.objects.select_for_update(nowait=nowait).filter(pk=stale.ip_pk).first()
    if ip is None:
        return "kept"
    links = _cleanup_links(ip)
    link = next((link for link in links if link.pk == stale.pk), None)
    if link is None or link.confirmation >= cutoff:
        return "kept"
    if (ip.vrf_id, _host(ip.address)) != (stale.vrf_id, stale.address):
        raise _RowRefused(f"the address of IP address {ip.pk} changed while the cleanup read it")
    if not _is_owned_description(ip.description):
        IPAMOwnershipLink.objects.filter(ip_address=ip).delete()
        return "conflict"
    if _marked_and_unconfirmed(link):
        return "kept"
    others = [other for other in links if other.pk != link.pk]
    if not last_links_go and not any(other.server_id == link.server_id for other in others):
        # The last link of its Server needs a later call with complete required sources.
        return "kept"
    if others:
        outcome = _restatus(ip, others)
        if outcome != "conflict":
            # On a conflict the link stays, so the status still matches the links and the next run tries again.
            link.delete()
        return outcome
    if _cleanup_adoption_waits(link, "ip_address"):
        return "waiting"
    if mode == "none" or ip.pk in referenced:
        link.delete()
        return "unlinked"
    if mode == "remove":
        ip.delete()
        return "removed"
    link.stale_mark = cutoff
    link.save(update_fields=["stale_mark"])
    if ip.status != "deprecated":
        ip.snapshot()
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


def _lock_network(vrf_id: int | None, source: str, address: str, *, nowait: bool = False) -> None:
    kind = "ip-range" if source == "pool" else "prefix"
    _lock_key(f"{kind} {vrf_id} {address}", nowait=nowait)


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


def _network_marker_kind(sources: Collection[str]) -> MarkerKind:
    """Prefer the Subnet marker when a Prefix has both live ownership sources."""
    return "pool" if "pool" in sources else ("subnet" if "subnet" in sources else "delegated prefix")


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
    kind = _network_marker_kind({source})
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
    was_marked = marker is not None
    if marker is None:
        links_query.delete()
        if not force:
            return "conflict"
        marker = Marker(kind, f" {obj.description}" if obj.description else "", legacy=False)
    links = list(links_query)
    adopted = (not links and was_marked) or any(link.adopted for link in links)
    own = next((link for link in links if _is_own_link(link, server, family, source)), None)
    others = _drop_superseded_stale_links([link for link in links if link is not own])
    if row.disagreement:
        _store_link(
            own, server, family, source, obj, own.facts if own else None, stale_mark=_kept_mark(own), adopted=adopted
        )
        return "disagreement"
    if any(_is_live(link) and link.facts["prefix_length"] != row.prefix_length for link in others):
        _store_link(own, server, family, source, obj, row.facts(), stale_mark=_kept_mark(own), adopted=adopted)
        return "disagreement"
    description = rewrite_marker(marker, _network_marker_kind({source} | _live_sources(others)))
    if description is None:
        _store_link(own, server, family, source, obj, row.facts(), stale_mark=_kept_mark(own), adopted=adopted)
        return "conflict"
    fields.update(status="active", description=description)
    changed = any(str(getattr(obj, name)) != str(value) for name, value in fields.items())
    if changed:
        obj.snapshot()
        for name, value in fields.items():
            setattr(obj, name, value)
        obj.save()
    _store_link(own, server, family, source, obj, row.facts(), stale_mark=None, adopted=adopted)
    return "updated" if changed else "unchanged"


def _remove_stale_network_links(server: Server, family: Family, source: str, cutoff: int, report: SyncReport) -> None:
    """Drop the stale links of a complete Subnet or Pool phase. These objects are never removed."""
    field_name = "ip_range" if source == "pool" else "prefix"
    links = IPAMOwnershipLink.objects.filter(server=server, family=family, source=source, confirmation__lt=cutoff)
    # A marked link still needs the release check: an operator may have removed its object's marker.
    candidates = list(links.select_related(field_name))
    nowait = connection.in_atomic_block
    for link, outcome in _each_row(
        candidates,
        report,
        source,
        lambda link: _remove_stale_network_link(link, field_name, cutoff, nowait=nowait),
        lambda link: f"stale {source} link {link.pk}",
    ):
        if outcome == "waiting":
            report.waiting_objects.add((field_name, getattr(link, field_name).pk))
        if outcome == "deprecated":
            report.deprecated += 1
        elif outcome == "updated":
            report.updated += 1
        elif outcome == "conflict":
            obj = getattr(link, field_name)
            report.conflicts.add(
                str(obj.prefix) if field_name == "prefix" else f"{_host(obj.start_address)} - {_host(obj.end_address)}"
            )


def _remove_stale_network_link(candidate: IPAMOwnershipLink, field_name: str, cutoff: int, *, nowait: bool) -> str:
    obj = getattr(candidate, field_name)
    address = str(obj.prefix) if field_name == "prefix" else f"{_host(obj.start_address)} - {_host(obj.end_address)}"
    _lock_network(obj.vrf_id, candidate.source, address, nowait=nowait)
    locked = type(obj).objects.select_for_update(nowait=nowait).filter(pk=obj.pk).first()
    if locked is None:
        return "kept"
    owners = _cleanup_links(locked)
    link = next((link for link in owners if link.pk == candidate.pk), None)
    if link is None or link.confirmation >= cutoff:
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
    others = [other for other in owners if other.pk != link.pk]
    if others:
        changed = False
        if _live_sources(others):
            sources = _live_sources(others)
            description = rewrite_marker(marker, _network_marker_kind(sources))
            if description is None:
                return "conflict"
            changed = locked.status != "active" or locked.description != description
            if changed:
                locked.snapshot()
                locked.status = "active"
                locked.description = description
                locked.save()
        link.delete()
        return "updated" if changed else "unlinked"
    if _cleanup_adoption_waits(link, field_name):
        return "waiting"
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
        locked.snapshot()
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
