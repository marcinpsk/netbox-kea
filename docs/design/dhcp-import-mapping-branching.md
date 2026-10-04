---
status: accepted
date: 2026-10-03
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# DHCP Import Mappings in branches

A DHCP Import Mapping associates a Kea source identity with an imported NetBox DHCP object.
The mapping must remain recoverable when a branch deletes, merges and restores that object.
[ADR 0008](../adr/0008-dhcp-import-mappings-follow-target-branch-history.md) records this exception
to the main-only policy. CONTEXT.md defines the domain terms.

## Contract

Only `KeaDhcpLink` gains branchability. Imported Subnets and Global Reservations already follow
native DHCP-plugin routing. The lifecycle applies to IPv4 and IPv6, instance deletion and queryset
deletion. In-Subnet Reservations continue to have no separate mapping.

| Operation | Target and mapping outcome |
| --- | --- |
| Create a fresh branch | Copy the imported target and its mapping into the branch |
| Delete the branch target | Delete its mapping in the same target alias; main retains both |
| Discard the branch | Preserve main's target and mapping |
| Squash merge | Delete the matching main target and mapping with native reversible history |
| Revert | Restore the original target and mapping primary keys and source association |
| Reimport on main | Update the restored target without creating a duplicate |

Server, sync settings, IPAM ownership and live Kea operations retain ADR 0007's main-only policy.
Imports remain main-only. The feature adds no manual mapping editor or branch import.

A source identity includes the Server and address family, then either the Kea subnet ID or the
Global Reservation Identity. Source and target uniqueness constraints remain enforced. A repeated
observation updates the observation timestamp without a semantic mapping-history change. Genuine
association changes retain native before and after history.

## Recovery safety

Merge and revert validate all affected mapping and target history before replay mutates main.
The native action remains atomic: any refusal rolls back the complete action, its status and its
history. Native dry runs retain their rollback behavior.

Refuse the whole action when:

- A main mapping has no matching reversible branch history.
- Main mapping or target data has changed since the state that replay expects.
- A primary key identifies another generation of a mapping or target.
- Required history fields or a usable creation generation are missing.
- The source Server or a required target type is unavailable.
- Restoring an association would violate source or target uniqueness.
- Configuration excludes the mapping or an affected target from branching.
- The branch lacks a complete mapping table or an affected target table.
- The selected replay strategy cannot restore the dependencies.
- The branch combines Tag changes with replay of a mapped DHCP target.
- A required existing Tag is missing, renamed or replaced on main.

Undo of a branch-created Global Reservation is also refused if main import adopted and mapped it
after merge. A branch cannot delete that newer association through an unrecorded target deletion.
Timestamp-only mapping observations do not conflict with an otherwise unchanged association.

Affected actions require the dependency-aware squash strategy. Refuse iterative replay before any
mutation, explain the squash requirement and preserve the selected strategy. Strategy selection
remains an explicit operator choice.

Tag changes must be separate from mapped DHCP target replay. This conservative rule refuses even
unrelated Tag changes in the same branch. Recovery uses existing unchanged Tags. It does not
interpret rename history or create replacement Tags.

Shared writer coordination starts before importer lookup, native replay selection, relationship
selection and deletion collection. It includes ordinary target and mapping ORM writers and the
relationship and parent-delete paths that can change protected target state. Native synchronous
history establishes replay provenance in the changing transaction. Operation context and native
request deletion history are restored on both success and error. Branch status alone does not
identify replay.

The detailed boundary registry, transaction ownership and contention rules are in
[the transaction design](dhcp-import-mapping-transactions.md). Keep its proof obligations when
changing a writer, relation or native branch adapter.

## Operator recovery

Create branches after installing the mapping migration. Older branches keep independent Server
and live Kea read pages. Their DHCP Plugin tab explains that mappings require a fresh branch;
reads and writes cannot fall through to main's mapping table.
This also applies to a branch created before the optional DHCP plugin was enabled.
An affected target table must exist in the branch schema before mapping access or target deletion.

For a recovery refusal:

1. Read the reason before retrying. The complete action changed nothing.
2. If another transaction owns metadata coordination, retry after that transaction finishes.
3. If the strategy is unsupported, select squash and retry.
4. If the schema is old or history is incomplete, recreate the branch from current main.
5. If a model is exempt, remove the affected exemption and create a fresh branch.
6. If main changed or an identity conflicts, preserve main and reconcile the intended change in a
   fresh branch. The feature provides no workflow for choosing a winning mapping.
7. If the branch includes Tag changes, apply the Tag work separately on main or in a Tag-only
   branch. Create a fresh branch for the DHCP changes. Preserve any newer main Tag state.

Configuration exclusions cause scoped refusals. They do not disable unrelated plugin reads or
reject startup. Absence of the optional DHCP or branching plugin preserves ordinary supported
behavior.

The feature does not retrofit branch schemas or history and does not reconstruct mappings already
lost under the previous policy. Preserve the existing target. Verify its Server, family and source
identity from trusted records before an explicit, audited repair on main. A matching target name
does not establish provenance.

## Validation contract

Use real import and native branch operations against PostgreSQL, and public HTTP requests for
operator messages. Stub external Kea transport only. The recovery suite must cover both target
kinds, families and deletion paths, discard, dry runs, complete rollback and retry. It must also
exercise newer main state, adopted branch-created targets, missing dependencies, reused identities,
incomplete history, uniqueness conflicts, observation-only import, old schemas and exemptions.

Separate PostgreSQL transactions must force imports after preflight and during merge or revert.
Test both lock orderings and the participating scalar, relationship and collector writers. Keep
known-good controls so a refusal test cannot pass merely because every action is refused.

Run branching-present and branching-absent gates, optional DHCP profiles and an independent
adversarial review before publication. The existing branch guards must continue to pin the sole
mapping exception and all remaining main-only behavior.

## Decision and prototype evidence

The operator accepted nine policy decisions and confirmed the complete agreement. The published
[specification](https://github.com/marcinpsk/netbox-kea/issues/296) and its implementation ticket
require the complete lifecycle and safety rules in one change.

The throwaway prototype at `a93d30b42a4555d353df9cc6c4f6a3ce40b074b6` demonstrated the squash
lifecycle for both kinds and families with real NetBox 4.7.0, netbox-branching 1.2.1 and
netbox-plugin-dhcp 0.2.0. Installed iterative replay failed to restore its mapping dependency.
Two unsafe controls, late main import and old-schema deletion, failed before prototype guards and
passed with them. The guarded prototype ran 14 tests and 16 subtests; inherited tests repeat six
lifecycle methods, so this is not 14 distinct scenarios.

Those results established feasibility, not complete concurrency or compatibility safety. The
prototype was not merged. Production safety follows the validation contract above and the
transaction design's independent ratification and behavioral proof obligations.
