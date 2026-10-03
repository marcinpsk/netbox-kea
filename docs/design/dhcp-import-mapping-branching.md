---
status: proposed
date: 2026-10-03
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# DHCP import mappings in branches

This interview examines whether the mapping from a Kea source identity to an imported NetBox DHCP object
should follow that object's branch history. The complete design needs shared agreement before implementation.

## Existing contract and problem

ADR 0007 keeps all plugin rows on main and refuses plugin writes while a branch is active. The proposal
reconsiders that contract for KeaDhcpLink. It does not propose changing Server settings or live Kea operations.

The existing recovery regression imports a DHCP target, deletes it in a branch, merges, reverts and imports
again. Revert restores the target without its mapping. The subsequent Subnet import attempts creation and
fails because the restored target already has that name.

Making the mapping branchable requires change logging and cleanup in the target's branch. Source inspection
also identifies replay-order, concurrent-main-import and old-branch-schema questions. The isolated prototype
has now demonstrated the squash recovery lifecycle. It is not a production implementation.

## Language

DHCP Import Mapping names the source-to-imported-target relationship represented by KeaDhcpLink. The
glossary's DHCP Link remains a DHCP selection domain. CONTEXT.md holds the domain definitions.

## Accepted decisions

- Mapping changes in branches follow the imported target's lifecycle automatically. This change adds no
  manual mapping editor. Live Kea writes and DHCP imports remain on main.
- Affected operations require a fresh branch created after the feature is installed. This change does not
  retrofit the mapping table and history into existing branches.
- A revert that conflicts with a newer main mapping fails as a whole and preserves newer main state. It
  explains the conflict. This change adds no workflow for selecting a winning mapping.
- Apply the lifecycle to both currently mapped target kinds, Subnets and Global Reservations, for IPv4
  and IPv6. In-Subnet Reservations continue to have no separate mapping.
- Mappings already lost under the previous policy require explicit operator repair outside this feature.
  Preserve existing objects. Do not infer ownership from matching names.
- A merge that would delete a newer main mapping absent from the branch's reversible history fails as
  a whole. Preserve main state and explain that the operator must recreate the branch from current main.
- Require the dependency-aware squash strategy for branches with affected mapping deletions. Real tests of
  installed iterative replay reached a missing target while restoring its mapping. Refuse an unsupported
  strategy before changes and explain the required choice. Do not silently switch the branch's strategy.
- Older branches retain ordinary read pages that do not need mappings. Their DHCP Plugin tab explains
  that mapping information requires a fresh branch. It does not read main mappings as branch data.
- If configuration excludes a mapping or its target from branching, refuse affected operations with a
  clear reason. Keep unrelated plugin features available. Do not reject the entire configuration at startup.

## Design tree

Three rounds resolved all nine policy questions. Automatic lifecycle scope excludes manual edit surfaces,
branch-local imports and a new conflict-resolution workflow. Fresh-branch support excludes old-schema
upgrades and historical reconstruction. The conflict rules preserve newer main state. The final round
resolved conditional strategy restrictions, old-branch read behavior and configuration exclusions.

The policy frontier is empty. The operator confirmed the complete shared understanding. The strategy
policy is now resolved by real dependency-ordering evidence: affected deletions require squash. No runtime
success is inferred from an interview answer. If implementation exposes a new product trade-off, reopen its
affected decision explicitly.

## Verified import surface

The Server's Sync to DHCP plugin action calls the mapping importer. The periodic IPAM synchronization job
does not invoke it. A concurrent main mapping import can therefore be a main UI action or a script that
calls the importer.

The current importer creates mappings for Subnets and Global Reservations. It matches In-Subnet
Reservations within their parent Subnet without a separate mapping. Mapping source identities retain the
Server and address family. The existing model constrains both source and target uniqueness.

There is no current manual mapping UI, REST endpoint or GraphQL type. The existing DHCP Plugin tab reads
mappings for import status and drift.

The drift reader uses an unqualified mapping queryset. If a branch lacks the newly branchable table, the
framework's branch-then-main search path can resolve that query to main. Ordinary Server, live Kea and job
read pages do not query these mappings. A schema-aware mapping read is therefore distinct from retaining
those ordinary read pages.

Configured model exemptions apply after a positive resolver result for the mapping and supported targets.
The feature must account for those exclusions instead of assuming that its resolver always controls routing.

## Runtime questions

- Does tracked target deletion record complete mapping history in both instance and queryset deletion?
- Does the installed dependency-aware replay restore target and mapping identities before reimport?
- Can a main import race merge or revert without deleting an unrecorded mapping or overwriting newer state?
- Can an old branch query accidentally reach the main mapping table, and can affected operations fail
  before that happens?
- How do optional DHCP installation and exempt target models affect the supported lifecycle?

The prototype used real branch operations and PostgreSQL. Interview answers alone cannot establish runtime
behavior. The original recovery regression failed before the experiment. The recorded evidence follows.

## Prototype evidence

The throwaway branch is `prototype/dhcp-import-mappings`, based on the refreshed API-action PR head. Its
primary-source record and mobile illustration are under `docs/prototypes/dhcp-import-mappings` on that
branch. NetBox 4.7.0, netbox-branching 1.2.1 and netbox-plugin-dhcp 0.2.0 were used.

- Main-only baseline: one recovery regression failed because the restored Subnet had no mapping.
- Unguarded branchability: six probes and eight subtests passed. Squash restored both original primary keys
  and the source association for both target kinds, both families and both deletion paths. Reimport reused
  the target. Iterative replay reached a missing target. Old-schema raw queries reached main. Synchronous
  AppliedChange provenance was observed inside main's deleting transaction.
- Safety requirements before guards: two regressions failed. A real main import after preflight attached a
  mapping that merge then deleted. An old-schema branch deletion reached main's mapping table.
- With prototype transaction and schema guards: 14 tests and 16 subtests passed. The unsafe cases refused
  and preserved main. Six lifecycle test methods repeat through inheritance; the count is not 14 distinct
  scenarios.
- Preserved iterative control: one focused test passed by asserting the native Subnet.DoesNotExist during
  revert, with only the strategy refusal disconnected. Actual output was "Subnet matching query does not
  exist." The separate guarded test verifies refusal before mutation.

This establishes bounded feasibility, not complete concurrency or compatibility safety. Newer-main revert
conflicts, all participating writers and lock orders, observation-only imports, configured exclusions,
public HTTP messages, dry runs and optional-plugin configurations remain production validation gates.
Do not merge the prototype as an implementation. Raw ORM fallback remains an intentional diagnostic control;
the complete public mapping read boundary still needs implementation and behavioral coverage.

## Observable acceptance conditions

- A branch deletion removes its target and mapping locally while main retains both. Discard leaves main
  untouched. Cover instance and queryset deletion for both supported target kinds and both address families.
- Merge removes the matching main target and mapping. Revert restores their original primary keys and
  source-to-target association. A subsequent real import updates the restored target without creating a
  duplicate or failing target-name uniqueness.
- Mapping source identities remain distinct across Servers and address families. Repeated import preserves
  the association. A timestamp-only observation does not become a conflict over source or target identity.
- Competing PostgreSQL transactions for main import and merge or revert cannot delete an unrecorded mapping
  or overwrite newer state. A refusal rolls back the complete operation. A missing main Server or conflicting
  source or target identity cannot be restored silently.
- An old branch can read independent plugin pages. Its mapping-dependent view reports unavailable branch
  information. Affected reads and changes never fall through to main's mapping table.
- Optional DHCP or branching absence preserves ordinary supported behavior. Exempt mapping or target models
  cause an explicit refusal of affected operations without disabling unrelated features.
- Exercise both replay strategies. Support only behavior proved safe. Where a strategy must be refused,
  verify the refusal occurs before mutation and leaves the selected strategy unchanged.
- Preserve the existing refusal of live Kea writes, branch imports, Server changes, settings changes and
  IPAM ownership mutations. Exercise dry-run replay as well as committed operations.

## Next phase

Capture the isolated prototype and its limitations as a primary source. Publish the agreed specification.
Build one complete vertical implementation slice that closes the remaining validation gates. Routing,
history, cleanup and replay safety must land together; a partial production change would be unsafe.

ADR 0008 records the proposed mapping exception. ADR 0007 remains the current runtime contract until that
exception is accepted and implemented. Production runtime code remains unchanged by this design phase.
