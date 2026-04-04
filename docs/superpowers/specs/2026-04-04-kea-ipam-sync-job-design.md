<!--
SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Design: Periodic Kea→NetBox IPAM Sync (Issue #31)

**Date:** 2026-04-04
**Status:** Approved
**Author:** Marcin Zieba / Copilot

---

## Problem

The current approach for syncing Kea DHCP leases/reservations to NetBox IPAM relies on Kea hook scripts calling external scripts/APIs on each lease event. This is noisy, fragile, and requires external infrastructure. The plugin already contains all the sync logic (`sync.py`); it just needs scheduling.

---

## Architecture

```text
rqworker startup
  → registry['system_jobs'] has KeaIpamSyncJob
  → enqueue_once(interval=sync_interval_minutes) schedules first run

KeaIpamSyncJob.run()
  → advisory lock (prevent concurrent runs)
  → for each Server:
      → get_client(version=4|6)
      → lease_get_all() → sync_lease_to_netbox(cleanup=False) per lease
      → reservation_get_page() → sync_reservation_to_netbox(cleanup=False) per reservation
      → cleanup_stale_ips_batch(all_synced) once per server
  → summary log via self.logger
```

---

## Components

### `netbox_kea/jobs.py` (new file)

- `_interval` module-level variable: read from `PLUGINS_CONFIG["netbox_kea"]["sync_interval_minutes"]` using `get_plugin_config()`. Evaluated at import time, which happens inside `AppConfig.ready()` when Django settings are fully available.
- `@system_job(interval=_interval)` decorator registers `KeaIpamSyncJob` in `registry['system_jobs']`.
- `KeaIpamSyncJob(JobRunner)`:
  - `Meta.name = "Kea IPAM Sync"`
  - `run()` acquires an advisory lock via `django_pglocks.advisory_lock` to prevent overlapping runs
  - Per-server loop: catches all exceptions to isolate failures
  - Leases: gated by `sync_leases_enabled` setting; respects `sync_max_leases_per_server`; warns when truncated
  - Reservations: gated by `sync_reservations_enabled` setting; gracefully skips result=2 (host_cmds absent)
  - Single `cleanup_stale_ips_batch()` call per server at the end
  - Summary via `self.logger.info()`

### `netbox_kea/__init__.py` (updated)

- `AppConfig.ready()`: imports `jobs` module (triggers `@system_job` registration)
- `default_settings` additions:
  - `sync_interval_minutes: 5` — interval between sync runs (minutes)
  - `sync_leases_enabled: True` — whether to sync active leases
  - `sync_reservations_enabled: True` — whether to sync reservations
  - `sync_max_leases_per_server: 50000` — hard cap to protect against enormous lease tables

### UI Editability

NetBox's `JobRunner.handle()` re-schedules itself after each run using `job.interval` (from the DB Job object, not the registry). So:
- **At startup**: `rqworker` reads `registry['system_jobs']` and calls `enqueue_once(interval=_interval)`, using the value from `PLUGINS_CONFIG`.
- **After first run**: the Job object exists in the DB. A user can edit its `interval` field via NetBox Admin → Jobs, and the new interval takes effect on the next reschedule.
- Both paths work naturally with the existing NetBox infrastructure.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `sync_interval_minutes` | `5` | How often the background job runs (minutes) |
| `sync_leases_enabled` | `True` | Sync active DHCP leases to IPAddress (status=active) |
| `sync_reservations_enabled` | `True` | Sync reservations to IPAddress (status=reserved) |
| `sync_max_leases_per_server` | `50000` | Cap on leases fetched per server per run |
| `stale_ip_cleanup` | `"remove"` | What to do with stale IPs: `remove`, `deprecate`, `none` |

---

## Tests

Unit tests in `netbox_kea/tests/test_jobs.py` (mock-based, no Docker):

| Test | What it verifies |
|---|---|
| `test_run_syncs_leases_and_reservations` | Normal path: leases + reservations synced, counts logged |
| `test_run_skips_leases_when_disabled` | `sync_leases_enabled=False` → `lease_get_all` never called |
| `test_run_skips_reservations_when_disabled` | `sync_reservations_enabled=False` → `reservation_get_page` never called |
| `test_run_isolates_per_server_errors` | Exception on server 1 → server 2 still synced |
| `test_run_logs_truncation_warning` | `lease_get_all` returns `truncated=True` → warning logged |
| `test_run_skips_host_cmds_absent` | `reservation_get_page` returns result=2 → no exception, just skip |
| `test_run_v4_only_server` | `dhcp4=True, dhcp6=False` → only v4 client called |
| `test_run_v6_only_server` | `dhcp4=False, dhcp6=True` → only v6 client called |

---

## Error Handling

- `get_client()` failure (ValueError, RequestException) → log + skip server
- `lease_get_all()` / `reservation_get_page()` failure (KeaException) → log + skip server
- Individual `sync_*_to_netbox()` failure → log + increment error counter, continue
- Job-level unhandled exception → let `JobRunner.handle()` mark job as errored + re-schedule

---

## DNS Integration

No additional work needed. `sync_lease_to_netbox()` already sets `dns_name` on the IPAddress from the Kea hostname. If `netbox-dns` with `IPAMDNSsync` is installed, it auto-creates A/AAAA/PTR records via Django signals.

---

## Out of Scope

- Prometheus metrics (Phase 5 nice-to-have)
- Per-server "last sync time" UI widget (can be viewed via NetBox Jobs list)
- Management command wrapper for `manage.py sync_kea_leases` (the Job is triggerable via NetBox UI)
