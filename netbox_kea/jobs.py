# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Background jobs for netbox-kea-ng plugin.

Registers periodic Kea→NetBox IPAM sync jobs using NetBox's built-in
``JobRunner`` / ``@system_job`` infrastructure so they run automatically via
``manage.py rqworker`` without any external scheduler.

The default sync interval is 5 minutes and can be overridden via
``PLUGINS_CONFIG["netbox_kea"]["sync_interval_minutes"]`` — the plugin's
``ready()`` hook patches the registry entry at startup.

Configuration knobs (all under ``PLUGINS_CONFIG["netbox_kea"]``):

``sync_interval_minutes`` (int, default 5)
    How often the sync job runs in minutes.

``sync_leases_enabled`` (bool, default True)
    Sync active Kea leases to NetBox IPAM (status=active).

``sync_reservations_enabled`` (bool, default True)
    Sync Kea reservations to NetBox IPAM (status=reserved).

``sync_max_leases_per_server`` (int, default 50000)
    Hard cap on leases fetched per server per run.  Prevents runaway memory
    consumption on very large deployments.  Set to 0 to disable the cap.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING, Any

from core.exceptions import JobFailed
from netbox.jobs import JobRunner, system_job

if TYPE_CHECKING:
    from .models import Server
    from .sync import DuplicateNetBoxRowsError

# Runtime import: get_type_hints() resolves this module's annotations, so a
# TYPE_CHECKING-only Family would make that fail with NameError.
from . import subnet_catalogue
from .constants import Family
from .reservations import Reservation, ReservationSnapshot
from .subnet_catalogue import CatalogueUnavailable, CompleteCatalogueSnapshot, VerifiedSubnet

logger = logging.getLogger(__name__)

# Default interval (minutes).  Can be overridden at startup via ready().
_DEFAULT_INTERVAL = 5


def _get_plugin_config() -> dict[str, Any]:
    """Return the netbox_kea section of PLUGINS_CONFIG (never raises)."""
    from django.conf import settings

    plugins_config = getattr(settings, "PLUGINS_CONFIG", {})
    if not isinstance(plugins_config, dict):
        logger.warning("PLUGINS_CONFIG is %s, expected dict — using defaults.", type(plugins_config).__name__)
        return {}
    config = plugins_config.get("netbox_kea", {})
    if not isinstance(config, dict):
        logger.warning("PLUGINS_CONFIG['netbox_kea'] is %s, expected dict — using defaults.", type(config).__name__)
        return {}
    return config


class _SnapshotSkipped:
    """Sentinel type for a Reservation Snapshot that Kea cannot serve at all."""

    __slots__ = ()


#: Kea reports result 2 when host_cmds is not loaded. Reservations are then not a
#: feature of this server, so the phase is skipped instead of counted as an error.
SNAPSHOT_SKIPPED = _SnapshotSkipped()


def _fetch_reservation_snapshot(
    server: Server, version: Family, catalogue: CompleteCatalogueSnapshot | None
) -> ReservationSnapshot | _SnapshotSkipped | None:
    """Read Reservations against the same verified catalogue as the other sync phases."""
    from .kea import KeaException

    if catalogue is None:
        return None

    try:
        client = server.get_client(version=version)
        return client.reservation_snapshot(version, catalogue)
    except KeaException as exc:
        if exc.unsupported_command:
            logger.warning("Server %s (v%s): host_cmds is unavailable; Reservation sync skipped", server.name, version)
            return SNAPSHOT_SKIPPED
        logger.warning("Server %s (v%s): Reservation Snapshot failed", server.name, version, exc_info=True)
        return None
    except Exception:
        logger.warning("Server %s (v%s): Reservation Snapshot failed", server.name, version, exc_info=True)
        return None


def _reservation_snapshot_ips(snapshot: ReservationSnapshot | _SnapshotSkipped | None) -> frozenset[str] | None:
    """Return all Snapshot addresses only when the traversal and every record are complete."""
    if snapshot is None or isinstance(snapshot, _SnapshotSkipped) or not snapshot.complete:
        return None
    return frozenset(str(address) for reservation in snapshot.records for address in reservation.addresses)


#: How many per-row reservation sync failures to log in full per server/version
#: before suppressing the rest.  The error count in the summary stays exact.
_ROW_ERROR_LOG_LIMIT = 10

#: How many conflicting IPs to name in the job summary and log line.  A bare count
#: tells an operator nothing about which manually-curated IPs the sync left alone.
_CONFLICT_SAMPLE_SIZE = 20


def _canonical_ip(ip_str: str) -> str:
    """Return the canonical text form of *ip_str*, or the input when unparseable.

    Conflict counts are deduplicated on this, so two spellings of the same IPv6
    address (``2001:db8::1`` and ``2001:0db8::0001``) collapse to one entry.
    """
    try:
        return str(ipaddress.ip_address(ip_str))
    except ValueError:
        return ip_str


def _record_conflicts(stats: dict[str, int], conflicts: list[str], conflict_ips: set[str] | None) -> None:
    """Fold *conflicts* into *stats*, deduplicating through *conflict_ips* when given.

    The accumulator stays a list because the sync helpers append to it; the caller's
    set is what deduplicates across phases and versions, so the count is taken from
    the set whenever there is one.
    """
    if conflict_ips is not None:
        conflict_ips.update(_canonical_ip(ip) for ip in conflicts)
        stats["conflicts"] = len(conflict_ips)
    else:
        stats["conflicts"] = stats.get("conflicts", 0) + len(conflicts)


def _sync_server_leases(
    server: Server,
    version: Family,
    *,
    max_leases: int,
    stats: dict[str, int],
    all_synced: list[dict | Reservation],
    reservation_ips: frozenset[str] | None = None,
    subnet_prefix_map: dict[int, int] | None = None,
    conflict_ips: set[str] | None = None,
) -> tuple[bool, frozenset[str]]:
    """Fetch all leases from *server* for *version* and upsert into NetBox IPAM.

    Returns ``(fully_completed, lease_ips)`` where *fully_completed* is ``True``
    only when the full lease set was fetched without truncation AND every
    individual lease row synced without error (``False`` means *all_synced* or
    *lease_ips* may be incomplete and should not be forwarded to reservation sync
    or used for cleanup) and *lease_ips* is the frozenset of successfully synced
    lease IP addresses processed this run (used for two-pass idempotent
    reservation sync).
    """
    from .sync import sync_lease_to_netbox

    try:
        client = server.get_client(version=version)
        collection = client.lease_get_all(version=version, max_leases=max_leases or None)
        raw_leases = collection.leases
        truncated = collection.truncated
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch leases from server %s (v%s): %s", server.name, version, exc)
        stats["errors"] += 1
        return False, frozenset()

    if truncated:
        logger.warning(
            "Server %s (v%s): lease fetch truncated at %d — increase sync_max_leases_per_server",
            server.name,
            version,
            max_leases,
        )

    logger.info("Server %s (v%s): fetched %d leases", server.name, version, len(raw_leases))

    lease_ips: set[str] = set()
    had_errors = False
    # Foreign (manually-curated) NetBox IPs skipped to avoid overwriting them.
    conflicts: list[str] = []
    for lease in raw_leases:
        try:
            ip, created, changed = sync_lease_to_netbox(
                lease,
                cleanup=False,
                reservation_ips=reservation_ips,
                subnet_prefix_map=subnet_prefix_map,
                conflicts=conflicts,
            )
            all_synced.append(lease)
            lease_ips.add(str(ipaddress.ip_interface(str(ip.address)).ip))
            if created:
                stats["created"] += 1
            elif changed:
                stats["updated"] += 1
        except Exception:  # noqa: PERF203
            logger.debug(
                "Failed to sync lease from server %s",
                server.name,
                exc_info=True,
            )
            stats["errors"] += 1
            had_errors = True

    _record_conflicts(stats, conflicts, conflict_ips)
    return not truncated and not had_errors, frozenset(lease_ips)


def _sync_server_reservations(
    server: Server,
    snapshot: ReservationSnapshot | _SnapshotSkipped | None,
    *,
    stats: dict[str, int],
    all_synced: list[dict | Reservation],
    protected: list[dict | Reservation],
    lease_ips: frozenset[str] | None = None,
    conflict_ips: set[str] | None = None,
) -> bool:
    """Synchronize all valid records in one typed Reservation Snapshot.

    Reservations that reserve no address — an identifier-only DHCPv4 host, or a
    DHCPv6 host that only delegates prefixes — are counted in ``stats["skipped"]``
    and left out of *all_synced*.  They are legal Kea configuration with nothing to
    write to IPAM, so treating them as errors failed the whole job (issue #110).

    A skipped Global Reservation still owns its addresses, so it joins *protected*:
    stale-IP cleanup must keep them when another record shares its hostname.

    Returns ``True`` when all reservation pages were fetched successfully,
    ``False`` when the sync was skipped (e.g. host_cmds not loaded) or failed.
    A ``False`` return means *all_synced* may be incomplete and cleanup must be
    skipped.  Only a genuine failure counts an error; ``SNAPSHOT_SKIPPED`` does not.
    """
    from .sync import sync_reservation_to_netbox

    processed = 0
    skipped = 0
    row_errors_logged = 0
    had_errors = False
    # Foreign (manually-curated) NetBox IPs skipped to avoid overwriting them.
    conflicts: list[str] = []

    if isinstance(snapshot, _SnapshotSkipped):
        return False
    if snapshot is None:
        stats["errors"] += 1
        return False
    if snapshot.diagnostics:
        stats["errors"] += len(snapshot.diagnostics)
        logger.warning(
            "Server %s (v%s): reported %d Reservation Snapshot diagnostic(s)",
            server.name,
            snapshot.family,
            len(snapshot.diagnostics),
        )

    for reservation in snapshot.records:
        scope = reservation.scope
        if scope.kind == "global" or not reservation.addresses:
            skipped += 1
            stats["skipped"] = stats.get("skipped", 0) + 1
            protected.append(reservation)
            continue
        try:
            result = sync_reservation_to_netbox(
                reservation,
                cleanup=False,
                lease_ips=lease_ips,
                conflicts=conflicts,
            )
            all_synced.append(reservation)
            processed += 1
            stats["created"] += result.created
            stats["updated"] += result.changed
        except Exception as exc:
            if row_errors_logged < _ROW_ERROR_LOG_LIMIT:
                row_errors_logged += 1
                logger.warning(
                    "Failed to sync Reservation (server %s, v%s, subnet-id %s, id-type %s): %s",
                    server.name,
                    reservation.family,
                    scope.subnet.subnet_id,
                    reservation.identity.identifier_type,
                    type(exc).__name__,
                )
                logger.debug("Reservation sync traceback", exc_info=True)
            elif row_errors_logged == _ROW_ERROR_LOG_LIMIT:
                row_errors_logged += 1
                logger.warning(
                    "Server %s (v%s): further per-Reservation sync failures suppressed"
                    " (first %d logged); see the final error count.",
                    server.name,
                    reservation.family,
                    _ROW_ERROR_LOG_LIMIT,
                )
            stats["errors"] += 1
            had_errors = True

    _record_conflicts(stats, conflicts, conflict_ips)
    if skipped:
        logger.info(
            "Server %s (v%s): skipped %d Global or addressless Reservation(s)",
            server.name,
            snapshot.family,
            skipped,
        )

    logger.info("Server %s (v%s): synced %d Reservations", server.name, snapshot.family, processed)
    # Mirror the lease path: a per-row failure must not leave cleanup_safe=True,
    # or stale cleanup runs with an incomplete keep-set and may delete live IPs.
    return snapshot.complete and not had_errors


def _sync_subnet_entry(
    subnet: VerifiedSubnet,
    sync_prefixes: bool,
    sync_ip_ranges: bool,
    vrf,
    stats: dict[str, int],
    server_name: str,
    duplicates: list[DuplicateNetBoxRowsError],
) -> None:
    """Sync one verified Subnet to a NetBox Prefix and its allocation ranges."""
    from .sync import (
        _POOL_TOO_LARGE,
        DuplicateNetBoxRowsError,
        sync_pool_to_netbox_ip_range,
        sync_subnet_to_netbox_prefix,
    )

    subnet_cidr = subnet.cidr

    if sync_prefixes:
        try:
            _, created, did_update = sync_subnet_to_netbox_prefix(subnet_cidr, vrf=vrf)
            if created:
                stats["created"] += 1
            elif did_update:
                stats["updated"] += 1
        except DuplicateNetBoxRowsError as exc:
            logger.exception("Failed to sync prefix %s from server %s", subnet_cidr, server_name)
            stats["prefix_errors"] += 1
            duplicates.append(exc)
        except Exception:
            logger.exception("Failed to sync prefix %s from server %s", subnet_cidr, server_name)
            stats["prefix_errors"] += 1

    if sync_ip_ranges:
        pools = subnet.configuration.pools if subnet.configuration is not None else ()
        for pool in pools:
            pool_str = pool.range
            try:
                result = sync_pool_to_netbox_ip_range(pool_str, subnet_cidr, vrf=vrf)
                if result is _POOL_TOO_LARGE:
                    # Intentional skip; not an error.
                    pass
                elif result is None:
                    logger.warning(
                        "Failed to parse pool %s in subnet %s from server %s",
                        pool_str,
                        subnet_cidr,
                        server_name,
                    )
                    stats["prefix_errors"] += 1
                else:
                    _, created, did_update = result
                    if created:
                        stats["created"] += 1
                    elif did_update:
                        stats["updated"] += 1
            except DuplicateNetBoxRowsError as exc:
                logger.exception("Failed to sync pool %s from server %s", pool_str, server_name)
                stats["prefix_errors"] += 1
                duplicates.append(exc)
            except Exception:
                logger.exception("Failed to sync pool %s from server %s", pool_str, server_name)
                stats["prefix_errors"] += 1


def _sync_server_prefixes_and_ranges(
    server: Server,
    version: Family,
    *,
    catalogue: CompleteCatalogueSnapshot | None,
    sync_prefixes: bool,
    sync_ip_ranges: bool,
    vrf=None,
    stats: dict[str, int],
    duplicates: list[DuplicateNetBoxRowsError],
) -> None:
    """Sync Prefixes and IP Ranges from the run's complete catalogue."""
    if catalogue is None:
        logger.warning(
            "Server %s (v%s): skipping prefix/range sync: Subnet Catalogue unavailable", server.name, version
        )
        stats["prefix_errors"] += 1
        return

    logger.info("Server %s (v%s): found %d subnets for prefix/range sync", server.name, version, len(catalogue.subnets))
    for subnet in catalogue.subnets:
        _sync_subnet_entry(subnet, sync_prefixes, sync_ip_ranges, vrf, stats, server.name, duplicates)


def _sync_one_server(
    server: Server,
    sync_leases: bool,
    sync_reservations: bool,
    sync_prefixes: bool,
    sync_ip_ranges: bool,
    max_leases: int,
    stats: dict[str, int],
    conflict_ips: set[str] | None = None,
    duplicates: list[DuplicateNetBoxRowsError] | None = None,
) -> None:
    """Sync a single server's leases, reservations, prefixes, and IP ranges.

    *conflict_ips* is an optional caller-owned set that collects the foreign NetBox
    IPs this run refused to overwrite, so the caller can name them in the job
    summary.  One set per server, shared by both phases and both IP versions: a
    foreign IP that has *both* a lease and a reservation is one conflict for the
    operator to resolve, not two.  Each phase still accumulates into its own list
    because ``sync_{lease,reservation}_to_netbox`` append to it.

    *duplicates* is an optional caller-owned list that collects the Kea subnets and
    pools that match more than one NetBox row, so the caller can name them.
    """
    from .sync import cleanup_stale_ips_batch

    all_synced: list[dict | Reservation] = []
    # Records the job deliberately did not write, whose addresses cleanup must keep.
    protected: list[dict | Reservation] = []
    if conflict_ips is None:
        conflict_ips = set()
    if duplicates is None:
        duplicates = []
    # Cleanup is only safe when both sources contributed, otherwise we risk
    # removing IPs that exist in the source we didn't sync.
    cleanup_safe = sync_leases and sync_reservations
    versions: tuple[tuple[Family, bool], ...] = ((4, server.dhcp4), (6, server.dhcp6))
    for version, enabled in versions:
        if not enabled:
            continue

        catalogue = None
        if any((sync_leases, sync_reservations, sync_prefixes, sync_ip_ranges)):
            try:
                catalogue = subnet_catalogue.for_synchronization(server, version)
            except CatalogueUnavailable as exc:
                logger.warning("Server %s (v%s): Subnet Catalogue unavailable: %s", server.name, version, exc)
        subnet_prefix_map = (
            {subnet.identity.subnet_id: subnet.identity.network.prefixlen for subnet in catalogue.subnets}
            if catalogue is not None
            else {}
        )
        if catalogue is None and sync_leases:
            logger.info(
                "Server %s (v%s): Subnet Catalogue unavailable; lease masks fall back to NetBox prefix matching",
                server.name,
                version,
            )

        reservation_snapshot = _fetch_reservation_snapshot(server, version, catalogue) if sync_reservations else None
        pre_reservation_ips = (
            _reservation_snapshot_ips(reservation_snapshot) if sync_leases and sync_reservations else None
        )

        lease_ips_set: frozenset[str] = frozenset()
        lease_phase_ok = False
        if sync_leases:
            sync_ok, lease_ips_set = _sync_server_leases(
                server,
                version,
                max_leases=max_leases,
                stats=stats,
                all_synced=all_synced,
                reservation_ips=pre_reservation_ips,
                subnet_prefix_map=subnet_prefix_map,
                conflict_ips=conflict_ips,
            )
            lease_phase_ok = sync_ok
            cleanup_safe &= sync_ok

        if sync_reservations:
            # Pass lease_ips_set only when the lease phase fully completed this run;
            # None tells reservation sync to use single-pass fallback mode.
            cleanup_safe &= _sync_server_reservations(
                server,
                reservation_snapshot,
                stats=stats,
                all_synced=all_synced,
                protected=protected,
                lease_ips=lease_ips_set if lease_phase_ok else None,
                conflict_ips=conflict_ips,
            )

        if sync_prefixes or sync_ip_ranges:
            _sync_server_prefixes_and_ranges(
                server,
                version,
                catalogue=catalogue,
                sync_prefixes=sync_prefixes,
                sync_ip_ranges=sync_ip_ranges,
                vrf=server.sync_vrf,
                stats=stats,
                duplicates=duplicates,
            )

    # Authoritative count: the per-phase increments above double-count an IP that is
    # foreign to both a lease and a reservation, so the deduplicated set wins.
    stats["conflicts"] = len(conflict_ips)
    if conflict_ips:
        sample = sorted(conflict_ips)[:_CONFLICT_SAMPLE_SIZE]
        logger.warning(
            "Server %s: %d NetBox IP(s) left untouched — not Kea-managed (description does not start"
            " with 'Synced from Kea DHCP'); first %d: %s",
            server.name,
            len(conflict_ips),
            len(sample),
            ", ".join(sample),
        )

    if all_synced and stats["errors"] == 0 and cleanup_safe:
        cleanup_stale_ips_batch(all_synced, protected)
    elif all_synced:
        logger.warning(
            "Server %s: skipping stale-IP cleanup (errors=%d, prefix_errors=%d, cleanup_safe=%s)",
            server.name,
            stats["errors"],
            stats.get("prefix_errors", 0),
            cleanup_safe,
        )


@system_job(interval=_DEFAULT_INTERVAL)
class KeaIpamSyncJob(JobRunner):
    """Periodic Kea→NetBox IPAM sync job.

    Iterates over all configured ``Server`` objects and syncs their active
    leases and/or reservations into NetBox ``IPAddress`` records.  Error on
    one server does not prevent syncing the remaining servers.

    The sync is idempotent: existing ``IPAddress`` objects are updated in place
    when their fields have changed; unchanged records are left untouched.
    """

    class Meta:
        name = "Kea IPAM Sync"

    @classmethod
    def enqueue_once(cls, *args: Any, **kwargs: Any) -> Any:
        """Heal ghost scheduled-job records before delegating to NetBox.

        NetBox schedules system jobs by calling ``enqueue_once`` on the job class
        once per ``rqworker`` startup (``core/management/commands/rqworker.py``).
        That runs after app initialisation and on the worker process only — so it
        is the correct place to reconcile stale schedule state, unlike
        ``AppConfig.ready()`` which also fires during ``collectstatic``/``migrate``
        (where the DB may be unreachable) and triggers Django's "database access
        during app initialization" warning.

        We clean ghost records first, then delegate to the stock
        ``enqueue_once`` so it can create a fresh schedule.  ``*args``/``**kwargs``
        are forwarded verbatim to insulate against signature drift across NetBox
        versions.
        """
        cls._heal_ghost_scheduled_jobs()
        return super().enqueue_once(*args, **kwargs)

    @classmethod
    def _heal_ghost_scheduled_jobs(cls) -> None:
        """Remove ghost scheduled-job DB records that have no live RQ counterpart.

        A ghost record arises when a periodic job's execution fails at the DB level
        (e.g. PostgreSQL in recovery mode) — the DB record stays ``scheduled`` forever
        while RQ marks the job ``failed``.  NetBox's ``enqueue_once()`` trusts the DB
        status and skips re-scheduling, silently breaking the periodic chain.

        Deleting stale DB records here lets the immediately-following
        ``super().enqueue_once()`` call create a fresh schedule.

        Safe to call in all contexts:
        - Never enqueues jobs (the caller handles that).
        - All DB and Redis I/O is wrapped so a missing DB or a Redis still coming
          up at worker startup never breaks scheduling.
        """
        try:
            import django_rq
            from rq.exceptions import NoSuchJobError
            from rq.job import Job as RQJob

            candidates = cls.get_jobs(None).filter(status__in=("scheduled", "pending"))
            if not candidates.exists():
                return

            conn = django_rq.get_connection("default")
            _DEAD = {"failed", "canceled", "stopped"}

            deleted = 0
            for db_job in candidates:
                try:
                    job_id = str(db_job.job_id) if db_job.job_id else None
                    if job_id is None:
                        db_job.delete()
                        deleted += 1
                        continue
                    try:
                        rq_job = RQJob.fetch(job_id, connection=conn)
                        status = rq_job.get_status()
                        # Handle both enum (rq ≥ 1.16) and plain string
                        status_str = status.value if hasattr(status, "value") else str(status)
                        if status_str in _DEAD:
                            db_job.delete()
                            deleted += 1
                    except NoSuchJobError:
                        db_job.delete()
                        deleted += 1
                except Exception:
                    logger.debug(
                        "netbox_kea: skipping ghost-job check for record %r due to per-record error.",
                        getattr(db_job, "pk", None),
                        exc_info=True,
                    )

            if deleted:
                logger.warning(
                    "netbox_kea: removed %d ghost scheduled-job record(s) with a dead or missing "
                    "RQ counterpart. Periodic IPAM sync will resume now.",
                    deleted,
                )
        except Exception:
            logger.warning(
                "netbox_kea: ghost-job self-heal skipped (DB or Redis not available at scheduling time).",
                exc_info=True,
            )

    def run(self, *args: Any, **kwargs: Any) -> None:
        """Execute the sync across all servers."""
        from .models import Server, SyncConfig

        summary: list[dict] = []
        try:
            sync_cfg = SyncConfig.get()
            if not sync_cfg.sync_enabled:
                self.logger.info("Global sync kill-switch is active (SyncConfig.sync_enabled=False) — skipping.")
                return

            config = _get_plugin_config()
            sync_leases = sync_cfg.sync_leases_enabled
            sync_reservations = sync_cfg.sync_reservations_enabled
            sync_prefixes = sync_cfg.sync_prefixes_enabled
            sync_ip_ranges = sync_cfg.sync_ip_ranges_enabled
            raw_max_leases = config.get("sync_max_leases_per_server", 50000)
            try:
                max_leases = int(raw_max_leases)
            except (TypeError, ValueError):
                self.logger.warning(f"Invalid sync_max_leases_per_server={raw_max_leases!r}; falling back to 50000")
                max_leases = 50000
            if max_leases < 0:
                self.logger.warning(
                    f"Negative sync_max_leases_per_server={max_leases} is not allowed; using 0 (no cap)"
                )
                max_leases = 0

            if not any([sync_leases, sync_reservations, sync_prefixes, sync_ip_ranges]):
                self.logger.info("All sync type flags are False — nothing to do.")
                return

            server_pk = kwargs.get("server_pk")
            server_qs = Server.objects.filter(pk=server_pk) if server_pk is not None else Server.objects.all()
            servers = list(server_qs)

            if not servers:
                self.logger.info("No Kea servers configured — nothing to sync.")
                return

            self.logger.info(f"Starting Kea IPAM sync for {len(servers)} server(s).")
            total: dict[str, int] = {
                "created": 0,
                "updated": 0,
                "errors": 0,
                "prefix_errors": 0,
                "conflicts": 0,
                "skipped": 0,
            }

            for server in servers:
                # In Run Now mode (server_pk provided), honour the explicit selection
                # and skip the per-server enabled check.
                if server_pk is None and not server.sync_enabled:
                    self.logger.info(f"Server {server.name}: sync_enabled=False — skipping.")
                    continue

                # Per-server type overrides: AND global flag with server flag.
                effective_leases = sync_leases and server.sync_leases_enabled
                effective_reservations = sync_reservations and server.sync_reservations_enabled
                effective_prefixes = sync_prefixes and server.sync_prefixes_enabled
                effective_ip_ranges = sync_ip_ranges and server.sync_ip_ranges_enabled

                self.logger.debug(f"Syncing server: {server.name} (pk={server.pk})")
                server_stats: dict[str, int] = {
                    "created": 0,
                    "updated": 0,
                    "errors": 0,
                    "prefix_errors": 0,
                    "conflicts": 0,
                    "skipped": 0,
                }
                # Foreign NetBox IPs this server refused to overwrite, deduplicated
                # across the lease and reservation phases and both IP versions.
                conflict_ips: set[str] = set()
                duplicates: list[DuplicateNetBoxRowsError] = []

                try:
                    _sync_one_server(
                        server,
                        effective_leases,
                        effective_reservations,
                        effective_prefixes,
                        effective_ip_ranges,
                        max_leases,
                        server_stats,
                        conflict_ips=conflict_ips,
                        duplicates=duplicates,
                    )
                except Exception:
                    self.logger.exception(f"Unhandled error syncing server {server.name}; see server logs")
                    server_stats["errors"] += 1

                self.logger.info(
                    f"Server {server.name}: created={server_stats['created']}"
                    f" updated={server_stats['updated']} errors={server_stats['errors']}"
                    f" prefix_errors={server_stats['prefix_errors']}"
                    f" conflicts={server_stats['conflicts']}"
                    f" skipped={server_stats['skipped']}"
                )
                # No row pks here: the list URL applies the viewer's own IPAM permissions.
                for dup in duplicates:
                    self.logger.error(
                        f"Server {server.name}: Kea {dup.kea_object} matches duplicate NetBox {dup.rows};"
                        f" the sync leaves them unchanged. Review them at {dup.list_url}"
                    )
                for key in total:
                    total[key] += server_stats.get(key, 0)

                summary.append(
                    {
                        "name": server.name,
                        "pk": server.pk,
                        "created": server_stats["created"],
                        "updated": server_stats["updated"],
                        "errors": server_stats["errors"],
                        "prefix_errors": server_stats["prefix_errors"],
                        "conflicts": server_stats["conflicts"],
                        "conflict_sample": sorted(conflict_ips)[:_CONFLICT_SAMPLE_SIZE],
                        "conflicts_truncated": max(0, len(conflict_ips) - _CONFLICT_SAMPLE_SIZE),
                        "skipped": server_stats["skipped"],
                    }
                )

            self.logger.info(
                f"Kea IPAM sync complete — servers={len(summary)}"
                f" created={total['created']} updated={total['updated']}"
                f" errors={total['errors']} prefix_errors={total['prefix_errors']}"
                f" conflicts={total['conflicts']} skipped={total['skipped']}"
            )
            if total["errors"] > 0 or total["prefix_errors"] > 0:
                raise JobFailed
        finally:
            if not isinstance(self.job.data, dict):
                self.job.data = {}
            self.job.data["summary"] = summary
            try:
                self.job.save(update_fields=["data"])
            except Exception:
                logger.exception("Failed to persist sync job summary data")
