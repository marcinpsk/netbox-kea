---
status: accepted
date: 2026-09-29
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Live Kea and control data remain main-only in branches

## Context

netbox-branching stages NetBox changes in a per-branch schema and replays them on merge. Kea is not
in NetBox: every Kea change the plugin makes is live when the command returns, so a branch cannot
stage it. Server and sync settings describe that live system.
A branch copy of a Server can only go stale: its connection fields fall behind main, and the Redis
snapshots are keyed by Server pk, so a branch read would fill the key that main reads.

## Decision

With netbox-branching installed and a branch active, live Kea and import operations remain read-only.
[ADR 0008](0008-dhcp-import-mappings-follow-target-branch-history.md) supersedes this decision only
for DHCP Import Mappings and their recovery lifecycle:

- Server, sync settings and IPAM ownership rows are main-only. A branching resolver answers
  `False` for those models. A delete in a branch reaches a main-only plugin model with a writing foreign key to a
  branchable model outside the plugin. Such a key must be a concrete `CASCADE` key, so the
  `pre_delete` receiver refuses the delete. The ADR 0006 ownership link is the first
  (`docs/design/ipam-ownership-branching.md`).
- `Server.sync_vrf` is `PROTECT`, so a VRF delete in a branch cannot null a main row through the
  branch connection's `search_path`.
- Every write through the plugin is refused: in plugin middleware (also for an unusable branch
  selection), in the Kea transport (`KeaClient.command()` takes a `KeaCommand` member with a read
  or write kind), and in `pre_save`/`pre_delete` receivers on main-only plugin models. Queryset
  `update()`, `bulk_create()`, `bulk_update()` and raw SQL on those main-only models send no model signal,
  so they are outside this guarantee, as for every non-branchable model in NetBox.

The design, with its evidence, is `docs/design/netbox-branching.md`. Issue #231 tracks the
implementation in the increments that the design lists. Each refusal applies from the increment
that adds it.

## Consequences

- A branch shows live Kea data and main's Kea servers; other NetBox objects follow
  netbox-branching's routing.
- DHCP Import Mappings exist in fresh branches and require the recovery boundaries in ADR 0008.
- A VRF that a Server syncs into can no longer be deleted, in main or in a branch, while the
  Server uses it.
- Full write support in branches would reverse this decision.
