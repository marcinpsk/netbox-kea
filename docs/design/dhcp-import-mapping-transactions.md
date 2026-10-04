---
status: accepted-for-implementation
date: 2026-10-04
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# DHCP Import Mapping transaction ownership

## Scope and acceptance predicate

The mapping lifecycle module owns coordination between native branch replay, the real importer and main
target writers. Its interface must preserve existing main save behavior, public signatures, native replay
semantics and dry runs. Lock order must precede relevant row locks and state selection. Conflicting main
state causes whole-action refusal. Operation context and native request history cannot survive an error.

The observable seam is the existing importer, native merge and revert, and actual HTTP target and mapping
read pages. Tests use real PostgreSQL transactions. No test substitutes replay or locking with a mock.

Signal-only coordination and explicit operation and writer integration are competing candidate shapes.
The installed framework emits preaction signals before its atomic replay. Postaction signals are absent
on failure and dry run. Django update_or_create can lock a row before pre_save. These facts require an owner
for coordination, rather than a set of independent checks in signals.

## Independent design procedure

The implementation designer is GPT-6.1 Sol with extra-high reasoning. A fresh GPT-6.1 Sol with high reasoning
receives only the constraints and installed framework source, without the first designer's proposal.
Both designs were complete before comparison. The first designer used extra-high reasoning. The blind
designer used high reasoning and received only the fixed policies and installed framework source. Astra
reviews the merged candidate with high reasoning in an actual read-only sandbox before implementation.

## Revision r4: merged integration candidate

Changes from r3:

- Define writer coverage by selection and mutation of protected scalar and relation state. Include
  forward, reverse and dynamically selected relationship managers, including reverse-FK mutators.
- Extend early deletion coordination to every root that can delete or mutate protected scalar or M2M
  state. Include direct write-on-delete FK parents and semantic M2M endpoints, then expand upstream
  CASCADE and incoming GenericRelation ownership. The installed delete-effect closure has 24 models.
- Preserve native related-manager selection through __call__, and adapt its returned manager before
  mutation. An adapted cached descriptor alone does not cover the dynamically selected native manager.

Changes carried from r2 to r3:

- Acquire coordination before native cascade collection, through bounded model and queryset delete
  adapters for the complete concrete CASCADE ancestor closure. Late signals remain secondary guards.
- Record the installed closure of seven models and preserve each ordinary, default and base manager.
- Specify custom NetBox tag-manager entry adapters. Their field descriptor does not use Django's
  ordinary related_manager_cls, and its target-specific reverse accessor is hidden.

Changes carried from r1 to r2:

- Acquire coordination at forward and reverse M2M manager entry, before relation selection. Retain late
  signal guards. A pre-change signal alone cannot serialize a cached relation delta.
- Include unique ordinary, default and base manager instances while preserving their queryset APIs.
- Use nonblocking acquisition when any initialized connection in the thread is already atomic.
- State that raw native deserialization calls Model.save_base directly. The enclosing strategy scope
  coordinates this path; a model-defined save_base adapter does not intercept it.
- Preserve complete required timestamp data in native CREATE and DELETE history. Exclude observation
  timestamps from semantic comparison, rather than omitting required fields from raw replay payloads.

One deep lifecycle module owns mapping identity, configured routing, transaction coordination, semantic
history matching and replay validation. Optional branching imports stay in the existing branching
integration module. The reader and writer use the same verified schema and identity definitions.

The importer enters an atomic metadata scope before linked-object selection, adoption and any row lock.
All participating main writers use one PostgreSQL advisory transaction lock. A new autocommit writer
opens its transaction and acquires the lock before selection or mutation. A writer already inside an
outer atomic transaction on any initialized connection in the thread uses the nonblocking transaction-lock
variant on its actual write alias. Contention raises a clear retry refusal. It cannot wait while retaining
an unknown caller's row locks, including locks on another alias. Reentrant acquisition succeeds inside
the same transaction. This preserves ordinary main autocommit saves and avoids row-lock inversion.

Bounded model adapters delegate the original Subnet, HostReservation and named semantic relation endpoint
save and save_base methods. The installed named endpoint is Tag. Native target history stores tag names,
so a scalar tag rename must coordinate as well as a tag relation mutation. Ordinary relations store PKs
and do not extend scalar writer coverage to unrelated endpoint attributes.
They span ordinary native validation and allocation. Native raw deserialization explicitly calls
Model.save_base, so its strategy-entry lock is the owner; no global base-method patch is used.
Manager-specific QuerySet subclasses preserve the existing managers, annotations and restricted-query
APIs, including unique objects, default and base manager instances. Their create, get_or_create,
update_or_create, update, bulk_create, bulk_update and delete entry points enter the same scope before
native selection or DML. KeaDhcpLink uses its own matching manager boundary. No global Django method is
replaced. Native validation, signals and change logging remain enabled.

The invariant is coordination before native selection and mutation of protected scalar or relation state.
The writer inventory derives from the supported target and mapping model metadata and the shared relation
identity definition. It includes ordinary
model and queryset writers, native raw replay, forward/reverse relationship mutators, custom tags and
every ordinary deletion root with an effect on that state. Input querysets are evaluated inside the scope.
Explicit already-materialized caller objects remain the caller's requested values, as in native Django.

Deletion entry adapters cover the complete delete-effect closure. Start with both targets and the mapping,
their direct FK parents whose deletion writes or deletes those rows, and their semantic M2M endpoints.
Exclude PROTECT, RESTRICT and DO_NOTHING FK parents, whose deletion cannot silently mutate a dependent
target. Expand upstream concrete CASCADE relations, parent links and actual incoming GenericRelation
ownership. Do not recursively add a dependency's SET_NULL parents when only that dependency's attributes
change and its target reference remains intact. This is derived coverage, rather than a growing manual
list of individual parents.

The supported installed closure has 24 models: ContentType; Device, Interface, Location, MACAddress,
Module, ModuleBay, Region, Site and SiteGroup; Tag; FHRPGroup, IPAddress and Prefix; ClientClass,
DHCPServer, DHCPServerInterface, HostReservation, SharedNetwork and Subnet; KeaDhcpLink and Server;
VMInterface and VirtualMachine. None has a concrete multi-table parent. Generic ownership includes
Interface, FHRPGroup and VMInterface for IPAddress, Interface and VMInterface for MACAddress, and
Region, SiteGroup, Site and Location for Prefix. Subnet server interfaces are DHCPServerInterface rows;
their concrete interface and VM interface ancestors enter through CASCADE.

Model delete and all unique manager QuerySet delete entry points acquire coordination before the native
collector selects descendants, and retain the transaction lock through its final mutation. Existing
SubnetManager, SharedNetworkManager and ContentTypeManager behavior remains intact. This covers direct
target, bulk, and ordinary parent deletion through their actual APIs without replacing Django Collector.
The target has no inverse GenericRelation to the mapping. Native private dependent cleanup remains in
the original collector. No global Django Collector method is replaced.

Forward and reverse relationship manager mutators enter coordination before evaluating input or relation
state, including set, add, remove, clear and participating creation/adoption methods. This includes
reverse-FK managers such as DHCPServer.child_host_reservations and every corresponding FK manager for
protected rows. Their cached descriptor and native __call__ selected-manager factory are both covered.
Delegate the native factory, then adapt its returned manager while preserving its selected API.
The adapter inventory
includes inherited and custom relation managers. Subnet has client classes, additional evaluation classes
and server interfaces. HostReservation has client classes, IPv6 addresses and both Prefix relations. Their
reverse managers are included. Inherited tags use NetBoxTaggableManagerField, whose descriptor creates
its field.manager directly. A scoped manager subclass preserves that custom implementation and coordinates
before tag lookup, creation or relation selection. Its target-specific reverse accessor is hidden. Shared
TaggedItem signals filter actual target instances. All adapters preserve original calls, native signals and actual
write routing. Native raw relation replay is covered by the enclosing strategy scope. A set operation
must not calculate a delta before acquiring coordination and then apply it to a reverted relation.

Late cascade pre_delete and relevant M2M pre-change signals retain a nonblocking lock guard. Register M2M
guards by the actual through sender, including reverse operations and clear without a pk_set. A conflicting
writer causes rollback of the native collector or M2M transaction. Signal guards never wait after an
upstream writer can hold row locks. Direct SQL and independently constructed QuerySets outside the ordinary
NetBox model-write contract are outside this bounded adapter. Tests prove ordinary public views, the
importer, manager writes, native raw replay and target M2M writers. This is not a promise about arbitrary
SQL transactions or unrelated external callers.

Transparent adapters around the native Branch merge and revert methods own an explicit operation
context and finally cleanup. They preserve arguments, return values, exceptions, commit flags, native
status transitions and native transaction lifetimes. They do not add an outer atomic block around the
action. Native strategy merge and revert entry points acquire coordination and validate the complete
affected footprint inside the existing native atomic block, before skip-missing reads, dependency
ordering or the first replay mutation. They then call the original strategy without replacing replay.
The native graph signal adds only a dependency the native GenericForeignKey ordering cannot represent.

Preaction checks perform structural schema, routing and strategy refusal. Current main-state validation
occurs under the changing transaction lock. It covers original PK reuse, missing Server or target
dependencies, source and target uniqueness, newer semantic target fields and M2M state, unmatched late
main mappings and undo of a branch-created target adopted on main. Affected iterative actions refuse
before mutation and keep their selected strategy. Recognized mapping conflicts are validated before any
unrelated native replay mutation. A native protection-rule refusal after an earlier replay mutation
proves database, status and AppliedChange rollback without an injected replay exception.

Operation context identifies the action. Synchronous native ObjectChange and AppliedChange provenance
identify each destructive replay mutation in its actual transaction. Branch status alone grants no
permission. A missing association causes refusal, rather than deleting an unrecorded main mapping.

The locked prevalidation includes targets selected by native parent deletion, not only targets named
directly in branch changes. Restoration checks required dependencies and source and target uniqueness
before the first replay mutation. A late deletion guard rechecks expected target semantics and generation,
the recorded mapping generation and the native applied deletion for the current request inside main's
atomic transaction. This second check covers a reentrant importer in the same transaction, which can
acquire the advisory lock again.

Each action isolates the native pre_delete suppression set. With an enclosing real request, finally
restores that request's prior set. A direct action without an enclosing request discards stale prior
state and clears its own state on every exit. Native event_tracking continues to own request and event
contexts. A successful retry on the same identities must produce new native deletion and applied history.

Mapping semantic fields are Server, family, Kea subnet ID or normalized Global Reservation identity,
target content type and original target PK. Observation timestamps are not semantic changes. A locked
no-op import updates last_synced without a semantic change record. Genuine source or target changes use
native change logging. Semantic comparison omits only nonsemantic timestamps and normalizes M2M identity
ordering. CREATE and DELETE history retain complete required timestamp fields, because native raw CREATE
does not run auto_now. Reversible deletion records retain original PK and both identities. An ordinary migration
registers the new branchable/change-logged model behavior and timestamp schema. Old branches are not
retrofitted. Their affected operations and actual DHCP Plugin tab use the same fresh-branch refusal.
Verify physical target tables as well as mapping columns. A branch created before the optional DHCP
plugin was enabled must not resolve a missing target table to main through the connection search path.

Creation time supplies separate row-generation provenance. Scoped target and mapping serialization
retains its full aware precision in the existing created field. Native replay parses and restores that
field. Django's default JSON encoder truncates datetime values to milliseconds, so rounding a current
row before comparison could hide same-millisecond PK reuse. Real unchanged-original, native-restored
and same-millisecond replacement controls must prove the full-precision comparison. This adds no receipt,
identity column or history retrofit.

The complete feature stays in one change. A narrow mechanical guard is the shared writer scope plus
real regression tests for every native entry point used here. A source scan cannot prove PostgreSQL
ordering and is not a substitute for these tests.

## Divergence table

| Decision | First design | Blind design | Evidence and disposition | Consequence |
| --- | --- | --- | --- | --- |
| Autocommit coordination | Bounded Python model and manager adapters | Prefer PostgreSQL statement triggers | Native save, raw replay and manager entry points are inspectable. Both designs require earlier adapters for SELECT FOR UPDATE. Select Python adapters. | Avoid cross-app trigger installation and optional-plugin DDL. Preserve all native calls; prove each participating entry point. |
| Replay lock placement | Graph signal, with an identified skip-missing limit | Graph signal or earlier scope when required | Native squash reads main before the graph signal. Move acquisition to native strategy entry inside native atomic. | The first safety read and every replay mutation share one transaction lock. |
| Unknown existing transactions | Late nonblocking signal guard | Earlier caller scope or explicit limit | Earlier caller row locks cannot be recovered at a later hook. Use nonblocking acquisition for every pre-existing outer atomic scope. | A reachable contention causes retry refusal instead of a deadlock. Ordinary uncontended writes remain valid. |
| Context cleanup | Around-action adapter | Around-action adapter or upstream API | Native postaction signals are absent on errors and dry runs; event_tracking does not clear delete suppression. Ratify a bounded transparent action adapter. | No status rewrite or replay replacement. Isolate and restore enclosing request history in finally. |
| Optional-schema capability | Shared reader/writer resolution | Include trigger capability if triggers selected | Only mapping schema and configured routing are needed for Python adapters. | Verify real namespace columns and routing before selection; never fall through to main. |

## Round 1 dispositions

The high-reasoning, read-only Astra review returned NOT-CLOSED on r1 with one blocker. Django's native
M2M set selects old relation IDs before emitting a pre-change signal. A writer that selected `{B}` can
resume after revert restores `{A}` and apply its old delta, producing `{A,C}` instead of `{C}`. Verified
against the installed related_descriptors set_base and native update_object. CLOSED in r2's design by
manager-entry coordination before relation selection, with forward/reverse real transaction tests required.

The reviewer also refuted the claim that a model-defined save_base adapter intercepts raw deserialization.
Verified Django DeserializedObject.save calls Model.save_base directly. CLOSED by naming the existing
strategy scope as that path's coordination owner. No global method patch is proposed.

## Round 2 dispositions

The high-reasoning, read-only Astra review returned NOT-CLOSED on r2 with one blocker. A parent deletion
can collect a mapped Global Reservation before revert moves it to another DHCPServer. A late signal
then acquires the free lock and deletes the stale collected child. Verified the target CASCADE foreign
key, native Model.delete, Collector collection and cached-PK deletion, and native FK restoration.
Revision r3 coordinates before collection at every concrete ancestor deletion entry point. It requires
a real parent-delete/revert race proving both orderings. A nonblocking late signal alone is insufficient.
The reviewer closed the revised M2M placement at design level and accepted the other r2 corrections.

## Round 3 dispositions

The high-reasoning, read-only Astra review ratified r3's core lifecycle, routing, identity and history
rules. It returned NOT-CLOSED on the writer mechanism with a reverse-FK input-selection blocker.
Verified native related manager set evaluates its queryset before add uses cached PKs. A queryset
selected from Server B can be applied after revert moves the row to Server C, violating either serial
ordering. Revision r4 uses the shared selection-before-mutation invariant for reverse-FK as well as M2M
entry points. Native dynamic selected-manager factories remain part of that interface, rather than an
escape from the adapter. Real input-queryset/revert schedules must prove both orderings.

Independent source inspection also found the same deletion effect through SET_NULL and M2M endpoint
deletions. Native core can select related targets before their save or relation-mutator guards run.
The r4 delete-effect closure puts coordination before those parent selections and before collection.
The earlier seven-model CASCADE-only closure is retained as evidence of the narrower r3 scope, not
claimed as complete r4 coverage. Model metadata determines the broader closure in one place.

The trigger mechanism is a structural split candidate, not deferred correctness work. The selected
Python shape must satisfy the whole acceptance predicate by itself. If its ordinary writer coverage or
replay placement cannot be proved, this revision is blocked rather than partially shipped.

## Evidence and limits

Installed primary sources show update_or_create locks before pre_save, raw replay calls save_base,
Subnet save validates before base save, native squash selects main before its graph signal, and native
public action status writes sit outside replay atomic. Native event_tracking restores context in finally
but leaves the core deletion suppression set to request_finished. These facts determine adapter placement.
The production tests must verify these facts against real installed libraries rather than mock adapters.

Native event_tracking flushes the configured event pipeline after each replay object. The default
pipeline invokes native action providers. This change guarantees database rows, native status and native
history rollback, as required by the accepted ticket. It does not replace the native event pipeline or
promise rollback of arbitrary external event consumers. Complete metadata prevalidation prevents a
recognized mapping conflict after earlier replay event dispatch. Existing native protection failures
retain their native event behavior.

## Section 0: refuted claims

- A model-defined save_base adapter intercepts native raw deserialization: REFUTED. Django's
  DeserializedObject.save calls Model.save_base directly. The enclosing native strategy scope owns
  coordination for this path. Reopen if the installed deserialization path changes.
- PostgreSQL triggers are required for safe ordinary writes: REFUTED at design level. Bounded native
  entry adapters can coordinate before both selection and mutation. Reopen if real writer coverage
  or transaction-order tests cannot satisfy the complete acceptance predicate.

## Round 4 dispositions

The high-reasoning Astra review ran in a read-only sandbox and returned RATIFY for implementation
of the complete lifecycle and writer scope. No design blocker remains. Generic related-manager
set must enter coordination before it evaluates inputs and old relations, as required by the shared
writer invariant. Dynamic selected-manager factories and manually disabled autocommit remain covered.
The review accepts the mechanism; the runtime gates below still require real behavioral evidence.

## Runtime gates

- Preserve ordinary main autocommit target writes through a proved transaction interface.
- Acquire coordination before state selection and row locks for participating metadata writers.
- Refuse a late main mapping absent from reversible merge history.
- Refuse revert of a branch-created target adopted and mapped later on main.
- Prove complete rollback, status preservation, applied-history rollback and context cleanup on errors.
- Keep schema and configured routing checks consistent for changes and public mapping reads.
- Preserve native dry-run and optional-plugin behavior.

## Verdict

Revision r4 is ratified for implementation. Product policy is unchanged. First implementable increment:
the real import, branch-local delete, squash merge, revert and same-PK reimport lifecycle for both target
kinds and families. First record the failing real regression, then implement the ratified mechanism.
