<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# IPAM upgrade adoption

## Scope and selected policy

Revision r2 implements the upgrade policy selected in ADR 0006 and the upgrade ticket.
The reconciliation module owns adoption, ownership marks, the barrier and counts.
The existing claim, reconcile, job and HTTP interfaces are the test seams.
The accepted ADR already selects the policy; this is the selected-plan route of design-blind.
No ownership data migration guesses owners. A schema migration adds only upgrade state.

## State and interface

Add Server.ipam_first_complete_at, nullable and read-only, and IPAMOwnershipLink.adopted, default false.
Add one internal Server.ipam_initial_observations JSON field for successful whole-workflow receipts.
A receipt records the job or DHCP import, its observed family/source pairs and an aware completion time.
One typed serializer in reconciliation owns receipt reading and writing.
The Server timestamp records when the initial observations across all enabled workflows first become complete.
The job and import remain separate actions. Neither action attests to the other.
A source uses the effective global and Server flags; import participation uses opt-in and installed-plugin state.
A disabled family, source or workflow does not hold a relevant barrier.
The job returns and accumulates typed SyncReport values instead of mutable stats dictionaries.
The report carries the counts of unowned marker objects and objects waiting for adoption completion.
Counts deduplicate objects, rather than counting their ownership links or reporting sources twice.
Expose the completion timestamp read-only in REST, GraphQL and the Server detail page. Keep receipts internal.

## Adoption and identity

For a non-global address claim, lock the global identity before the target VRF identity.
Read the target and legacy global rows under row locks.
Moving requires a valid ownership marker, no current owner and all configured Servers using the same target VRF.
A target collision keeps the global row and reports a conflict while the target follows normal claim policy.
Mixed Server VRFs keep the global row and create a target row for each reporting Server.
A global Server claims the global marker row in place.
Never adopt a blank description without an explicit forced claim.
Keep the object primary key, related references and changelog when a move occurs.
Rows absent from all observed keep-sets remain unowned and are never cleaned up.

An adoption mark is set when a claim links an existing unowned marker object.
A new link to any object with an adoption-marked link inherits that mark.
Propagate the mark before dropping superseded stale links, so their removal cannot erase the protection.
Partial observations may safely link valid marker rows; they cannot record first-run completion.
This allows valid phase work to survive a sibling failure without declaring the upgrade complete.

## Barrier

The last ownership link of an adoption-marked object remains while a relevant enabled Server lacks the required whole-workflow observation receipt.
For IP addresses, lease and Reservation sources can be owners. Prefixes use Subnet, delegated-prefix and live delegated-prefix lease (`lease-prefix`, DHCPv6) sources.
IP Ranges use Pool sources. Determine relevant families and effective source flags from configured Server state.
Global source flags and Server flags both apply. The DHCP import can report Reservation addresses, Subnet Prefixes, Pool ranges and delegated Prefixes.
Its opt-in plus an installed plugin makes those sources relevant, including import-only Servers.
Delegated Prefix ownership is relevant only to DHCPv6.
A disabled periodic job does not exclude an enabled DHCP import from the barrier.
The barrier applies to removal, deprecation and final unlinking, including cleanup mode none and reference protection.
An operator release still removes the links, because the description is the release signal.
Deleting a Server drops its links and preserves objects; a deleted Server no longer holds the barrier.
A complete run with no reported records still records completion.
A job receipt is recorded only after all enabled family reports finish without incomplete phases or row errors.
An import receipt is recorded only after every required family has complete configuration and Reservation observations,
successful ownership work and committed delegated attachments. Missing results do not count as empty observations.
Two complementary partial runs cannot combine into one whole-workflow receipt.
A complete empty observation counts. A claim-only or partial bulk action cannot record completion.
Public purpose-specific completion functions consume typed report evidence with actual completed source coverage.
The job and import share the effective-source policy used by barrier queries.
Re-read current required scope before publication; newly enabled unobserved pairs cannot receive completion credit.
Merge receipts under a Server NO KEY UPDATE row lock, so concurrent workflow completion preserves both receipts.
This lock permits deferred ownership foreign-key checks to finish while policy writes wait for cleanup.
The historical first-complete timestamp is never reset. A newly enabled unobserved source can hold relevant marked objects
until its required workflow completes that scope. Barrier decisions use receipts, rather than the historical timestamp alone.
A Run Now invocation cannot silently treat an incomplete or narrower source run as the Server completion.
Normal last-link rules apply once relevant Servers complete their required initial observations.
Final adopted-link cleanup locks all current ownership links before the Server and Sync Configuration tables, then reads current scope.
These locks are nonblocking. A busy owner or policy write fails only its cleanup row and keeps the object.
The next sync retries that row. This also prevents Server cascade deadlocks within a DHCP import transaction.
Locks stay until the owning transaction commits. DHCP imports can hold them across several rows.
Cleanup inside an outer transaction also refuses busy identity and object locks, before it can wait with retained policy locks.
The policy locks also cover new Servers and bulk updates.
Count snapshots read policy once without these locks, because they do not authorize cleanup.
Operator release and cleanup with other owners do not take the policy locks.

## Replacement and guard

Delete old sync writes, stale cleanup, hostname indexes, cleanup parameters and snapshot sentinel paths.
Move still-needed ownership primitives into reconciliation. No private sync helper remains imported by another module.
The reservation mutation autosync also uses claim, so no old writer remains after the replacement.
Retain useful behavioral regressions by migrating them to public claim, reconcile or job interfaces.
Keep read-only synchronization badge helpers and DCIM MAC synchronization in sync.py.
Add a rule to the existing local OpenGrep ruleset that refuses IPAM deletes and status writes outside reconciliation.
Test direct model, queryset and typed-object writes; include known-good reconciliation and unrelated model controls.
Do not add OpenGrep to CI.

## Validation and acceptance

Real ORM and real KeaClient transport tests cover same-ID movement, global adoption, mixed-VRF order independence,
existing ownership, target collision, pre-upgrade stale retention, blank conflicts, inherited adoption marks,
barrier waiting and release, incomplete family runs, disabled sources, and deleted Server counts.
A separate-connection race validates global-first locking and competing adoption identity reads.
HTTP tests retain permissions, explicit force and no cleanup for per-row actions.
Guard tests prove forbidden IPAM writes fail and permitted writes pass.
Run focused tests and type gates during implementation, then the full isolated native suite and browser suite.
Update the parent acceptance checklist only for requirements with current test or gate evidence.
The operator merged the parent PRs during implementation and selected develop as the new PR base.
The task branch includes that merged history. The new PR targets develop.

## Review record

The r1 reviewer confirmed global-first adoption locking, all-Server VRF selection and last-link protection.
It found that periodic completion cannot prove the separate delegated-prefix import completed.
The coordinator traced the job phase construction and the separate import reconciliation and accepted the blocker.
A fresh blind Astra medium designer received only the ticket, ADR and source pointers, without this proposal.
The coordinator drafted its separate receipt proposal before reading the blind design.
Both designs preserve existing actions and require independent whole-workflow completion evidence.
The blind designer also identified that imports own Reservation, Subnet and Pool objects, not only delegated Prefixes.
Revision r2 includes those potential import owners and uses current observed scope for the barrier.

| Decision | Coordinator | Blind designer | Evidence and disposition | Consequence |
| --- | --- | --- | --- | --- |
| Completion | Separate job and import receipts | Separate receipts plus combined timestamp | Jobs and imports run independently. Select receipts. | A job cannot prove an import completed. |
| Representation | Timestamp and compact import family map | Timestamp and typed scoped workflow receipts | Import and effective flags can change. Select scoped JSON receipts. | Current coverage can detect newly enabled sources. |
| Import scope | Include all importer sources | Include Reservation, Subnet, Pool and delegated sources | Actual import calls claim for all these object types. Both agree. | Import-only Servers remain potential owners. |
| Unified coordinator | Reject new periodic import behavior | Alternative unified run | It changes existing execution policy and cannot finish an import-only Server through its current action. Reject. | Existing actions retain their purpose. |
| Publication | Lock Server row | Lock Server row and re-read scope | Concurrent finishes otherwise overwrite receipts; scope can change during reads. Merge both requirements. | Receipt publication is atomic and scope-checked. |

The independent Astra high reviewer ratified revision r2 for the complete adoption, barrier, removals, guard and counts scope. It confirmed reachable import-only completion, missing-family protection, whole-workflow receipts and current-scope checks.

## Section 0: refuted claims

The historical first-completion timestamp cannot decide the barrier alone. A later enabled source needs a
new complete observation. Current workflow coverage receipts decide protection; the timestamp keeps its
historical meaning.

## Status

Revision r2 is ratified and implemented. Real ORM tests cover job-before-import protection, incomplete observations, concurrent receipt preservation and normal cleanup after completion. The job and import share receipt state and effective-source policy.
