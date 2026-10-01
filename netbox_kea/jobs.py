# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Background jobs for netbox-kea-ng plugin.

Registers periodic Kea→NetBox IPAM sync jobs using NetBox's built-in
``JobRunner`` / ``@system_job`` infrastructure so they run automatically via
``manage.py rqworker`` without any external scheduler.

The sync interval comes from ``SyncConfig.interval_minutes``, which the Sync Jobs
page edits. ``enqueue_once`` and each periodic run read it, so a saved value
applies after the next scheduled run.

The ``PLUGINS_CONFIG["netbox_kea"]`` settings and their rules are in ``plugin_settings.SETTINGS``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.exceptions import JobFailed
from netbox.jobs import JobRunner, system_job

if TYPE_CHECKING:
    from .models import Server

# Runtime imports: get_type_hints() resolves this module's annotations, so a
# TYPE_CHECKING-only Family or DuplicateNetBoxRowsError would raise NameError.
from . import branching, subnet_catalogue
from .constants import Family
from .plugin_settings import plugin_setting
from .reservations import Reservation
from .subnet_catalogue import CatalogueUnavailable, CompleteCatalogueSnapshot, VerifiedSubnet
from .sync import DuplicateNetBoxRowsError

logger = logging.getLogger(__name__)

# NetBox requires a registry interval; enqueue_once replaces it with SyncConfig.interval_minutes.
_DEFAULT_INTERVAL = 5


#: How many conflicting IPs to name in the job summary and log line.  A bare count
#: tells an operator nothing about which manually-curated IPs the sync left alone.
_CONFLICT_SAMPLE_SIZE = 20


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
        sync_pool_to_netbox_ip_range,
        sync_subnet_to_netbox_prefix,
    )

    subnet_cidr = subnet.cidr

    if sync_prefixes:
        try:
            _, created, did_update = sync_subnet_to_netbox_prefix(subnet.network, vrf=vrf)
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
            try:
                result = sync_pool_to_netbox_ip_range(pool, subnet.network, vrf=vrf)
                if result is not _POOL_TOO_LARGE:
                    _, created, did_update = result
                    if created:
                        stats["created"] += 1
                    elif did_update:
                        stats["updated"] += 1
            except DuplicateNetBoxRowsError as exc:  # noqa: PERF203
                logger.exception("Failed to sync pool %s from server %s", pool.range, server_name)
                stats["prefix_errors"] += 1
                duplicates.append(exc)
            except Exception:
                logger.exception("Failed to sync pool %s from server %s", pool.range, server_name)
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
    *,
    conflict_ips: set[str],
    disagreement_ips: set[str],
    duplicates: list[DuplicateNetBoxRowsError],
) -> None:
    """Sync a single server's leases, reservations, prefixes, and IP ranges.

    The lease and Reservation phases of each family run in one ``reconcile`` call (ADR 0006).

    *conflict_ips* is a caller-owned set that collects the NetBox IPs this run refused to change, so the caller can
    name them in the job summary. One set per server, shared by both phases and both IP versions: a foreign IP that
    has *both* a lease and a reservation is one conflict for the operator to resolve, not two.

    *disagreement_ips* collects the addresses whose owners report different facts (ADR 0006).

    *duplicates* is a caller-owned list that collects the Kea subnets and
    pools that match more than one NetBox row, so the caller can name them.
    """
    from .ipam_reconciliation import LeasePhase, Phase, ReservationPhase, reconcile
    from .sync import cleanup_stale_ips_batch

    all_synced: list[dict | Reservation] = []
    # Records the job deliberately did not write, whose addresses cleanup must keep.
    protected: list[dict | Reservation] = []
    # The old stale cleanup is only safe when both sources contributed, otherwise it
    # could remove IPs that exist in the source the run did not read.
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
            else None
        )
        if catalogue is None and sync_leases:
            logger.info(
                "Server %s (v%s): Subnet Catalogue unavailable; lease masks fall back to NetBox prefix matching",
                server.name,
                version,
            )

        phases: list[Phase] = []
        if sync_leases:
            phases.append(LeasePhase(max_leases=max_leases or None, subnet_prefix_lengths=subnet_prefix_map))
        if sync_reservations:
            phases.append(ReservationPhase(catalogue=catalogue))
        if phases:
            report = reconcile(server, version, phases)
            stats["created"] += report.created
            stats["updated"] += report.updated
            stats["errors"] += report.errors
            stats["skipped"] += len(report.skipped_reservations)
            conflict_ips.update(report.conflicts)
            disagreement_ips.update(report.disagreements)
            # Until #214 the old stale cleanup reads the reported records too; it never touches a linked row.
            all_synced.extend(report.lease_records)
            all_synced.extend(report.reservation_records)
            protected.extend(report.skipped_reservations)
            cleanup_safe &= report.complete

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

    stats["conflicts"] = len(conflict_ips)
    stats["disagreements"] = len(disagreement_ips)
    if disagreement_ips:
        sample = sorted(disagreement_ips)[:_CONFLICT_SAMPLE_SIZE]
        logger.warning(
            "Server %s: %d NetBox IP(s) keep their facts because their owners report different facts; first %d: %s",
            server.name,
            len(disagreement_ips),
            len(sample),
            ", ".join(sample),
        )
    if conflict_ips:
        sample = sorted(conflict_ips)[:_CONFLICT_SAMPLE_SIZE]
        logger.warning(
            "Server %s: %d NetBox IP(s) left untouched: the description does not start with the sync marker,"
            " or the new marker and the note do not fit; first %d: %s",
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
    def enqueue_once(
        cls, instance: Any = None, schedule_at: Any = None, interval: int | None = None, *args: Any, **kwargs: Any
    ) -> Any:
        """Heal ghost scheduled-job records before delegating to NetBox.

        NetBox schedules system jobs by calling ``enqueue_once`` on the job class
        once per ``rqworker`` startup (``core/management/commands/rqworker.py``).
        That runs after app initialisation and on the worker process only — so it
        is the correct place to reconcile stale schedule state, unlike
        ``AppConfig.ready()`` which also fires during ``collectstatic``/``migrate``
        (where the DB may be unreachable) and triggers Django's "database access
        during app initialization" warning.

        We clean ghost records first, then delegate to the stock
        ``enqueue_once`` so it can create a fresh schedule.  The signature is
        NetBox's, so a positional ``interval`` binds once; ``SyncConfig.interval_minutes``
        replaces it, and the other arguments are forwarded verbatim.
        """
        from .models import SyncConfig

        cls._heal_ghost_scheduled_jobs()
        return super().enqueue_once(instance, schedule_at, SyncConfig.get().interval_minutes, *args, **kwargs)

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

    def _fail_in_branch(self) -> None:
        """Raise JobFailed when a branch is active in the worker: the job reads and writes main only."""
        if (branch := branching.active_branch()) is not None:
            # NetBox saves the error when it marks the job failed; the job itself writes nothing here.
            self.job.error = f"Branch {branch} is active in the worker. The Kea IPAM sync runs on main only."
            raise JobFailed(self.job.error)

    def run(self, *args: Any, **kwargs: Any) -> None:
        """Execute the sync across all servers. It fails before any read when a branch is active in the worker."""
        from .models import Server, SyncConfig

        self._fail_in_branch()
        summary: list[dict] = []
        try:
            sync_cfg = SyncConfig.get()
            if self.job.interval:
                # handle() schedules the successor with job.interval after run() returns.
                self.job.interval = sync_cfg.interval_minutes
            if not sync_cfg.sync_enabled:
                self.logger.info("Global sync kill-switch is active (SyncConfig.sync_enabled=False) — skipping.")
                return

            sync_leases = sync_cfg.sync_leases_enabled
            sync_reservations = sync_cfg.sync_reservations_enabled
            sync_prefixes = sync_cfg.sync_prefixes_enabled
            sync_ip_ranges = sync_cfg.sync_ip_ranges_enabled
            max_leases = plugin_setting("sync_max_leases_per_server")

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
                "disagreements": 0,
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
                    "disagreements": 0,
                    "skipped": 0,
                }
                # Foreign NetBox IPs this server refused to overwrite, deduplicated
                # across the lease and reservation phases and both IP versions.
                conflict_ips: set[str] = set()
                disagreement_ips: set[str] = set()
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
                        disagreement_ips=disagreement_ips,
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
                    f" disagreements={server_stats['disagreements']}"
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
                        "disagreements": server_stats["disagreements"],
                        "skipped": server_stats["skipped"],
                    }
                )

            self.logger.info(
                f"Kea IPAM sync complete — servers={len(summary)}"
                f" created={total['created']} updated={total['updated']}"
                f" errors={total['errors']} prefix_errors={total['prefix_errors']}"
                f" conflicts={total['conflicts']} disagreements={total['disagreements']} skipped={total['skipped']}"
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
