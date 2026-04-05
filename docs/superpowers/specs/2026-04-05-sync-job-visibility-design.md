<!--
SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Design: Sync Job Visibility & Control UI

**Date:** 2026-04-05
**Status:** Approved
**Author:** Marcin Zieba / Copilot
**Relates to:** Issue #31 (periodic sync job — already implemented in `feat/rq-jobs`)
**Builds on:** `docs/superpowers/specs/2026-04-04-kea-ipam-sync-job-design.md`

---

## Problem

The background Kea→NetBox IPAM sync job (`KeaIpamSyncJob`) is fully functional but entirely invisible. There is no UI to:

- See whether the sync is enabled or disabled (global or per-server)
- View the last sync result (counts, errors, timestamp)
- Change the sync interval without editing `settings.py`
- Trigger a manual sync from the browser
- Enable/disable sync for specific servers without editing code

The only way to observe it today is through the NetBox Admin → Jobs panel, which is several clicks away and not discoverable from the plugin.

---

## Approved Design (Option C)

Two complementary surfaces:

1. **Per-server "Sync Status" tab** — scoped to one server, shows last run stats, enable/disable toggle for this server, Run Now, mini log + link to NetBox native Jobs view.
2. **Plugin-level "Sync Jobs" page** — cross-server overview table, global kill-switch, DB-stored interval editor (overrides `PLUGINS_CONFIG` default), Run Now per row.

---

## Architecture

```text
SyncConfig (singleton DB model)
  interval_minutes   → overrides PLUGINS_CONFIG default at runtime
  sync_enabled       → global kill-switch

Server.sync_enabled  → per-server enable/disable (new BooleanField)

KeaIpamSyncJob.run()
  → reads SyncConfig (or falls back to PLUGINS_CONFIG)
  → skips server if SyncConfig.sync_enabled=False OR server.sync_enabled=False

Sync Status tab (per-server view)
  → queries Job.objects.filter(object_type=Server, object_id=pk)
  → shows most recent Job result (status, timestamps, data counts)
  → per-server sync_enabled toggle (inline PATCH or edit redirect)
  → "Run Now" → enqueues KeaIpamSyncJob immediately

Sync Jobs page (plugin-level)
  → lists all Servers with latest Job result per server
  → SyncConfig form (interval + global enabled)
  → "Run Now" per row
```

---

## New Model: `SyncConfig`

Singleton pattern — at most one row. Enforced by `pk=1` in `save()`.

```python
class SyncConfig(models.Model):
    interval_minutes = models.PositiveIntegerField(default=5)
    sync_enabled = models.BooleanField(default=True)

    class Meta:
        app_label = "netbox_kea"

    @classmethod
    def get(cls) -> "SyncConfig":
        obj, _ = cls.objects.get_or_create(pk=1, defaults={"interval_minutes": 5, "sync_enabled": True})
        return obj
```

- Not a `NetBoxModel` (no tags/changelog needed — it's a config object, not an asset).
- Requires a DB migration.
- `__init__.py` `ready()` hook: after registering the job, call `SyncConfig.get()` to read `interval_minutes` and apply it to the live registry entry. This makes the DB value take effect at worker startup without restarting Django.

---

## Server Model Change

Add one field to `Server`:

```python
sync_enabled = models.BooleanField(
    verbose_name="IPAM Sync Enabled",
    default=True,
    help_text="Include this server in the periodic Kea→NetBox IPAM sync job.",
)
```

- Requires a DB migration.
- Shown on the Server edit form (existing `ServerForm`).
- `KeaIpamSyncJob._sync_one_server()` checks `server.sync_enabled` before processing.

---

## Surface 1: Per-Server "Sync Status" Tab

**URL:** `/plugins/kea/servers/{pk}/sync-status/`
**Tab label:** "Sync Status"
**Visibility:** Always shown (tab itself; content degrades if Job records absent).

### Content

| Section | Details |
|---|---|
| **Last Sync** card | Most recent `Job` object for this server: status badge (OK / Error / Running / Never), completed_at, duration |
| **Counts** | Created, updated, unchanged, errors — read from `job.data["summary"]` |
| **Server enable toggle** | Inline form that POSTs to update `server.sync_enabled`; shows current value |
| **Run Now** button | POST to trigger `KeaIpamSyncJob` immediately for this server only |
| **Job history** mini-table | Last 5 `Job` rows (timestamp, status, counts); link "→ View all sync jobs" → `/core/jobs/?object_type=netbox_kea.server&object_id={pk}` |

### Job result data contract

`KeaIpamSyncJob` already writes a summary via `self.logger`. We extend it to also store structured data in the Job's `data` field:

```python
self.data["summary"] = {
    "servers": [
        {
            "name": server.name,
            "created": n,
            "updated": n,
            "unchanged": n,
            "errors": n,
        }
    ]
}
```

The view reads this to render counts without re-parsing log strings.

---

## Surface 2: Plugin-Level "Sync Jobs" Page

**URL:** `/plugins/kea/sync-jobs/`
**Menu:** Kea → Sync Jobs (new nav item)

### Content

| Section | Details |
|---|---|
| **Global config form** | `SyncConfig` form: interval (minutes input), global kill-switch checkbox, Save button |
| **Server summary table** | One row per `Server`: name, per-server enabled, last sync timestamp, status badge, created/updated/errors counts, Run Now button |
| **"Run All Now"** button | Enqueues one `KeaIpamSyncJob` covering all servers (same as scheduled run) |

The counts come from the most recent `Job` for each server (one extra query per page using `Job.objects.filter(object_type=..., object_id__in=[...]).order_by("object_id", "-created").distinct("object_id")`).

---

## "Run Now" Implementation

`KeaIpamSyncJob` is a `system_job` with `interval`. NetBox's `JobRunner` supports `enqueue_once()` for deduplication. "Run Now" calls:

```python
KeaIpamSyncJob.enqueue(server_pk=server.pk)   # per-server variant
KeaIpamSyncJob.enqueue()                       # all-servers variant
```

The job accepts an optional `server_pk` kwarg (integer). When set, it syncs only that server. When absent, it syncs all (normal scheduled path).

---

## Interval Change at Runtime

When the user saves a new `interval_minutes` via the Sync Jobs page:
1. `SyncConfig` DB record is updated.
2. The view calls `KeaIpamSyncJob.enqueue_once(interval=new_interval)` to re-schedule with the new interval. This replaces the existing scheduled job in the RQ queue.
3. On next `rqworker` restart, `ready()` reads `SyncConfig` and re-applies the interval again.

---

## URL Routing

```python
# In urls.py, before include(get_model_urls(...))
path("sync-jobs/", views.SyncJobsView.as_view(), name="sync_jobs"),
path("servers/<int:pk>/sync-status/", views.ServerSyncStatusView.as_view(), name="server_sync_status"),
path("servers/<int:pk>/sync-now/", views.ServerSyncNowView.as_view(), name="server_sync_now"),
```

---

## Views

| View | Class | Method | Notes |
|---|---|---|---|
| `SyncJobsView` | `View` | GET + POST | GET renders table + form; POST saves `SyncConfig` and re-schedules |
| `ServerSyncStatusView` | `ObjectView` | GET | Reads `Job` records, renders tab content |
| `ServerSyncNowView` | `View` | POST only | Enqueues job for one server, redirects to sync status tab |
| `ServerSyncToggleView` | `View` | POST only | Toggles `server.sync_enabled`, redirects back |

---

## Templates

- `netbox_kea/sync_jobs.html` — plugin-level page
- `netbox_kea/server_sync_status.html` — per-server tab content

---

## Navigation

Add `"Sync Jobs"` to the plugin's `PluginMenu` items in `__init__.py`:

```python
PluginMenuItem(
    link="plugins:netbox_kea:sync_jobs",
    link_text="Sync Jobs",
    permissions=["netbox_kea.view_server"],
),
```

---

## Permissions

No new custom permissions needed. Reuse:
- `netbox_kea.view_server` → view sync status tab and jobs page
- `netbox_kea.change_server` → toggle per-server sync_enabled, trigger Run Now
- `netbox_kea.change_syncconfig` (auto-created by Django) → edit `SyncConfig` (interval + global kill-switch); assign to staff users

---

## `KeaIpamSyncJob` Changes

| Change | Details |
|---|---|
| Accept `server_pk` kwarg | When set, only sync that server (for "Run Now per server") |
| Check `SyncConfig.sync_enabled` | Skip entire run if global kill-switch is off |
| Check `server.sync_enabled` | Skip individual server if per-server flag is off |
| Write `self.data["summary"]` | Structured counts per server for the UI |
| Read interval from `SyncConfig` at startup | `ready()` hook applies DB interval after job registration |

---

## Migrations

1. `000X_add_syncconfig.py` — create `SyncConfig` table (interval_minutes, sync_enabled)
2. `000Y_server_sync_enabled.py` — add `sync_enabled` BooleanField to `Server`

---

## Tests

All unit tests in `netbox_kea/tests/` (no Docker required).

| Test file | Coverage |
|---|---|
| `test_jobs.py` | `server_pk` kwarg path, `SyncConfig` kill-switch, per-server `sync_enabled` skip, `data["summary"]` written correctly |
| `test_views_sync_jobs.py` (new) | GET/POST `SyncJobsView`, `ServerSyncStatusView` with mock Jobs, `ServerSyncNowView` enqueue, permission gates |
| `test_models.py` | `SyncConfig.get()` singleton, `Server.sync_enabled` default |

---

## Out of Scope

- Per-server sync interval (global interval only)
- Prometheus / OTEL metrics
- Webhook on sync completion
- Bulk enable/disable from the table (future enhancement)
