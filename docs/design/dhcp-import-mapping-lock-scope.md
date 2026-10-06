---
status: ratified
date: 2026-10-06
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# DHCP Import Mapping lock scope

This record revises the coordination scope in `dhcp-import-mapping-transactions.md`. It answers the
review finding on PR #301 (thread 4190125058): the global mapping lock refuses unrelated core NetBox
writes while a DHCP import runs.

## Ratified design (r6)

Astra (GPT-6 high, read-only) ratified r6 on 2026-10-06 for the full design, r1 to r6 as amended.
This section states the result. The sections after it hold the brief, the two blind designs, the
divergence table and every round, and they are the evidence for each rule below.

### Contract

1. Coordinated operations exclude each other with one PostgreSQL advisory transaction key
   (`_METADATA_LOCK`), EXCLUSIVE only. The acquisition rule of the transaction design is unchanged:
   blocking when no initialized connection in the thread is in a transaction, `pg_try_*` otherwise.
2. An uncoordinated deletion never deletes, detaches or field-updates a committed Subnet,
   HostReservation or KeaDhcpLink.
3. A race between an uncoordinated deletion and an uncommitted coordinated operation that references
   the deleted row ends with PostgreSQL's deferred FK check rolling back the whole coordinated
   transaction, as for any core NetBox writer. No partial state commits.

### Who takes the key

- Takes it: `coordinated_import` (whole call), native replay at strategy entry, `preflight_action`,
  target and mapping writers (save, save_base, queryset writes, forward, reverse and generic relation
  mutators), target and mapping deletes, and closure deletions whose probe reaches protected state.
- Does not take it: a closure deletion whose probe reaches no Subnet, HostReservation or
  KeaDhcpLink (an "uncoordinated deletion"), and Tag save, save_base and queryset writes.

### Uncoordinated deletion

- `deletion_scope(subject, using)` replaces the body of `_guard_parent_delete`. When the key is held
  in this transaction, it delegates (reentrant). Otherwise it runs a read-only native `Collector`
  probe (a generalized `_delete_effect_targets` that takes `using` and a queryset and reports
  collected KeaDhcpLink rows; `ProtectedError` and `RestrictedError` count as relevant). A relevant
  probe enters `metadata_scope` and runs the native delete. An empty probe marks `using` as
  uncoordinated for the duration of the native delete.
- Late `pre_delete` hook while uncoordinated: a protected sender raises the stale-gate refusal. A
  sender in `fence_models()` (derived and cached: the remote models of every FK and M2M of the three
  protected models) runs the fence: `SELECT 1 ... WHERE pk = %s FOR UPDATE`, then one `exists()` per
  protected relation that points at the row. A committed reference raises the stale-gate refusal.
- Protected writes while uncoordinated and without the key: instance `save()` and `.remove()` of a
  protected row are refused. Queryset `update` and `delete`, relation removal and through-row deletes
  run natively inside the adapter savepoint. Zero affected rows: return normally (Django schedules
  SET_NULL updates on empty querysets). One or more rows: roll the savepoint back and raise the
  stale-gate refusal.

### Replay and Tags

- Replay prevalidation reads the named endpoints it validates (Tags) with `FOR KEY SHARE`, so a Tag
  rename waits for the replay to commit (E5). This replaces the Tag advisory adapter.
- Tag keeps thin save, save_base and queryset-write adapters that take no key and map 40P01 and
  55P03 to `MetadataBusy`.

### Errors

- `MetadataBusy(AbortRequest)` is the one refusal type. Messages: contention; stale gate ("This
  deletion now reaches an imported DHCP target or its mapping."); lock conflict ("A database lock
  conflict stopped this change."); importer COMMIT ("The DHCPv<family> import referenced an object
  that no longer exists. Nothing changed for DHCPv<family>. Run the import again."). Each ends with
  "Nothing changed. Retry this operation." where that is not already stated.
- 40P01 and 55P03 from lock statements, the fence and the native operation map to `MetadataBusy`
  outside the adapter savepoint, after Django rolled it back. No SQL runs in a `finally` on a broken
  transaction. No constraint-mode change and no 23503 classification at adapters.
- When `coordinated_import` opened the transaction, a 23503 `IntegrityError` from its COMMIT becomes
  the importer COMMIT refusal.
- `views/dhcp_plugin_sync.py` catches `MetadataBusy` before `except Exception` and shows its message.
- The IPAM sync job's `_each_row` treats `MetadataBusy` as a row failure; the next run retries.
- `BranchRefusalMiddleware.process_exception`: a request that entered lifecycle coordination carries
  a marker. An `OperationalError` caused by 40P01 or 55P03, including one from the view's COMMIT,
  sends `clear_events.send(sender=None)` and then returns the refusal: a UI request gets an error
  message and a redirect to the referring page, and a REST request gets HTTP 400
  `{"detail": <message>}`. Other exceptions pass through.

### Cases that still refuse or fail natively

1. Target or mapping writes, and relevant deletions, inside a transaction while another coordinated
   operation holds or awaits the key (as before).
2. A closure deletion whose effect reaches a protected row or a committed protected reference,
   found by the probe (coordinated) or by the late hook, fence or zero-row rule (refused).
3. Replay when the key is held at strategy entry; a Tag rename racing a replay that validated it.
4. A nested import inside a caller transaction while the key is held.
5. Any 40P01 or 55P03 at a lifecycle boundary or at a coordinated request's COMMIT (retry refusal).
6. Native FK failure with full rollback: a coordinated operation, a branch merge, or a core write
   that references a row an uncoordinated deletion removes before it commits.

### Validation gates

Real PostgreSQL, `TransactionTestCase`, threads, no lock or replay mocks. Each new test fails on
ab9803d4 and passes on the change:

- unrelated Device, Site, IP address and Prefix deletes and a Tag edit complete through the real
  views during a paused import, and the import commits;
- the IPAM sync job's stale IP address delete completes during an import; a relevant one is a row
  failure;
- an IP address deletion whose only DHCP effect is an empty SET_NULL update completes;
- a relevant Device deletion during an import refuses, nothing deleted;
- an uncoordinated deletion that a concurrent writer makes relevant after the probe refuses before
  DML, rows intact;
- a dependency deletion during replay ends in whole-action refusal or full rollback, branch status
  and AppliedChanges unchanged;
- a Tag rename racing replay validation waits or refuses;
- a lock error at a coordinated view's COMMIT returns the refusal (UI redirect, REST 400) and
  dispatches no event;
- a deadlock in an uncoordinated deletion maps to `MetadataBusy`;
- an importer COMMIT-time FK failure maps to the importer refusal.

The transaction-design regression tests stay green (stale cascade, M2M delta, custom tags, reverse
FK, PK reuse, generic relation, main save, import after revert, writer-first replay).

## Brief (step 1)

### Problem

`netbox_kea/dhcp_mapping_lifecycle.py` coordinates the importer, native branch replay and main
target writers with one PostgreSQL advisory transaction lock (`_METADATA_LOCK`, `metadata_scope`).
`register()` installs adapters on the delete-effect closure (`delete_effect_models()`, 24 installed
models: ContentType; Device, Interface, Location, MACAddress, Module, ModuleBay, Region, Site,
SiteGroup; Tag; FHRPGroup, IPAddress, Prefix; ClientClass, DHCPServer, DHCPServerInterface,
HostReservation, SharedNetwork, Subnet; KeaDhcpLink, Server; VMInterface, VirtualMachine) and on
the writers of the targets, the mapping and the named endpoint Tag (`metadata_writer_models()`).
A caller that is already inside an atomic block uses `pg_try_advisory_xact_lock` and gets
`AbortRequest("DHCP mapping metadata is changing in another transaction. Retry this operation.")`
on contention. An autocommit caller waits (`pg_advisory_xact_lock`).

`coordinated_import` holds the lock for the whole `import_server_config` call: every Subnet, Pool,
Reservation, option, client class and IPAM claim of one Server and family.

Consequence on every install with netbox-branching and netbox-plugin-dhcp, also when no branch
exists: while an import runs, NetBox UI/REST deletes of a Device, Site, IP address or Prefix, every
Tag save, and the IPAM sync job's stale `ip.delete()` (`ipam_reconciliation.py` ~1265, inside a
savepoint) are refused with a DHCP-mapping message that has no relation to the action. Each of those
writers in turn can make an import or a branch merge fail.

### Class

The owner is the lifecycle module (`dhcp_mapping_lifecycle.py`); its seam is `metadata_scope` and
the adapters `register()` installs. The failure class: a coordination scope sized by "can this model
ever affect protected state" instead of "does this operation affect protected state now". It makes
availability of unrelated workflows depend on an optional plugin's background work.

### Fixed constraints (from the ratified r4 design, not reopened here)

- Coordination happens before native selection, cascade collection and row locks. A late signal
  cannot recover row locks the caller already holds.
- A writer that is inside an unknown outer transaction must not wait while it holds row locks that
  the lock holder may need (row-lock inversion). Contention there ends in a clear refusal or in a
  wait that cannot deadlock silently. An uncaught PostgreSQL deadlock error (HTTP 500) is not
  acceptable.
- Native replay (merge, revert, squash and iterative strategies) and the importer keep their r4
  correctness: no stale cascade collection, no M2M delta from an old selection, no PK reuse
  confusion, whole-action refusal on conflicting main state.
- Real PostgreSQL transactions in tests. No mock replaces the lock or replay.
- No new configuration options. No compatibility layer for the old behavior.

### Observable acceptance conditions

1. While an import holds its coordination for one Server and family, a separate PostgreSQL
   transaction that deletes a Device, Site, IP address or Prefix whose deletion reaches no DHCP
   target or mapping completes (it may wait, it is not refused). Same for a Tag save on a Tag that
   no target carries, and for the IPAM sync job's stale IP address delete.
2. A concurrent write whose effect does reach a target or mapping that the import or a replay is
   changing is still coordinated: it waits or gets the retry refusal, and the r4 regression tests
   (stale cascade, M2M delta, Tag rename vs replay, PK reuse) stay green.
3. No deadlock surfaces as HTTP 500; a PostgreSQL deadlock or lock timeout maps to the retry refusal.
4. The design states the remaining cases that still refuse, by model and condition.

### Evidence pointers

- `netbox_kea/dhcp_mapping_lifecycle.py`: `metadata_scope` (~75), `observe_mapping` (~120),
  `_guard_model_write`/`_guard_queryset_write` (~1020-1066), `coordinated_import` (~1069),
  `delete_effect_models` (~1082), `_guard_parent_delete` (~1115), `register` (~1337), relation
  manager adapters (`_register_relations`).
- `netbox_kea/branching.py` `_mapping_strategy` (~232): replay takes the lock at strategy entry.
- `netbox_kea/integrations/dhcp_plugin.py` `import_server_config` (~1010).
- `netbox_kea/ipam_reconciliation.py` stale cleanup (~1240-1265).
- `docs/design/dhcp-import-mapping-transactions.md` (r4, ratified): rationale and round dispositions.
- Tests: `netbox_kea/tests/test_dhcp_mapping_recovery.py`, `netbox_kea/tests/test_branching.py`.

### Candidate shapes (record only; not given to the blind designer)

- A. Narrow the importer hold: coordinate per imported unit (Subnet with its Pools, Reservation
  batch) instead of the whole import.
- B. Shared/exclusive split: parent-deleters and endpoint writers take a shared lock; importer and
  replay take the exclusive lock. Parent-deleters stop contending with each other, but still contend
  with the importer.
- C. Relevance gate: a parent delete or Tag write takes the lock only when its effect reaches a
  protected row. Conflicts with "coordinate before selection" unless the relevance check itself is
  safe without the lock.
- D. Keyed locks: lock per protected identity (Server+family, target row) instead of one global key.
- E. Bounded wait: blocking acquire with `lock_timeout`, relying on PostgreSQL deadlock detection
  (advisory locks take part in it) and mapping deadlock/timeout errors to the retry refusal.

## Blind co-design (step 2)

- Designer A: Claude Opus (xhigh reasoning) agent. Input: this brief, the candidate shapes above,
  the repository and framework sources. It ran throwaway PostgreSQL experiments in a scratch database.
  It read the blind designer's input packet (the same brief plus sandbox notes) by mistake, but not
  the blind design.
- Designer B (blind): GPT-6.1 Sol, high reasoning, read-only sandbox. Input: the brief only (without
  the candidate shapes), the repository and framework sources. It could not reach PostgreSQL.
- Both designs were complete before comparison.

## Divergence table (step 3)

| Decision | A | B | Evidence | Disposition | Consequence |
| --- | --- | --- | --- | --- | --- |
| Gate for unrelated deletes | Native Collector probe; no advisory lock on an empty effect | Effect check under object guards; no mutex on an empty effect | Agree | Gate on effect, not model | Acceptance 1 met for deletes |
| Stability of a negative probe | Late pre_delete fence: `FOR UPDATE` on each collected row a protected FK/M2M can reference, then a reference check; protected row in the collection refuses. Replay `FOR KEY SHARE` on validated rows; importer `SET CONSTRAINTS ALL IMMEDIATE` | New advisory object guards (hashed keys), a topology admission key, and new adapters on every intermediate topology writer (Interface moves, IP and MAC assignment, DHCPServerInterface) | A: Collector sends every pre_delete before DML (Django 6.1), NetBox disables fast deletes; E4b: immediate FK makes the fence wait for the referencing writer. B: adapter surface grows by every topology edge, and B names derivation of those edges as its main coverage risk | A. Native row locks the DELETE takes anyway, one seam (late hook) instead of adapters on all intermediate writers (deletion test: B's guards duplicate what row locks give) | Open: an ordinary target writer that sets an FK to a row an unrelated delete removes fails at COMMIT (deferred FK), as in core NetBox. Round 1 question |
| Target and mapping writers vs each other | Advisory SHARED: they no longer refuse each other | Mutex stays exclusive | Native Django has no such serialization; importer and replay keep EXCL | A | Fewer refusals; r4 writer-vs-replay exclusion kept |
| Tag writes | No advisory lock; rename vs replay by `KEY SHARE` (E5); explicit r4 amendment | Guard Tag rows and names; mutex only for a carried Tag | E5 run by A | A | Tag adapters removed from `metadata_writer_models()` |
| Replay | EXCL always, plus `KEY SHARE` on validated rows | Mutex only for a protected effect | Unrelated deletes take no lock under A, so a parent-only merge refuses only protected writers | A | Simpler replay entry |
| Deadlock and lock timeout | Map 40P01/55P03 at lifecycle boundaries to `MetadataBusy`; no added timeout | Same mapping, plus an internal 5 s `lock_timeout` and a request-level adapter for commit-time failures | Transactional callers never wait on the key in either design | A; B's commit-time adapter is unnecessary when the importer has immediate FK checks | Round 1: confirm no lifecycle path fails at COMMIT |
| Busy error inside the importer | Unit savepoint counts an error and continues | `MetadataBusy` must unwind the whole import | The importer holds EXCL, so advisory contention cannot occur inside it; only a mapped deadlock can, and per-unit error counting is the importer's existing contract | A | Round 1 to attack |
| IPAM stale cleanup | `MetadataBusy` is a row failure | Same | Agree | Adopt | Next run retries |

## Merged candidate r1

The merged candidate is design A (`dhcp-import-mapping-lock-scope` r1), below, with the
dispositions above.
### Design r1 (from design A)


### 1. Decision

Chosen shape: **C + B, with row-level dependency locks.** Coordination follows the effect of
the operation, not the model it touches.

1. **Relevance gate (C).** A deletion of a delete-effect closure model first runs a read-only
   probe with the native `Collector`. If the probe reaches no Subnet, HostReservation or
   KeaDhcpLink, the deletion takes no advisory lock. Late hooks verify the gate before any
   DML: a protected row in the native collection, or a protected reference to a collected
   row, refuses the deletion.
2. **Mode split (B).** One advisory key, two modes. Importer and native replay take it
   EXCLUSIVE. Target writers, mapping writers and relevant deletions take it SHARED, so they
   never refuse each other.
3. **Row-level dependency locks.** Unrelated writers no longer request the key, so the
   importer and replay protect the main rows they reference: replay locks every validated
   dependency `FOR KEY SHARE`, and the importer runs with immediate FK checks. Tag saves take
   no advisory lock; a rename waits on replay's `KEY SHARE` of the Tags it validated.
4. **Error mapping (from E).** Deadlock (40P01) and lock timeout (55P03) at a lifecycle
   boundary become the retry refusal. Transactional callers still never wait on the key.

Why: acceptance 1 forbids refusing an unrelated writer during an import. Only a shape where
unrelated writers never request the import's lock meets it (section 9).

Deep-module view: the **module** stays `dhcp_mapping_lifecycle.py`; its **interface** to the
adapters is `metadata_scope(using, exclusive=)` and `deletion_scope(subject, using)`. Probe,
fence, stale-gate refusal and error mapping sit behind that **seam** (**depth**). The probe
reuses `_delete_effect_targets`: replay footprint and gate share one reachability rule
(**leverage**). **Deletion test:** without `deletion_scope`, every closure deletion coordinates
again (r4), so the gate carries real behavior.

### 2. Evidence (throwaway PostgreSQL database, created and dropped)

| # | Experiment | Result |
| --- | --- | --- |
| E1a | A holds adv(1), waits adv(2); B holds adv(2), waits adv(1) | `deadlock detected` on A: advisory locks take part in deadlock detection |
| E1b | A holds adv(7), waits a row; B holds the row, waits adv(7) | `deadlock detected` on **A, the advisory holder**: A waited first, so its 1 s `deadlock_timeout` fired first after the cycle formed |
| E2 | One transaction: EXCL then try SHARED; SHARED then try EXCL | both granted; with another SHARED holder, SHARED `t` and EXCL try `f` |
| E3 | Try SHARED while an EXCL request waits behind a SHARED holder | `f` (queue fairness) |
| E4a | Deferred FK (Django default): A inserts a child of parent 1, open; B `FOR UPDATE`, sees 0 refs, deletes, commits | B never waits; A fails at COMMIT: `violates foreign key constraint` |
| E4b | Same, A ran `SET CONSTRAINTS ALL IMMEDIATE` | B's `FOR UPDATE` waits for A's commit, then sees 1 ref |
| E5 | Row held `FOR KEY SHARE`; full-row UPDATE with unchanged unique `name`; rename | first not blocked; rename blocked |
| E6 | Advisory xact lock taken in a savepoint | `ROLLBACK TO` releases it, `RELEASE` keeps it; a parent-held lock survives a nested rollback |
| E7 | `SET LOCAL lock_timeout` in a savepoint | 55P03; rollback restores the setting, RELEASE keeps it |
| E8 | 200 000 keyed advisory locks in one transaction (800 connections × 64) | 20 000 succeed; 200 000: `out of shared memory` |

`deadlock_timeout` is superuser-only, so the plugin cannot choose the victim (E1b).

### 3. Locks

Advisory key: the existing `_METADATA_LOCK` pair. Acquisition rule (r4, unchanged): blocking
when no initialized connection in the thread is in a transaction, `pg_try_*` otherwise.

| Writer | Advisory mode | Row locks |
| --- | --- | --- |
| Importer (`import_server_config`) | EXCL, whole call | `SET CONSTRAINTS ALL IMMEDIATE` if the scope opened the transaction: each reference takes `KEY SHARE` at write time (E4b) |
| Native replay (strategy entry) | EXCL, inside the native atomic | `FOR KEY SHARE` on each main row prevalidation finds present: FK and M2M dependencies of planned protected writes, source Servers, validated Tags |
| Target writers (Subnet, HostReservation: save, save_base, queryset writes, relation mutators) | SHARED | none |
| Mapping writers (KeaDhcpLink, `observe_mapping`) | SHARED | none |
| Closure deletion, probe reaches protected state | SHARED | none |
| Closure deletion, probe reaches nothing | none | fence `FOR UPDATE` on collected endpoint rows (4.3) |
| Tag save and queryset update | none | none |
| Generic-relation manager writes on closure owners | SHARED (r4) | none |

### 4. Interfaces

**4.1 `metadata_scope(using="default", *, exclusive=False)`** yields `True` when it opened the
outermost transaction. It selects `pg_[try_]advisory_xact_lock[_shared]` by mode and caller
state, records `using` in a `_held` ContextVar while active, and maps 40P01/55P03 from the
lock statement and the body to `MetadataBusy`. `coordinated_import` and `_mapping_strategy`
pass `exclusive=True`; `preflight_action` and all adapters use SHARED.

**4.2 `deletion_scope(subject, using)`** replaces the body of `_guard_parent_delete`:

```
if using in _held or _holds_key(using):   # pg_locks lookup, only inside an atomic block
    reentrant native delete
elif _protected_effect(subject, using):   # native Collector probe, read-only
    with metadata_scope(using): native delete
else:
    _uncoordinated = using; native delete; reset in finally
```

`_protected_effect` generalizes `_delete_effect_targets` to take `using` and a queryset, and
to report collected KeaDhcpLink rows. A `ProtectedError` or `RestrictedError` in the probe
counts as relevant, so the native call raises it unchanged. Subnet, HostReservation and
KeaDhcpLink keep `_guard_target_delete`, now SHARED.

**4.3 Late hooks.** `_late_delete_guard` (pre_delete of the 24 closure models): with `using`
in `_held`, the r4 checks; with `_uncoordinated == using`, a protected sender raises the
stale-gate refusal and a sender in `fence_models()` runs the fence; otherwise r4 (SHARED).
The fence runs `SELECT 1 FROM <table> WHERE pk = %s FOR UPDATE` (the lock the native DELETE
takes later), then one `exists()` per protected relation that points at the row; a reference
refuses. `fence_models()` is derived and cached: the remote models of every FK and M2M of the
three protected models (installed: DHCPServer, SharedNetwork, Subnet, Prefix, IPAddress,
MACAddress, ClientClass, DHCPServerInterface, Tag, Server, ContentType).
`_guard_model_write`, `_guard_queryset_write`, `_guard_relation_write`, `_guard_target_delete`
and `_late_relation_guard` refuse inside `_uncoordinated` without `_held` (NetBox's
`handle_deleted_object` reaches a target through `.save()` or `.remove()`); else SHARED.
`metadata_writer_models()` becomes the targets plus KeaDhcpLink: Tag loses its save,
save_base and queryset-write adapters; `named_endpoint_models()` stays for replay validation.
Django 6.1 `Collector.delete` sends every pre_delete before fast deletes, field updates and
row deletes, and NetBox's sender-less `pre_delete` receiver disables fast deletes, so each
collected non-auto-created row passes a late hook before any DML.

**4.4 Import and replay entry.** `coordinated_import`: `refuse_in_branch`, then
`metadata_scope(exclusive=True)`, then `SET CONSTRAINTS ALL IMMEDIATE` only if the scope
opened the transaction (a nested import keeps the caller's constraint mode).
`_mapping_strategy`: `metadata_scope(exclusive=True)`, try variant as r4. In `prepare_replay`,
`_require_dependencies`, `_require_servers` and `_validate_named_identities` read through one
helper, `_lock_present(model, column, values)`, which runs `SELECT ... FOR KEY SHARE` (no ORM
spelling) and returns the present values, before the first replay mutation.

**4.5 IPAM sync job.** `_each_row` adds `MetadataBusy` to its caught tuple: a refused row is
a row failure that the next run retries. The stale `ip.delete()` passes `deletion_scope`; the
job already holds that row `FOR UPDATE`, so its fence is reentrant.

### 5. Behavior on contention

| Writer | Caller outside a transaction | Caller inside a transaction |
| --- | --- | --- |
| Importer | waits for SHARED holders and replay | refused while the key is held |
| Native replay | n/a | refused while the key is held or awaited; waits on row locks of validated rows |
| Target and mapping writers | wait for the EXCL holder | refused while EXCL is held or awaited (E3) |
| Relevant closure deletion | waits | refused while EXCL is held or awaited |
| Unrelated deletion (Device, Site, IP, Prefix, Tag) | never refused for coordination; may wait on native row locks (for example IPs the import claimed) | same |
| Tag save or rename | native; a rename of a Tag replay validated waits for its commit | same |
| IPAM stale delete | n/a | unrelated: proceeds; relevant during an import: row failure |

### 6. Failure behavior

`MetadataBusy(AbortRequest)` is the one retry refusal; NetBox views catch `AbortRequest`
outside their atomic and REST returns 400. Messages, each ending "Nothing changed. Retry this
operation.":

- contention: "DHCP targets or their mappings are changing in a DHCP import or branch merge."
- stale gate: "This deletion now reaches an imported DHCP target or its mapping."
- 40P01/55P03 (`psycopg.errors.DeadlockDetected` or `LockNotAvailable` as the
  `OperationalError.__cause__`): "A database lock conflict stopped this change." Raised `from`
  the original at the lock statement, the fence, the replay prevalidation locks and the
  outermost adapter body. The transaction stays aborted or rolled back to its savepoint.

No lock timeout is added; a DBA-level `lock_timeout` on an autocommit wait maps to the
refusal. Deadlocks remain possible only on native row locks (fence, immediate FK checks,
replay `KEY SHARE`). PostgreSQL detects them (E1) and the victim depends on timing (E1b):
importer units run in savepoints that count an error and continue; replay refuses whole.

### 7. r4 invariants

- **Coordination before selection.** Relevant deletions, target and mapping writers, importer
  and replay acquire the key before native selection. The probe is a read that only chooses
  the path; native collection runs again after acquisition. An unrelated deletion is not a
  metadata writer; its first protected effect refuses in the pre_delete phase, before DML.
- **No row-lock inversion.** Transactional callers never wait on the key. New waits are row
  waits the native statements take anyway; their cycles are detected (E1) and mapped.
- **Stale cascade (r2).** A collection that contains a protected row runs under SHARED, which
  excludes importer and replay; an unrelated one that contains it refuses.
- **M2M and reverse-FK deltas (r1, r3).** Protected relation mutators enter SHARED before input
  and relation selection. Unchanged.
- **Tag rename vs replay.** Replay holds `KEY SHARE` on each validated Tag until commit; a
  rename changes a unique column, so it waits (E5); an earlier rename fails revalidation. This
  replaces the r4 Tag advisory adapter (an explicit r4 amendment).
- **PK reuse.** Target and mapping deletes are SHARED, so they exclude the importer's
  select-then-save and replay. Generation checks are unchanged.
- **Whole-action refusal.** Late replay guards are unchanged. A dependency deleted first now
  gives "dependency is missing" (locked read) instead of a commit-time IntegrityError.
- **Reentrancy.** EXCL then SHARED in one transaction is granted (E2); a nested savepoint
  rollback keeps the parent's lock (E6).

### 8. Cases that still refuse

1. Subnet, HostReservation, KeaDhcpLink: any save, delete, queryset write or relation
   mutation inside a transaction while an import or replay holds or awaits EXCL.
2. A closure deletion inside a transaction during an import or replay whose probe reaches a
   target or mapping: Device or Interface whose IP, MAC or DHCPServerInterface a target uses;
   DHCPServer or SharedNetwork with targets; Prefix used by a Subnet or reservation prefix set;
   ClientClass or Tag on a target; Server with mappings; a target or mapping ContentType.
3. A closure deletion that becomes relevant while it runs (a writer committed a protected
   reference between probe and fence). This can occur without any import or replay.
4. Replay when any writer holds or awaits the key at strategy entry, or when a validated
   dependency is deleted first.
5. An import inside a caller transaction while the key is held.
6. Generic-relation manager writes on closure owners (`interface.ip_addresses.remove`), as r4.
7. Any deadlock or lock timeout at a lifecycle boundary.

Not mapped: native IntegrityError from non-DHCP merge races (native netbox-branching behavior).

### 9. Rejected shapes

- **A, narrow the import hold per unit.** Xact locks end with the transaction, so narrowing
  means a commit per Subnet or Reservation. Unrelated transactional writers that hit a unit
  are still refused (fails acceptance 1), and prologue upserts by unique name (DHCPServer,
  ClientClass) leave replay serialization. Possible later change for row-lock hold time only.
- **B alone.** SHARED conflicts with the importer's EXCL: unrelated deletes and Tag saves stay
  refused during every import (fails acceptance 1).
- **D, keyed locks per protected identity.** A deleter cannot name keys before collection,
  adoption selects unlinked reservations before their key is known, and per-row keys exhaust
  the shared lock table (E8; a default 100-connection server has about 6 400 slots in all).
- **E, bounded blocking wait for transactional callers.** A writer that waits while holding
  row locks can make the importer or replay the deadlock victim (E1b), and the plugin cannot
  set `deadlock_timeout`. That is the failure this change removes. Only the mapping is kept.
- **Second advisory key, taken SHARED by every closure writer, EXCL by replay.** Unrelated
  deletes stay refused during every merge; row-level dependency locks give replay the same
  safety without that.

### 10. Validation (real PostgreSQL, `TransactionTestCase`, threads, no lock or replay mocks)

A paused import holds EXCL in `upsert_subnet` (pre_save receiver on the import thread). Each
new test must fail on ab9803d4 and pass on the change.

| Test | Asserts |
| --- | --- |
| `test_unrelated_http_deletes_and_tag_edit_complete_during_import` | Real delete views for Device, Site, IPAddress, Prefix and the Tag edit view, objects reach no target: 302, no refusal, rows gone or renamed, deleter never waits in `pg_locks` advisory; the import commits with `errors == 0` |
| `test_stale_ip_delete_completes_during_import` | Real `reconcile`, stale mode `remove`, unreferenced IP: `report.removed == 1`, no row failure |
| `test_relevant_device_delete_refuses_during_import` | Device whose IP is a reservation `ipv4_address`: `MetadataBusy`, nothing deleted; after the import the delete succeeds and the FK is NULL |
| `test_relevant_stale_ip_delete_is_a_row_failure_during_import` | The job records one row failure and finishes the other rows |
| `test_unrelated_delete_refuses_when_the_import_references_its_row` | Import paused after `hw_address = M`; deleting M's interface waits (`import_pid = ANY(pg_blocking_pids(deleter))`), then refuses; the import commits with M referenced. Red without immediate constraints (import fails at COMMIT, E4a) |
| `test_dependency_delete_during_replay_refuses_cleanly` | Replay first: deleter waits on `KEY SHARE`, then refuses. Deleter first: replay refuses "dependency is missing", status and AppliedChanges unchanged |
| `test_tag_rename_before_replay_validation_refuses_replay` | Open rename; replay waits on the row, then refuses with the Tag instruction |
| `test_protected_row_in_uncoordinated_collection_refuses_before_dml` | `connection.execute_wrapper` pauses the deleter after the probe; another thread moves a mapped reservation under the DHCPServer and commits; deletion refuses; rows intact |
| `test_relevant_writers_share_coordination` | Two target writers in two transactions both hold SHARED in `pg_locks`; no refusal |
| `test_deadlock_in_uncoordinated_deletion_maps_to_retry_refusal` | Deleter fences IP X, then waits on Y held by B; B requests X within 1 s; deleter raises `MetadataBusy`, not `OperationalError` |
| `test_lock_timeout_maps_to_retry_refusal` | Session `lock_timeout` on an autocommit import that waits on a held key: `MetadataBusy` |

r4 tests unchanged: stale cascade `test_parent_delete_collects_after_native_revert` (the probe
sees the merged child, so the writer still waits on the key); M2M delta
`test_m2m_set_selects_relations_after_native_revert`, `test_custom_tags_select_relations_after_native_revert`,
`test_reverse_fk_set_evaluates_input_after_native_revert`; PK reuse
`test_revert_refuses_a_reused_target_primary_key`, `test_merge_preserves_a_reused_mapping_primary_key`;
and the generic-relation, main-save, import-after-revert and writer-first replay tests.
Mechanics only: `test_tag_rename_waits_until_mapped_target_merge_commits` asserts
`replay_pid = ANY(pg_blocking_pids(writer_pid))`, not an advisory wait;
`test_inactive_branch_transactions_refuse_main_lock_contention` pauses an import as the EXCL
holder, because a second target writer now shares the key.

### 11. Uncertainties

- `SET CONSTRAINTS ALL IMMEDIATE` over a whole import is not yet run against NetBox internals
  (change logging, search cache, custom fields); the import suite must pass under it. Fallback:
  explicit `KEY SHARE` of each referenced row in `upsert_subnet` and `_upsert_reservation`,
  which must then follow the target model relations.
- Probe cost: one extra native collection per closure deletion; not measured on a large Site.
- Not checked: whether every NetBox core path enters the adapters without earlier row locks.
  The design does not rely on it.
- An unrelated deletion can wait for the whole import on rows the import locked (claimed IPs,
  immediate FK references; ContentType is a fence model too). Acceptance 1 allows the wait.

## Round 1 dispositions (Astra, GPT-6 high, read-only; verdict NOT-RATIFIED r1)

Each blocker was checked against the cited source before acceptance.

1. Stale collection under SHARED (relevant delete D collects reservation R, writer W moves R to
   another DHCPServer and commits, D deletes the cached PK and its mapping): ACCEPTED. Verified the
   CASCADE FK (`netbox_dhcp/models/host_reservation.py:101`) and Collector deleting cached PKs
   (`django/db/models/deletion.py:532`). The "native Django has no such serialization" disposition
   is withdrawn: r4 supplies it. CLOSED in r2 by restoring EXCLUSIVE for every coordinated writer.
2. Subnet ID allocation `MAX + 1` race under SHARED (`netbox_dhcp/models/subnet.py:213`): ACCEPTED,
   same fix as 1.
3. Nested import keeps the deferred-FK window (E4a) and fails at the caller's COMMIT: ACCEPTED. The
   only production caller (`run_dhcp_plugin_import`, `views/dhcp_plugin_sync.py:155`) runs outside a
   transaction; nested imports occur in tests (TestCase). CLOSED in r2 by setting immediate checks
   for every import, and restoring deferred checks at exit when nested.
4. Removing the Tag adapters leaves a waiting Tag rename with an untranslated 55P03 or 40P01:
   ACCEPTED. CLOSED in r2: Tag writers keep a thin adapter that maps lock errors, without the
   advisory lock.
5. The import view's `except Exception` (`views/dhcp_plugin_sync.py:291`) hides the retry refusal:
   ACCEPTED. CLOSED in r2 by an explicit `MetadataBusy` branch before the generic handler.

Non-blocking, all ACCEPTED: the pre_delete phase precedes Collector DML, but NetBox's sender-less
`handle_deleted_object` itself writes (history, related saves), so the `_uncoordinated` write guards
are load-bearing; a busy unit's error message names the failed unit, not a rollback of earlier
units; the importer assigns no Tags, so r1's claim about importer Tag assignment is withdrawn.

## Revision r2

Changes from r1 (they amend the r1 sections below):

- One advisory key, EXCLUSIVE only. SHARED mode is removed. Importer, replay, target writers,
  mapping writers, relevant closure deletions and generic-relation manager writes take EXCLUSIVE
  exactly as in r4. `metadata_scope` keeps its r4 signature plus the error mapping.
- What changes from r4 is only who takes the key: an unrelated closure deletion (probe reaches no
  Subnet, HostReservation or KeaDhcpLink) takes none and runs under the late fence, and a Tag save or
  queryset update takes none.
- Immediate FK checks: every `coordinated_import` and every coordinated target or mapping write runs
  `SET CONSTRAINTS ALL IMMEDIATE` at scope entry. When the scope did not open the transaction, it runs
  `SET CONSTRAINTS ALL DEFERRED` at exit (Django creates every FK `DEFERRABLE INITIALLY DEFERRED`, so
  this restores the default). A writer that sets an FK to row X then takes `KEY SHARE` on X at
  statement time. Against a fenced unrelated deletion of X, either the deleter's fence waits and then
  sees the reference (stale-gate refusal), or the writer waits on the fence and gets FK violation
  23503 at statement time inside its adapter.
- Error mapping inside coordinated adapters: 40P01, 55P03, and 23503 raised by a coordinated save or
  queryset write (native form and serializer validation already refuse a missing FK target, so 23503
  at save time means a concurrent delete) become `MetadataBusy` with "A referenced object was deleted
  or locked in another transaction. Nothing changed. Retry this operation."
- Tag writers: Tag keeps thin save, save_base and queryset-write adapters that take no advisory lock
  and only map 40P01 and 55P03 to `MetadataBusy`. Tag rename against replay stays protected by replay's
  `FOR KEY SHARE` (E5).
- Import view: `views/dhcp_plugin_sync.py` catches `MetadataBusy` before `except Exception` and shows
  its message.
- Section 8 (cases that still refuse) adds: concurrent target or mapping writers refuse each other
  inside a transaction (as r4); a target write whose referenced row an unrelated deletion removes.

## Round 2 dispositions (Astra, GPT-6 high, read-only; verdict NOT-RATIFIED r2)

All five round-1 blockers were reported CLOSED for their original schedules.

1. `SET CONSTRAINTS ALL IMMEDIATE` flushes unrelated pending deferred FKs. The reviewer executed the
   native squash `_dependency_order_by_references` on a branch that creates ContactGroup G, assigns
   Contact C to it and edits HostReservation R: order C UPDATE, R UPDATE, G CREATE. R's coordinated
   save would check C's pending M2M FK and fail a valid merge. `ALL DEFERRED` at exit also does not
   restore an enclosing IMMEDIATE scope. ACCEPTED. Verified `squash.py` orders UPDATE before CREATE and
   reads FK/GFK dependencies, not M2M. CLOSED in r3: no constraint-mode change anywhere.
2. 23503 at a coordinated write does not prove a concurrent delete (`QuerySet.update()` with a missing
   FK value has no validation); relation writes through `_guard_relation_write` were not covered; the
   catch must sit outside the adapter savepoint. ACCEPTED. CLOSED in r3: no 23503 classification;
   referenced rows are read explicitly under a lock, and the refusal message is true for any cause.

Non-blocking, ACCEPTED: the deletion gate itself survived; raw replay (`save_base` from the
deserializer) bypasses model adapters, and dependency prevalidation skips unmapped targets, so replay
dependency locking must be stated for every planned target write.

## Revision r3

Changes from r2:

- Removed: `SET CONSTRAINTS ALL IMMEDIATE`, `SET CONSTRAINTS ALL DEFERRED` and the 23503 mapping.
  No lifecycle code changes the transaction's constraint mode.
- Reference locks in coordinated writers. Before the native write, and after the advisory lock, each
  coordinated target or mapping writer reads the rows it will reference with one helper,
  `_lock_present(model, pks)` (`SELECT pk ... FOR KEY SHARE`, PKs sorted):
  - model `save` and `save_base`: the non-null FK values of the instance;
  - forward and reverse relation mutators (`add`, `set`, `create`, through-row inserts): the endpoint
    rows being added;
  - queryset `update`, `bulk_create`, `bulk_update`: the FK values in the written fields.
  A referenced row that the read does not return raises `AbortRequest("A referenced object does not
  exist. Nothing changed.")`. The message is true for a concurrent delete and for invalid input alike,
  so no cause is guessed and no retry is promised.
- Effect on the unrelated deletion of row X (late fence `FOR UPDATE` on X, then a reference check):
  if the writer locked X first, the fence waits for the writer's commit, sees the reference and
  refuses with the stale-gate message. If the fence locked X first, the writer's read waits, the
  deleter commits, and the writer refuses as above. A writer that ran before both commits normally.
- The importer needs no special mode: its Subnet, Pool, Reservation and mapping writes go through the
  same adapters, so a nested import gets the same protection (round 1 blocker 3).
- Replay: inside an active replay operation the writer adapters skip `_lock_present`, because native
  raw replay bypasses them and prevalidation owns dependency stability. Prevalidation runs
  `_lock_present` on the present FK and M2M dependencies of every planned target and mapping write,
  mapped or not; dependencies planned by the same replay are uncommitted and cannot be deleted by
  another transaction. Missing-dependency refusal keeps its r4 scope (protected targets and mappings).
- Error mapping stays at the adapter boundary, outside the adapter's savepoint: 40P01 and 55P03 from
  the lock statements, `_lock_present`, the fence and the native operation become `MetadataBusy`
  after Django rolls the savepoint back. No SQL runs in a `finally` on a broken transaction.
- Cases that still end in a native error: an uncoordinated core write (outside the adapter inventory)
  that references a row an unrelated deletion removes fails at COMMIT with the native FK error, as in
  NetBox without this plugin.

## Round 3 dispositions (Astra, GPT-6 high, read-only; verdict NOT-RATIFIED r3)

Both round-2 blockers were reported CLOSED.

1. The importer writes deferred FKs outside the reference-lock inventory (ClientClass C referencing
   DHCPServer S, `integrations/dhcp_plugin.py:590-611`); an unrelated deletion of S commits first and
   the import fails at COMMIT with 23503. Replay has the same gap through an ancillary row's FK.
   ACCEPTED on mechanism. Verified that `metadata_writer_models()` holds only Subnet, HostReservation,
   KeaDhcpLink and Tag (`dhcp_mapping_lifecycle.py:160-165`).
2. Replay's missing-dependency refusal covers only protected targets, so an unmapped target's M2M
   endpoint deleted first reaches COMMIT and fails with 23503 (`django .../related_descriptors.py`
   inserts through rows by numeric PK without a lookup). ACCEPTED on mechanism.

Both are the third variant of one mechanism: making every reference written by a coordinated
operation visible to an uncoordinated deletion before it commits. Rounds 1-3 each found a further
reference path (target FK, relation endpoint, ancillary model, replay). The inventory cannot be made
complete by listing paths. In each schedule the outcome is a whole-transaction rollback by the
PostgreSQL deferred FK check, not committed inconsistent state; round 2 and round 3 found no schedule
in which an uncoordinated deletion silently deletes, detaches or updates committed protected state.
r4 therefore changes the contract instead of extending the inventory.

## Revision r4

Changes from r3:

- Contract, stated as three guarantees:
  1. Coordinated operations (importer, replay, target and mapping writers, relevant deletions) exclude
     each other with the EXCLUSIVE key, exactly as r4 of the transaction design.
  2. An uncoordinated deletion never deletes, detaches or field-updates a committed Subnet,
     HostReservation or KeaDhcpLink. The late hook refuses a protected row in the collection, and the
     fence (`FOR UPDATE` on the endpoint row, then the committed-reference check) refuses a protected
     reference committed before the fence. A protected reference that commits after the fence must
     take `KEY SHARE` on the row at its COMMIT-time FK check, so it waits for the deleter and then fails.
  3. A race between an uncoordinated deletion and an uncommitted coordinated operation that references
     the deleted row ends with PostgreSQL's deferred FK check rolling back the whole coordinated
     transaction. No partial state commits. This is the same outcome as for any core NetBox writer.
- Removed: `_lock_present` in writer adapters and the replay dependency locks of r3. Replay keeps
  `FOR KEY SHARE` only on the named endpoints it validates (Tags), which replaces the r4 Tag advisory
  adapter (E5). Missing-dependency refusal keeps its r4 scope.
- COMMIT-time mapping where the lifecycle owns the COMMIT: when `coordinated_import` opened the
  transaction, a 23503 `IntegrityError` raised by its COMMIT becomes `AbortRequest("The import
  referenced an object that no longer exists. Nothing changed. Run the import again.")`. The wording
  is true for every cause. Replay COMMIT belongs to netbox-branching's job, which records the failure
  natively; a UI or REST target edit's COMMIT belongs to NetBox's view (native behaviour).
- Unchanged from r2/r3: EXCLUSIVE only; no constraint-mode changes; Tag adapters map only 40P01 and
  55P03; `MetadataBusy` before the import view's generic handler; error mapping outside the adapter
  savepoint; IPAM stale cleanup treats `MetadataBusy` as a row failure.
- Cases that end in a native FK error with full rollback (section 8 addition): a coordinated
  operation, or a branch merge, that references a row an unrelated deletion removes before the
  coordinated operation commits.

## Round 4 dispositions (Astra, GPT-6 high, read-only; verdict NOT-RATIFIED r4)

Both round-3 blockers CLOSED under guarantee 3. No counterexample to guarantee 2 was found
(collected protected rows, FK and M2M through-row references, GFK mappings, Server and ContentType
deletion, `handle_deleted_object` side effects).

1. Acceptance 1: Django schedules SET_NULL field updates without checking for matching rows
   (`django/db/models/deletion.py:69-73,378-380`), so an unrelated IP address deletion calls the
   adapted `HostReservation.objects.filter(ipv4_address=...).update(ipv4_address=None)` on an empty
   queryset, and the r1 `_uncoordinated` guard refuses it. The reviewer executed this with the Django
   6.1 Collector on SQLite. ACCEPTED.
2. Acceptance 3: a deferred FK check at the view's own COMMIT can wait on an uncoordinated deleter's
   row lock and end in 55P03 (with a configured `lock_timeout`) or 40P01 after every adapter has
   returned; NetBox's edit view catches only `AbortRequest` (`netbox/views/generic/object_views.py:354`).
   ACCEPTED.

Non-blocking, ACCEPTED: the importer refusal names the family, because the view imports families in
separate transactions; the r4 importer refusal must be `MetadataBusy` so the view's dedicated branch
shows it.

## Revision r5

Changes from r4:

- Protected writes during an uncoordinated deletion are judged by their effect. Inside
  `_uncoordinated` without `_held`, a guarded protected write (queryset `update` or `delete`,
  relation removal, through-row deletion) runs natively inside the adapter's savepoint and returns
  normally when it affected zero rows. When it affected one or more rows, the adapter rolls the
  savepoint back and raises the stale-gate refusal. Instance `save()` and `.remove()` of a specific
  protected row stay refused (they always affect a row). The row count is the count PostgreSQL
  reports for the statement, so no separate read can race it; the fence and late hooks still refuse
  every committed protected reference earlier.
- COMMIT-owning request boundary: the existing plugin middleware `BranchRefusalMiddleware`
  (`branching.py:461`, registered in `netbox_kea/__init__.py:22`, installed only with
  netbox-branching, like the lifecycle adapters) gains `process_exception`. A request that entered
  lifecycle coordination sets a request marker. When such a request raises `OperationalError` whose
  cause is 40P01 or 55P03, including at the view's COMMIT, the middleware returns the retry refusal:
  for a UI request a redirect to the referring page with the `MetadataBusy` message as an error
  message, for a REST request HTTP 409 with `{"detail": <message>}`. Other exceptions pass through.
  Django has already rolled the transaction back when the exception reaches middleware.
- The importer's COMMIT-time refusal is a `MetadataBusy` with "The DHCPv<family> import referenced an
  object that no longer exists. Nothing changed for DHCPv<family>. Run the import again."

## Round 5 dispositions (Astra, GPT-6 high, read-only; verdict NOT-RATIFIED r5)

Both round-4 blockers CLOSED. The reviewer executed the Django 6.1 Collector with the zero-row rule
(protected update returned 0, endpoint deletion completed) and Django's real request handler with the
middleware (plugin `process_exception` runs before CoreMiddleware; Django has rolled back first). No
counterexample to guarantee 2.

1. The middleware returns a response after rollback without discarding NetBox's queued events, so
   `event_tracking` flushes an event for an uncommitted change (executed: HTTP 409, zero committed rows,
   one dispatched event). ACCEPTED. NetBox's own refusal sends `clear_events`
   (`netbox/views/generic/object_views.py:354-357`).

Non-blocking, ACCEPTED: use HTTP 400 like native `AbortRequest` (`netbox/api/viewsets/__init__.py:219`);
the zero-row rule is conservative (PostgreSQL counts matched rows, so an idempotent update counts 1);
Collector deletes auto-created through rows without an adapter, so through-row safety rests on the
endpoint fence and committed-reference check, as stated in guarantee 2.

Split assessment at the round-5 cap: the remaining blocker is one missing call in request failure
handling, not a stalled mechanism. No split. r6 fixes it, and one confirmation round beyond the cap
checks only the r6 change.

## Revision r6

Changes from r5:

- The middleware's lock-error branch sends `clear_events.send(sender=None)` before it builds the
  response, as NetBox's native refusal does, and it answers a REST request with HTTP 400
  `{"detail": <message>}` (the native `AbortRequest` code) instead of 409.
