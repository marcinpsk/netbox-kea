---
status: accepted
date: 2026-09-29
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# netbox_kea rows stay in main, and a branch is read-only for the plugin

## Context

netbox-branching stages NetBox changes in a per-branch schema and replays them on merge. Kea is not
in NetBox: every Kea change the plugin makes is live when the command returns, so a branch cannot
stage it. The plugin's own rows (`Server`, `SyncConfig`, `KeaDhcpLink`) describe that live system.
A branch copy of a Server can only go stale: its connection fields fall behind main, and the Redis
snapshots are keyed by Server pk, so a branch read would fill the key that main reads.

## Decision

With netbox-branching installed and a branch active, the plugin reads and does not write:

- netbox_kea rows are main-only. A branching resolver answers `False` for `Server`, which is
  change-logged; the other two are plain models. A plugin model becomes branchable only when it has
  a writing foreign key (`CASCADE`, `SET_NULL`, `SET_DEFAULT`, `SET(...)`) to a branchable model
  outside the plugin; the ADR 0006 ownership link will be the first.
- `Server.sync_vrf` is `PROTECT`, so a VRF delete in a branch cannot null a main row through the
  branch connection's `search_path`.
- Every write through the plugin is refused: in plugin middleware (also for an unusable branch
  selection), in the Kea transport (`KeaClient.command()` takes a `KeaCommand` member with a read
  or write kind), and in `pre_save`/`pre_delete` receivers on the plugin models. Queryset
  `update()`, `bulk_create()`, `bulk_update()` and raw SQL on plugin models send no model signal,
  so they are outside this guarantee, as for every non-branchable model in NetBox.

The design, with its evidence, is `docs/design/netbox-branching.md`. Issue #231 tracks the
implementation in the increments that the design lists. Each refusal applies from the increment
that adds it.

## Consequences

- A branch shows live Kea data and main's Kea servers; other NetBox objects follow
  netbox-branching's routing.
- No plugin row exists in a branch, so merge, discard and revert need no plugin validator.
- A VRF that a Server syncs into can no longer be deleted, in main or in a branch, while the
  Server uses it.
- Full write support in branches would reverse this decision.
