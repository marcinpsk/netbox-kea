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

    plugins_config = getattr(settings, "PLUGINS_CONFIG", {})
    if not isinstance(plugins_config, dict):
        logger.warning("PLUGINS_CONFIG is %s, expected dict — using defaults.", type(plugins_config).__name__)
        return {}
    config = plugins_config.get("netbox_kea", {})
    if not isinstance(config, dict):
        logger.warning("PLUGINS_CONFIG['netbox_kea'] is %s, expected dict — using defaults.", type(config).__name__)
        return {}
    return config


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
            logger.warning(
                "Server %s (v%s): host_cmds hook not loaded — reservation sync skipped",
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


def _sync_server_prefixes_and_ranges(
    server: Server,
    version: int,
    *,
    sync_prefixes: bool,
    sync_ip_ranges: bool,
    vrf=None,
    stats: dict[str, int],
) -> None:
    """Fetch subnets from *server* for *version* and sync to NetBox Prefixes / IP Ranges.

    Calls ``config-get`` once per version to obtain the full subnet list
    (including shared-network subnets).  For each subnet:
    - When *sync_prefixes* is ``True``, calls :func:`.sync.sync_subnet_to_netbox_prefix`.
    - When *sync_ip_ranges* is ``True``, calls :func:`.sync.sync_pool_to_netbox_ip_range`
      for each pool entry in the subnet.
    - *vrf* is forwarded to both sync functions (``None`` means global VRF).
    """
    from .kea import KeaException
    from .sync import sync_pool_to_netbox_ip_range, sync_subnet_to_netbox_prefix

    service = f"dhcp{version}"
    dhcp_key = f"Dhcp{version}"
    subnet_key = f"subnet{version}"

    try:
        client = server.get_client(version=version)
        config = client.command("config-get", service=[service])
    except (KeaException, Exception) as exc:  # noqa: BLE001
        logger.warning("Failed to fetch config-get from server %s (v%s): %s", server.name, version, exc)
        stats["errors"] += 1
        return

    try:
        raw_args = config[0].get("arguments") if config and isinstance(config[0], dict) else None
        conf = raw_args.get(dhcp_key, {}) if isinstance(raw_args, dict) else {}
    except Exception:  # noqa: BLE001
        logger.warning("Failed to parse config-get response from server %s (v%s)", server.name, version)
        stats["errors"] += 1
        return

    subnets: list[dict] = list(conf.get(subnet_key) or [])
    for sn in conf.get("shared-networks") or []:
        subnets.extend(sn.get(subnet_key) or [])

    logger.info("Server %s (v%s): found %d subnets for prefix/range sync", server.name, version, len(subnets))

    for subnet in subnets:
        subnet_cidr = subnet.get("subnet")
        if not subnet_cidr:
            continue

        if sync_prefixes:
            try:
                _, created = sync_subnet_to_netbox_prefix(subnet_cidr, vrf=vrf)
                if created:
                    stats["created"] += 1
                else:
                    stats["updated"] += 1
            except Exception:  # noqa: BLE001, PERF203
                logger.debug("Failed to sync prefix %s from server %s", subnet_cidr, server.name, exc_info=True)
                stats["errors"] += 1

        if sync_ip_ranges:
            for pool_entry in subnet.get("pools") or []:
                pool_str = pool_entry.get("pool") if isinstance(pool_entry, dict) else None
                if not pool_str:
                    continue
                try:
                    result = sync_pool_to_netbox_ip_range(pool_str, subnet_cidr, vrf=vrf)
                    if result is not None:
                        _, created = result
                        if created:
                            stats["created"] += 1
                        else:
                            stats["updated"] += 1
                except Exception:  # noqa: BLE001, PERF203
                    logger.debug("Failed to sync pool %s from server %s", pool_str, server.name, exc_info=True)
                    stats["errors"] += 1


def _sync_one_server(
    server: Server,
    sync_leases: bool,
    sync_reservations: bool,
    sync_prefixes: bool,
    sync_ip_ranges: bool,
    max_leases: int,
    stats: dict[str, int],
) -> None:
    """Sync a single server's leases, reservations, prefixes, and IP ranges."""
    from .sync import cleanup_stale_ips_batch

    all_synced: list[dict] = []
    # Cleanup is only safe when both sources contributed, otherwise we risk
    # removing IPs that exist in the source we didn't sync.
    cleanup_safe = sync_leases and sync_reservations
    for version, enabled in ((4, server.dhcp4), (6, server.dhcp6)):
        if not enabled:
            continue
        if sync_leases:
            cleanup_safe &= _sync_server_leases(
                server, version, max_leases=max_leases, stats=stats, all_synced=all_synced
            )
        if sync_reservations:
            cleanup_safe &= _sync_server_reservations(server, version, stats=stats, all_synced=all_synced)
        if sync_prefixes or sync_ip_ranges:
            _sync_server_prefixes_and_ranges(
                server,
                version,
                sync_prefixes=sync_prefixes,
                sync_ip_ranges=sync_ip_ranges,
                vrf=server.sync_vrf,
                stats=stats,
            )

    if all_synced and stats["errors"] == 0 and cleanup_safe:
        cleanup_stale_ips_batch(all_synced)
    elif all_synced:
        logger.warning(
            "Server %s: skipping stale-IP cleanup (errors=%d, cleanup_safe=%s)",
            server.name,
            stats["errors"],
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

            if not any([sync_leases, sync_reservations, sync_prefixes, sync_ip_ranges]):
                self.logger.info("All sync type flags are False — nothing to do.")
                return

            server_pk = kwargs.get("server_pk")
            if server_pk is not None:
                servers = list(Server.objects.filter(pk=server_pk))
            else:
                servers = list(Server.objects.all())

            if not servers:
                self.logger.info("No Kea servers configured — nothing to sync.")
                return

            self.logger.info("Starting Kea IPAM sync for %d server(s).", len(servers))
            total: dict[str, int] = {"created": 0, "updated": 0, "errors": 0}

            for server in servers:
                # In Run Now mode (server_pk provided), honour the explicit selection
                # and skip the per-server enabled check.
                if server_pk is None and not server.sync_enabled:
                    self.logger.info("Server %s: sync_enabled=False — skipping.", server.name)
                    continue

                # Per-server type overrides: AND global flag with server flag.
                effective_leases = sync_leases and server.sync_leases_enabled
                effective_reservations = sync_reservations and server.sync_reservations_enabled
                effective_prefixes = sync_prefixes and server.sync_prefixes_enabled
                effective_ip_ranges = sync_ip_ranges and server.sync_ip_ranges_enabled

                self.logger.debug("Syncing server: %s (pk=%s)", server.name, server.pk)
                server_stats: dict[str, int] = {"created": 0, "updated": 0, "errors": 0}

                try:
                    _sync_one_server(
                        server,
                        effective_leases,
                        effective_reservations,
                        effective_prefixes,
                        effective_ip_ranges,
                        max_leases,
                        server_stats,
                    )
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

                summary.append(
                    {
                        "name": server.name,
                        "pk": server.pk,
                        "created": server_stats["created"],
                        "updated": server_stats["updated"],
                        "errors": server_stats["errors"],
                    }
                )

            self.logger.info(
                "Kea IPAM sync complete — servers=%d created=%d updated=%d errors=%d",
                len(summary),
                total["created"],
                total["updated"],
                total["errors"],
            )
        finally:
            if not isinstance(self.job.data, dict):
                self.job.data = {}
            self.job.data["summary"] = summary
            self.job.save(update_fields=["data"])
