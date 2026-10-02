<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# DHCP import ownership

## Problem and acceptance

The DHCP import creates shared IPAM objects without their Server ownership links.
The IPAM Reconciliation module must own each link and its fact comparison.
The import must consume returned objects rather than repeat address lookups.
All Reservations of one snapshot must participate in one claim.
A complete delegated-prefix phase can drop its own last Prefix link.
An incomplete read or failed import must preserve stale links.
Prefixes are never deleted. Deprecation remains optional and respects DHCP references.
The existing failed Reservation write must leave no newly created delegated Prefix.

The test seam is the existing import interface, with real DHCP and IPAM models.
Only the external Kea HTTP transport is stubbed.
Typed records and an AST regression guard prevent private synchronization imports.

## Candidate shapes

1. Extend `claim` with typed Subnet and Pool records and typed result maps.
   Use the existing network ownership implementation for these records.
2. Add a separate network claim interface beside the address claim interface.
   This keeps the address signature narrow but gives the adapter another ownership interface.

The coordinator's initial design selects the first shape.
Use homogeneous batches for each source and group each batch before any ownership write.
Existing address callers retain their result interface.
The import consumes Prefix and IP Range results alongside address results.
All Prefix sources use one object identity lock, regardless of source.

Save DHCP Reservation rows before the delegated-prefix phase creates their Prefixes.
Clear dropped Prefix references in those row saves.
Run the phase with the successfully imported records and mark it incomplete after any skipped or failed row.
Attach its returned Prefixes after reconciliation, in the same transaction as that phase.
A failed attachment rolls back the phase and its cleanup.
Take the phase cutoff before the Reservation network read.
The import takes a Reservation observation instead of a bare snapshot.
Every caller supplies the cutoff taken before its read, including test fixtures.
There is no missing-cutoff fallback.
Expose the existing MAC synchronization through one public helper, without copying it.
Return curated Global addresses with an explicit conflict outcome so the import keeps its warning.

Revision r2 closes the required-MAC transaction gap found in r1.
Group every Reservation of the snapshot before any write, including records whose MAC write can fail.
For each grouped address, apply its ownership report and resolve its required MAC rows in the same savepoint.
If a required MAC cannot be resolved, refuse that row and roll back its IP and link changes.
The returned address outcome is an error, and unrelated addresses continue.
The import must not attach an error result.
Each successful address result returns its resolved MAC rows, keyed by hardware address and hostname.
The import checks every address outcome before it attaches the exact returned MAC row.
This closes the late lookup window where a duplicate MAC could invalidate an already successful claim.
Other DHCP row failures do not roll back previously successful address claims.
Addressless and Global hardware Reservations still resolve their MAC for the DHCP row.
Their row save fails if the required MAC cannot be resolved.

## Review record

The independent designer receives the factual problem and source pointers only.
It runs Astra at medium reasoning in a fresh, read-only context.
The coordinator drafts the initial design before comparing the independent proposal.
Both designers select typed network claim records and one delegated-prefix phase.
The independent design proposes homogeneous source batches; the merged design uses them.
Separate network claim functions would add another ownership interface without removing any policy.
Callbacks from reconciliation into DHCP writes would couple ownership to the optional plugin.
The merged design instead saves DHCP rows first and uses one transaction for the delegated-prefix phase and attachments.
Both designs require a shared Prefix lock and a cutoff taken before the network read.
The merged design requires a typed observation from every caller, rather than accepting an unsafe late cutoff.

| Decision | Coordinator | Independent designer | Evidence and disposition | Consequence |
| --- | --- | --- | --- | --- |
| Ownership interface | Extend `claim` | Extend `claim` | Existing network ownership code supplies the policy. Keep one interface. | Typed Prefix and range results replace importer lookups. |
| Network batch | Group mixed records by source | One homogeneous source per call | Existing claim batches have one source. Retain that invariant. | Submit one Subnet batch and one Pool batch. |
| Delegated writes | Save DHCP rows first | Save DHCP rows first | The name-collision regression forbids an unused new Prefix. | Reconcile successful rows and attach Prefixes in one transaction. |
| Cleanup cutoff | Explicit observation | Explicit observation | Existing cleanup uses a sequence cutoff before reads. Require the observation. | No late-cutoff fallback can remove a concurrent confirmation. |
| Prefix lock | One object identity | One object identity | Both Subnet and delegated claims can create the same Prefix. | Normalize by object type rather than source. |

The r1 reviewer found that batched address claims committed before required MAC resolution.
The current helper can return no MAC after a database failure.
That would leave an IP behind when the DHCP import rejected the Reservation.
The coordinator verified the claim savepoint and the existing MAC rollback regression.
Revision r2 moves required MAC resolution into each address claim savepoint.
The named candidate is revision r2, covering the import and the reconciliation extension.
An independent Astra high reviewer will ratify it before implementation.

## Section 0: refuted claims

None yet.

## Status

The independent Astra high reviewer ratified r2 for the import and reconciliation extension.
The required-MAC savepoint change closes the r1 blocker.
Implementation must prove failed MAC rollback with healthy sibling continuation,
whole-snapshot disagreement, correct ownership sources, shared Prefix locks,
pre-read cutoffs, complete and incomplete delegated cleanup, current reference guards,
and delegated attachment rollback through real-model regressions.
The implementation extends `claim` with `SubnetClaim` and `PoolClaim` batches.
`ClaimResult` returns the Prefix and IP Range objects as well as addresses.
`ReservationObservation` pairs each snapshot with its pre-read cutoff.
`DelegatedPrefixPhase` uses that cutoff and the imported records' completeness.
The import no longer imports private synchronization helpers.

The first real-model regression failed against the previous import because all three ownership sources were absent.
It passes through the new import interface.
Focused regressions prove snapshot disagreement, Server VRF scope, optional deprecation,
failed and incomplete cleanup, required-MAC rollback with a healthy sibling,
concurrent confirmation protection and delegated attachment rollback.
A separate-connection concurrency test proves Subnet and delegated claims create one shared Prefix.
An AST regression prevents private synchronization imports from returning to the adapter.
A failed delegated claim preserves the Reservation's still-reported Prefix attachments.
The import warns about the retained attachments and imports healthy siblings.
A real duplicate-Prefix regression reproduced attachment loss before that fix.
Full repository validation and final code review accompany the implementation PR.
