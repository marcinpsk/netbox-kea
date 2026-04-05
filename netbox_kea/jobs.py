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

import logging
from typing import TYPE_CHECKING, Any

from netbox.jobs import JobRunner, system_job

if TYPE_CHECKING:
    from .models import Server

logger = logging.getLogger(__name__)

# Default interval (minutes).  Can be overridden at startup via ready().
_DEFAULT_INTERVAL = 5


def _get_plugin_config() -> dict[str, Any]:
    """Return the netbox_kea section of PLUGINS_CONFIG (never raises)."""
    from django.conf import settings

    return getattr(settings, "PLUGINS_CONFIG", {}).get("netbox_kea", {})


def _sync_server_leases(
    server: Server,
    version: int,
    *,
    max_leases: int,
    stats: dict[str, int],
    all_synced: list[dict],
) -> bool:
    """Fetch all leases from *server* for *version* and upsert into NetBox IPAM.

    Returns ``True`` when the full lease set was fetched and synced without
    truncation, ``False`` otherwise.  A ``False`` return means *all_synced* may
    be incomplete and cleanup must be skipped.
    """
    from .sync import sync_lease_to_netbox

    try:
        client = server.get_client(version=version)
        raw_leases, truncated = client.lease_get_all(version=version, max_leases=max_leases or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch leases from server %s (v%s): %s", server.name, version, exc)
        stats["errors"] += 1
        return False

    if truncated:
        logger.warning(
            "Server %s (v%s): lease fetch truncated at %d — increase sync_max_leases_per_server",
            server.name,
            version,
            max_leases,
        )

    logger.info("Server %s (v%s): fetched %d leases", server.name, version, len(raw_leases))

    for lease in raw_leases:
        try:
            _ip, created = sync_lease_to_netbox(lease, cleanup=False)
            all_synced.append(lease)
            if created:
                stats["created"] += 1
            else:
                stats["updated"] += 1
        except Exception:  # noqa: BLE001, PERF203
            logger.debug(
                "Failed to sync lease %s from server %s",
                lease.get("ip-address", "?"),
                server.name,
                exc_info=True,
            )
            stats["errors"] += 1

    return not truncated


def _sync_server_reservations(
    server: Server,
    version: int,
    *,
    stats: dict[str, int],
    all_synced: list[dict],
) -> bool:
    """Fetch all reservations from *server* for *version* and upsert into NetBox IPAM.

    Returns ``True`` when all reservation pages were fetched successfully,
    ``False`` when the sync was skipped (e.g. host_cmds not loaded) or failed.
    A ``False`` return means *all_synced* may be incomplete and cleanup must be
    skipped.
    """
    from .kea import KeaException
    from .sync import sync_reservation_to_netbox

    service = f"dhcp{version}"
    from_index = 0
    source_index = 0
    processed = 0

    try:
        client = server.get_client(version=version)
        while True:
            page, next_from, next_source = client.reservation_get_page(
                service,
                source_index=source_index,
                from_index=from_index,
                limit=100,
            )
            for reservation in page:
                try:
                    _ip, created = sync_reservation_to_netbox(reservation, cleanup=False)
                    all_synced.append(reservation)
                    processed += 1
                    if created:
                        stats["created"] += 1
                    else:
                        stats["updated"] += 1
                except Exception:  # noqa: BLE001, PERF203
                    ip = reservation.get("ip-address") or (reservation.get("ip-addresses") or ["?"])[0] or "?"
                    logger.debug(
                        "Failed to sync reservation %s from server %s",
                        ip,
                        server.name,
                        exc_info=True,
                    )
                    stats["errors"] += 1
            if next_from == 0 and next_source == 0:
                break
            from_index = next_from
            source_index = next_source
    except KeaException as exc:
        if exc.response.get("result") == 2:
            logger.debug(
                "Server %s (v%s): host_cmds hook not loaded — skipping reservation sync",
                server.name,
                version,
            )
            return False
        logger.warning("Failed to fetch reservations from server %s (v%s): %s", server.name, version, exc)
        stats["errors"] += 1
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unexpected error fetching reservations from server %s (v%s): %s", server.name, version, exc)
        stats["errors"] += 1
        return False

    logger.info("Server %s (v%s): synced %d reservations", server.name, version, processed)
    return True


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

    def run(self, *args: Any, **kwargs: Any) -> None:
        """Execute the sync across all servers."""
        from .models import Server

        config = _get_plugin_config()
        sync_leases = config.get("sync_leases_enabled", True)
        sync_reservations = config.get("sync_reservations_enabled", True)
        raw_max_leases = config.get("sync_max_leases_per_server", 50000)
        try:
            max_leases = int(raw_max_leases)
        except (TypeError, ValueError):
            self.logger.warning(
                "Invalid sync_max_leases_per_server=%r; falling back to 50000",
                raw_max_leases,
            )
            max_leases = 50000
        if max_leases < 0:
            self.logger.warning(
                "Negative sync_max_leases_per_server=%d is not allowed; using 0 (no cap)",
                max_leases,
            )
            max_leases = 0

        if not sync_leases and not sync_reservations:
            self.logger.info("Both sync_leases_enabled and sync_reservations_enabled are False — nothing to do.")
            return

        servers = list(Server.objects.all())
        if not servers:
            self.logger.info("No Kea servers configured — nothing to sync.")
            return

        self.logger.info("Starting Kea IPAM sync for %d server(s).", len(servers))
        total: dict[str, int] = {"created": 0, "updated": 0, "errors": 0}

        for server in servers:
            self.logger.debug("Syncing server: %s (pk=%s)", server.name, server.pk)
            server_stats: dict[str, int] = {"created": 0, "updated": 0, "errors": 0}

            try:
                self._sync_one_server(server, sync_leases, sync_reservations, max_leases, server_stats)
            except Exception as exc:  # noqa: BLE001, PERF203
                self.logger.error("Unhandled error syncing server %s: %s", server.name, exc, exc_info=True)
                server_stats["errors"] += 1

            self.logger.info(
                "Server %s: created=%d updated=%d errors=%d",
                server.name,
                server_stats["created"],
                server_stats["updated"],
                server_stats["errors"],
            )
            for key in total:
                total[key] += server_stats[key]

        self.logger.info(
            "Kea IPAM sync complete — servers=%d created=%d updated=%d errors=%d",
            len(servers),
            total["created"],
            total["updated"],
            total["errors"],
        )

    def _sync_one_server(
        self,
        server: Server,
        sync_leases: bool,
        sync_reservations: bool,
        max_leases: int,
        stats: dict[str, int],
    ) -> None:
        """Sync a single server's leases and reservations."""
        from .sync import cleanup_stale_ips_batch

        all_synced: list[dict] = []
        cleanup_safe = True
        for version, enabled in ((4, server.dhcp4), (6, server.dhcp6)):
            if not enabled:
                continue
            if sync_leases:
                cleanup_safe &= _sync_server_leases(
                    server, version, max_leases=max_leases, stats=stats, all_synced=all_synced
                )
            if sync_reservations:
                cleanup_safe &= _sync_server_reservations(server, version, stats=stats, all_synced=all_synced)

        if all_synced and stats["errors"] == 0 and cleanup_safe:
            cleanup_stale_ips_batch(all_synced)
        elif all_synced:
            self.logger.warning(
                "Server %s: skipping stale-IP cleanup (errors=%d, cleanup_safe=%s)",
                server.name,
                stats["errors"],
                cleanup_safe,
            )
