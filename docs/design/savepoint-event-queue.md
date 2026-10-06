<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Savepoint rollback and the NetBox event queue

Status: **r9 RATIFIED** (r7 rework, round 3, 2026-10-06), for #302. Sections 17-25 are the current design;
section 26 records the implementation and its one deviation (units need NetBox 4.7).
r6 (sections 0 and 2-16) was ratified, implemented locally, and then superseded by the operator constraint in
section 17: the plugin must not depend on NetBox event internals. Those sections stay as history.

## Decision (r9)

`netbox_kea/event_scope.py` `atomic(using=None)` replaces every `transaction.atomic` in the plugin. When a
request is being tracked and no connection holds a transaction, it runs the block as a **unit** inside its own
`event_tracking`: on NetBox 4.7 the unit's events dispatch after its COMMIT, or not at all when its exception
leaves the block. Inside a transaction it is a plain savepoint, and the plugin orders its refusals before its
event-producing writes; what remains there is NetBox's limit and is documented. No `events_queue`,
`EventContext` or `enqueue_event` use. Details: section 24 (with section 22), verdict in section 25.

## Decision (r6, superseded)

`netbox_kea/event_queue.py` owns "a rolled-back block leaves the NetBox event queue as it found it". Its
interface is `atomic(using=None)`, a drop-in for `transaction.atomic` at every site whose exception can be
caught, and at `_lock_boundary`.

- The first `atomic()` in a queue generation replaces the ContextVar's dict with a `JournaledQueue` holding
  the same entries and pins each lazy 4.7 payload (O(queue) once per generation).
- While journaled, each new 4.7 `EventContext` becomes a module-level `PinnedEventContext` whose payload is
  serialized at enqueue and at each coalesce (4.3 is eager natively).
- Each frame records the before-image of each key on first touch (`__getitem__`, `get`, `__setitem__`:
  `copy.copy(entry)` plus a copy of `snapshots`). On an exception, or a normal exit with the rollback mark
  set, it restores those keys, drops keys it added, and reinstalls its queue if a clear replaced it. On
  commit it merges into the parent frame on the same generation (earliest before-image wins).
- Mutators the producers never use raise `EventQueueInvariantError(RuntimeError)` before acting while a
  frame is open. Reads are plain dict reads.
- Contract: the only producers are NetBox's `enqueue_event` and the `clear_events` receiver. Limits are in
  section 0. `_events_follow_rollback` and `_event_copy` are deleted.
- Guards: opengrep `kea-caught-atomic-without-event-journal` and `kea-events-queue-outside-owner`.


## 0. Refuted claims

Each holds under r6's producer contract (section 15). Reopen when a NetBox release changes how
`enqueue_event` or the `clear_events` receiver touches the queue, or when plugin code enters
`event_tracking` inside an open transaction.

- R3.2/R4.1 (nested `event_tracking` inside a frame dispatches early): REFUTED as out of scope, round 5. The
  plugin's only `event_tracking` (`netbox_kea/jobs.py:282`) surrounds `_run_sync` before any transaction;
  NetBox enters it in middleware, async jobs and the script runner before any transaction opens.
  **Correction (implementation, 2026-10-06):** netbox-branching 1.2.1 enters `event_tracking` once per change
  inside `Branch.merge`/`revert`'s transaction (`merge_strategies/iterative.py:30,50`), which is inside the
  plugin's replay `metadata_scope()` frame. Restoration still holds: the inner scope sets and resets its own
  queue token, so the frame's queue is back in place at frame exit; with no current request (the merge job)
  the frame does not journal. The early per-change flush is netbox-branching's own behaviour and is out of
  scope.
- R4.2 (base-dict and C-level calls bypass the journal): REFUTED as out of scope, round 5. Neither producer
  uses them; the brief's acceptance 1 is per reachable site, not universal interception.
- R4.3 (retained entry references, in-place mutation of `data`/`snapshots`): REFUTED as out of scope,
  round 5. Native coalescing re-fetches `queue[key]` and replaces `postchange` and (pinned) `data`.

## 1. Brief

### Problem

NetBox queues change events in memory and dispatches them (event rules, webhooks, scripts) when the
request or job ends. The plugin opens `transaction.atomic()` blocks, often per row, and catches the
exception that rolls one back so the remaining rows continue. The rollback reverts the rows. It does not
revert the queue. So NetBox can dispatch an event for a row that never committed, or an update event whose
`postchange` snapshot and payload describe a reverted state.

cde698f2 fixed one site, `coordinated_import`, with `_events_follow_rollback`
(`netbox_kea/dhcp_mapping_lifecycle.py:1222`). It copies every queued event at entry and restores the copy
on exception. That is O(queue) per entry, so it cannot go into a per-row loop: the IPAM job's lease phase
runs 10^3 to 10^5 rows against a queue of the same order.

The decision: which module owns "a rolled-back block leaves the event queue as it found it", where its
seam sits, how it stays sub-linear in queue size per block, and what mechanical guard stops a new caught
`atomic()` from reintroducing the defect.

### NetBox facts (both supported ends: 4.3 floor, 4.7 ceiling)

- `netbox/context.py:11`: `events_queue` is a `ContextVar` holding a plain `dict`, keyed
  `"<app_label>.<model>:<pk>"`.
- `event_tracking` (`netbox/context_managers.py`) sets the queue to `{}`, and after the block flushes
  `list(queue.values())` without checking transaction state. In 4.7 it skips the flush when the block raises.
  The request middleware wraps it around Django's exception-to-response conversion, so a view exception
  still flushes.
- `JobRunner.handle` has no `event_tracking`. The plugin's IPAM job adds its own (`netbox_kea/jobs.py:282`).
- `core/signals.py` `handle_changed_object` (post_save, m2m_changed) saves an ObjectChange, then calls
  `enqueue_event`. `handle_deleted_object` (pre_delete) enqueues a delete event. Both return early without a
  current request.
- `enqueue_event`, 4.7 (`extras/events.py:109-156`): returns unless the model has the `event_rules` feature.
  A new key gets an `EventContext` (dict subclass; payload serialized lazily at dispatch from the stored
  instance). An existing key is coalesced **in place**: `queue[key]['snapshots']['postchange'] = ...`;
  a delete promotes `queue[key]['event_type']` and freezes the payload; otherwise
  `queue[key].refresh_serialization_source(instance)`.
- `enqueue_event`, 4.3: same keying and in-place coalescing, but the entry is a plain `dict` with an eager
  `data` payload, re-serialized in place on coalesce (`queue[key]['data'] = serialize_for_event(instance)`).
- `clear_events` (`core/signals.py:45`) has one receiver that replaces the whole queue with `{}`. NetBox's
  own `discard_events_on_rollback` (`netbox/api/viewsets/mixins.py:243-278`) clears the whole queue and its
  docstring says it must not be used in a loop that catches a per-object failure and continues.
- Event-producing models in play: every `netbox_dhcp` target (Subnet, Pool, DHCPServer, HostReservation,
  ClientClass, Option, OptionDefinition), ipam IPAddress/Prefix/IPRange, dcim MACAddress, `netbox_kea.Server`.
  `KeaDhcpLink` writes an ObjectChange but no event. `IPAMOwnershipLink` writes neither.

### Sites (develop cde698f2)

Reachable: an event-producing write commits to the queue, a later statement in the same atomic raises, the
exception is caught, and the outer work commits and flushes.

| Site | Writes before the raise | Caught at | Rows |
|---|---|---|---|
| `integrations/dhcp_plugin.py:666` `upsert_subnet` | Subnet save, then `observe_mapping` (KeaDhcpLink, lock errors) | `except Exception` :702 | per Subnet |
| `integrations/dhcp_plugin.py:836` `_upsert_reservation` | MACAddress, HostReservation save, m2m `set`/`remove` (coalesce in place), then `observe_mapping` | `except Exception` :881 | per Reservation, 10^3-10^4 |
| `integrations/dhcp_plugin.py:948` `import_reservation_snapshot` | `reconcile()` Prefix writes; `ipv6_prefixes.set` on HostReservations whose events committed earlier (in-place coalesce) | `except Exception` :973 | once per family |
| `ipam_reconciliation.py:753` `_each_row` | IPAddress/Prefix/IPRange save, then `_store_link`; `raise _RowRefused` :624 after the IP event | `DatabaseError, _RowRefused, DuplicateNetBoxRowsError, MetadataBusy` :755 | per row, 10^3-10^5 |

Narrow (reachable only when a later post_save/m2m receiver or a DB error raises after the event):
`dhcp_plugin.py:270, 335, 512, 610, 724`, `sync.py:108` `sync_mac_address`.
Unclear: the delete guards in `dhcp_mapping_lifecycle.py` (`_unaffected` :173, `_late_delete_guard` :1447)
raise `MetadataBusy` after a native delete may have queued a delete event, and `_each_row` catches
`MetadataBusy`. Unreachable: `ipam_reconciliation.py:408`, `config_write.py:665`, `branching.py:240`.

Callers: the DHCP import runs from `ServerDhcpPluginSyncNowView.post` under the request's
`event_tracking`, inside `coordinated_import`'s outer atomic, so its inner atomics are savepoints. The IPAM
job and the per-row views (`views/leases.py:87`, `sync_views.py:67,140,192`,
`reservation_mutations.py:335`) run `_each_row` with no outer atomic, so each row is a top-level
transaction.

### Constraints

- No change to NetBox or netbox_dhcp. Works on NetBox 4.3 through 4.7.
- Per-row tolerance stays: a failed row is reported and the run continues.
- Cost per protected block is independent of the queue size (at most proportional to the entries that
  block touches).
- One mechanism for every site, `coordinated_import` included. The old helper is deleted, not kept beside it.
- Fail fast on unexpected state: no silent fallback when the queue is not in the shape the mechanism expects.

### Observable acceptance conditions

1. For each reachable site, a test runs the real code under `event_tracking` with the event recorder
   (`netbox_kea/tests/utils.py:107-113`), makes a statement after an event-producing write raise inside the
   block, lets the site catch it, commits, and asserts: no dispatched event names a row that does not exist;
   an event whose key was queued before the block carries the snapshot and payload it had before the block.
   Each test fails on cde698f2.
2. A rolled-back block that touched none of the earlier entries leaves them unchanged, and a block that
   succeeds leaves its events queued for the caller.
3. A loop of 10^4 protected blocks over a queue of 10^4 entries costs time linear in the loop, not
   quadratic (stated as a measured bound in a test or benchmark).
4. An opengrep rule in `.opengrep/kea-rules.yaml` flags an `atomic()` block whose exception is caught by the
   enclosing code without the chosen mechanism. It fires on cde698f2's reachable sites and is silent on the
   fixed tree and on its `ok:` fixtures.
5. `test_dhcp_mapping_recovery.py:3931` (nested import restores the caller's queue) still passes.

## 2. Design A (Claude, drafted blind to design B)

### Owner and interface

New module `netbox_kea/event_queue.py` owns "a rolled-back block leaves the event queue as it found it".
Its interface is one context manager, a drop-in for `transaction.atomic`:

```python
@contextmanager
def atomic(using: str | None = None) -> Iterator[None]: ...
```

- Opens `transaction.atomic(using=using)` and a journal frame on the current queue.
- On an exception that leaves the block, or on a normal exit with `transaction.get_rollback(using)` set
  (an exception caught *inside* the block marks it for rollback, and Django then rolls back silently), it
  restores every entry the block touched and drops every key the block added, then re-raises if there was
  an exception.
- On commit (savepoint release), the frame merges into its parent frame: the block's events stay queued for
  the caller.
- Cost: O(1) to enter and leave, plus O(entries touched) on rollback or merge. The first block in an
  `event_tracking` scope converts the queue (one O(queue) pass per scope, not per block).
- No current request: NetBox enqueues nothing (`handle_changed_object` returns early), so the journal is a
  no-op.

### Mechanism: a journaling queue

`enqueue_event` touches the queue only through `key in queue`, `queue[key]` and `queue[key] = ...` on both
4.3 and 4.7, and `event_tracking` reads the queue back through `events_queue.get()` at the end. So the
first `atomic()` in a scope replaces the ContextVar value with `JournaledQueue(dict)` holding the same
entries. The subclass overrides `__getitem__` and `__setitem__`: on the first touch of a key in the
innermost open frame, it records the entry's pre-block state (or "absent" for a new key). Mutating methods
the enqueue path never uses (`__delitem__`, `pop`, `popitem`, `clear`, `update`, `setdefault`) raise while a
frame is open, so a NetBox change that starts using them fails a test instead of escaping the journal.

Pre-block state of an entry:

- 4.3 plain dict: shallow copy plus a copy of `snapshots` (`data` is reassigned, not mutated).
- 4.7 `EventContext` (`UserDict`): `copy.copy` (copies `.data` and `_serialization_source`), plus a copy of
  `snapshots`, plus the payload pinned: read `entry["data"]` once on the copy so the restored entry carries
  the pre-block serialization. Without the pin, the restored entry would serialize at dispatch from an
  instance the reverted block may have changed in memory.

`clear_events` inside a frame replaces the ContextVar with a new plain `{}`. On exit the frame finds the
ContextVar no longer holds its `JournaledQueue`; it then does nothing (the clear discarded the block's
events and everything before them, which is NetBox's intent), and the next `atomic()` installs a fresh
journal on the new dict.

### Sites

Every site in the brief table, the narrow ones included, replaces `transaction.atomic()` with
`event_queue.atomic()`. `_lock_boundary` (`dhcp_mapping_lifecycle.py:97-110`) opens its atomic through
`event_queue.atomic()`, which covers `metadata_scope`, so `coordinated_import` drops
`_events_follow_rollback` and `_event_copy` (deleted). The delete guards then roll back their own queued
delete event when they raise.

### Guard

opengrep rule `kea-caught-atomic-without-event-journal`: inside `try: ... except ...: ...` (any handler),
flag `with transaction.atomic(...)` and `with atomic(...)` where `atomic` is imported from
`django.db.transaction`. Exclude `netbox_kea/event_queue.py`. Limits, stated: a catch in a caller of the
function that opens the atomic is not seen, and neither is `@transaction.atomic` as a decorator.

### Rejected

- Copy the whole queue per block (status quo `_events_follow_rollback`): O(queue) per row.
- Receivers on `pre_save`/`m2m_changed pre_*`/`pre_delete` that snapshot the touched key: core's
  `handle_deleted_object` is itself a `pre_delete` receiver connected first, so the snapshot would come
  after the mutation.
- `clear_events` in the per-row catch: discards every committed row's events (NetBox's own docstring).
- Filter at flush (drop events whose row is gone): cannot detect a reverted update of an existing row.

## 3. Design B (Codex gpt-6-astra, effort high, read-only, blind)

Inputs isolated: B received section 1 only (problem, NetBox facts, sites, constraints, acceptance), in a
fresh `codex exec` context; design A was written to this file after B launched and B's sandbox was
read-only. Summary of B:

- Owner `netbox_kea/event_transactions.py`: `atomic_events(using=...)` plus `register()`.
- Seam: runtime adapters replace NetBox's `enqueue_event` and the `clear_events` receiver. The adapter runs
  native `enqueue_event` against a temporary empty queue, materializes the payload (eager on 4.7), stores an
  immutable record, and **reimplements coalescing** (keep prechange, replace postchange and payload, promote
  to delete).
- Undo log per frame `(generation, key, previous-or-ABSENT)`, replayed backwards; `clear` journals the old
  queue by reference so `write, clear, write, rollback` restores the entry queue exactly.
- `EventQueueInvariantError` on unexpected state; broad per-row handlers must re-raise it.
- Apply at every site, narrow ones included, and at `_lock_boundary`; exclude `config_write._serialized`
  from the rule.

## 4. Merge

| Decision | A | B | Evidence | Disposition | Consequence |
|---|---|---|---|---|---|
| Interception seam | `JournaledQueue(dict)` overriding `__getitem__`/`__setitem__`; native coalescing unchanged | Adapter replacing `enqueue_event`, coalescing reimplemented | `enqueue_event` touches the queue only by `in`, `[]`, `[]=` on 4.3 and 4.7 (section 1). `core/signals.py` imports `enqueue_event` by name, so B must patch that binding and every other importer; B names this as its main risk. Deletion test: B's adapter duplicates a NetBox function that changed shape between 4.3 and 4.7. | **A**, open for round 1 | Locality: one subclass, no copy of NetBox logic. Test that differs: a NetBox release that changes coalescing breaks B silently, A not at all. |
| 4.7 lazy payload after rollback | Pin `data` on the saved copy at first touch | Eager serialization at every enqueue | **A's pin is wrong**: the first touch happens in `post_save`, after the block changed the instance, so the pin captures block state. B is right that the restored entry must not serialize from an instance the reverted block changed. | **Neither as drafted. r1: on rollback, re-source each restored `EventContext` from the database** (`_serialization_source = type(src)._default_manager.get(pk=src.pk)`), O(restored entries) queries, no mutation of the caller's instance; the reverted block left that row in its pre-block state, so the payload serializes the committed state. 4.3 needs nothing (eager `data` is in the copy). | Keeps 4.7 lazy semantics on the success path. Test: same Python instance saved in a committed block, then changed and saved in a reverted block; dispatched `data` shows the committed value. |
| `clear_events` inside a block | Detach: clear wins, frame does nothing | Journal old queue by reference; rollback restores it exactly | Brief acceptance 2: a rolled-back block leaves the queue as it found it. | **B**: on rollback the frame reinstalls its `JournaledQueue` (`events_queue.set`) after undoing its own touches; on success the replacement stands. O(1). | Test: write, clear, write, rollback restores the entry queue. |
| Rollback without exception | `get_rollback(using)` at exit restores | "Explicit rollback marking must also discard the frame" | Same point. | Agreed | Test: caught exception inside the block, normal exit. |
| Fail-fast error vs broad handlers | Unsupported mutators raise while a frame is open | `EventQueueInvariantError`; broad handlers re-raise it first | Sites catch `Exception` (`dhcp_plugin.py:702,881,973`), which would swallow an invariant error and continue. | **B, adapted**: the invariant error is raised from the journal at **exit**, outside the site's own block but inside its try; so sites re-raise it. **Undispositioned for round 1**: whether a narrower mechanism (raise a `BaseException` subclass) is acceptable. | |
| `config_write._serialized` | Use the wrapper (cheap) | Exclude from the rule | It holds only the advisory lock (`config_write.py:699`). | **A**: no exclusion list to maintain; cost is O(1) when nothing is touched. | One fewer special case in the rule. |
| Narrow and delete-guard sites, `_lock_boundary` | Apply everywhere, wrap `_lock_boundary` | Same | Agreed | Agreed | `coordinated_import` loses `_events_follow_rollback`/`_event_copy`. |
| Queue conversion cost | First block per scope converts the plain dict (one O(queue) pass) | No conversion (adapter owns records) | Per-scope, not per-block. | A, open for round 1 | Acceptance 3 measures blocks, not scopes. |

Unresolved for round 1: the fail-fast error channel through broad handlers; whether the per-scope
conversion is acceptable; whether `__getitem__` interception misses any queue access on 4.3/4.7.

## 5. r1 (merged candidate)

Design A with these changes from the merge: (1) no payload pin; on rollback each restored 4.7
`EventContext` is re-sourced from the database; (2) on rollback a frame reinstalls its queue if a clear
replaced it; (3) `EventQueueInvariantError(RuntimeError)` for unexpected state, raised at frame exit, and
every broad per-row handler re-raises it before its own recovery; (4) every caught `atomic()` and
`_lock_boundary` use `event_queue.atomic()`, with no rule exclusion except the owner module.

## 6. Round 1 (Codex gpt-6-astra, high, read-only): NOT RATIFIED r1

| # | Finding | Severity | Verified by Claude | State |
|---|---|---|---|---|
| R1.1 | An earlier event holds instance A; a block changes A in memory and fails before any enqueue; nothing touches the queue, and 4.7 serializes A's reverted state at dispatch. | blocker | Yes: `EventContext.__getitem__` serializes `_serialization_source` at first `data` read (section 1); reviewer's Python model printed `dispatched= {'value': 'failed'}`. | ACCEPTED, closed in r2 by (1) |
| R1.2 | Re-sourcing from the database reads another writer's commit, or raises on a concurrent delete; it also wrongly touches frozen delete events (`_serialization_source` is None). | blocker | Yes: same `EventContext` code; a new `.get()` after rollback is read-committed, not the historical state. | ACCEPTED, re-sourcing removed in r2 |
| R1.3 | Re-raising in per-row handlers is not fail-fast: `jobs.py:377` catches `Exception` per server and continues; `sync_views.py:71` catches `RuntimeError`; a view exception still flushes. | major | Yes: `jobs.py:368-379`, `views/sync_views.py:71`. | ACCEPTED, closed in r2 by (3) |
| R1.4 | "Cost independent of queue size" contradicts the first-block conversion; a clear starts a new generation that converts again. | minor | Yes. | ACCEPTED, contract restated in r2 by (4) |

Section-4 dispositions: interception seam CONFIRMED for 4.3/4.7 enqueue (executed: `q[k]` and `q[k][f] = v`
hit an overridden `__getitem__`; `get`, `values`, `items`, `setdefault`, `update`, `|=` bypass it); clear
handling CONFIRMED (44,990 randomized rollback comparisons in a Python model; merge rule: earliest
before-image wins, merged only into a parent frame on the same queue generation); rollback-without-exception
CONFIRMED (capture the flag before Django's exit, restore after the rollback); `config_write` wrapping and
the site scope CONFIRMED.

## 7. r2

Changes from r1, verbatim:

1. **Payload pinned at enqueue while journaled.** Every entry in a `JournaledQueue` carries a materialized
   payload. On 4.3 that is native (eager `data`). On 4.7, `__setitem__` of a new `EventContext` swaps its
   class in place to `PinnedEventContext(EventContext)` and reads `entry["data"]` once; the subclass
   overrides `refresh_serialization_source` to call the parent and then read `data` again. So the payload is
   serialized from the instance at the moment NetBox enqueues or coalesces it, the same moment the
   `postchange` snapshot is taken, and dispatch never reads the instance again. Frozen delete events are
   unchanged (`freeze_data` already materializes). Native coalescing stays NetBox's.
2. **Rollback restores copies only.** A frame's before-image is `copy.copy(entry)` plus a copy of
   `snapshots`; because the payload is materialized, the copy carries the historical payload. No database
   read on rollback.
3. **Fail fast at the tracking boundary, not in handlers.** On an invariant violation (an unsupported
   mutator while journaled, a queue of an unexpected type, a frame closed out of order) the journal marks the
   queue poisoned and raises `EventQueueInvariantError(RuntimeError)`. A poisoned queue raises the same error
   from `values()`, `items()`, `__iter__` and every later `atomic()` entry. `event_tracking` reads
   `values()` to flush, so a poisoned request or job ends in an error and dispatches nothing, whatever
   handler swallowed the first raise. No handler edits. Violations detected inside the block raise inside the
   atomic, so the transaction rolls back.
4. **Cost contract.** Initialization is O(queue) once per queue generation (the first `atomic()` after
   `event_tracking` starts or after a clear): it copies the entries into the `JournaledQueue` and pins each
   lazy payload. Each block then costs O(1) to enter and exit plus O(entries touched) on rollback or merge.
   Every journaled enqueue costs one serialization, which is NetBox 4.3's native cost.
5. Unsupported mutators guarded: `__delitem__`, `pop`, `popitem`, `clear`, `update`, `setdefault`, `__ior__`.
   Native-enqueue integration tests run on both NetBox ends.

## 8. Round 2 (Codex gpt-6-astra, high, read-only): NOT RATIFIED r2

R1.1 CLOSED (executed model: a pinned payload stays `committed` after the instance changes without an
enqueue). R1.2 CLOSED (no database read; frozen delete keeps its payload). R1.4 CLOSED (10,000/20,000 entries
initialize with exactly 10,000/20,000 serializations, no reconversion). R1.3 NOT-CLOSED, now R2.1.

| # | Finding | Severity | Verified by Claude | State |
|---|---|---|---|---|
| R2.1 | Poison lives on the queue object. A native `clear_events` installs a new `{}`, and a settled rollback can reinstall an older clean generation; either way the poison is lost and tracking flushes. Executed: poison, catch, clear, enqueue: dispatches `later-event`; outer frame on Q0, clear, child poisons Q1, outer rolls back: Q0 dispatches. | major | Yes: clear receiver `events_queue.set({})` (section 1) and r2 (3) attach the flag to the queue. | ACCEPTED, closed in r3 by (1) |

Also confirmed: the in-place class swap keeps `isinstance(entry, EventContext)` and survives `copy.copy`
and pickle; NetBox 4.7's built-in actions pass payload and snapshots, not the wrapper, to RQ. Pinning changes
timing: a related-row change without a new enqueue of this object no longer shows in its payload. Raising
inside the atomic rolls back only when the exception reaches the block's exit.

## 9. r3

Changes from r2, verbatim:

1. **Poison is scoped to the tracking request, not to a queue object.** The module keeps
   `_poisoned_request: ContextVar[object | None]`. A violation sets it to `current_request.get()`. The scope
   is poisoned while `_poisoned_request.get() is current_request.get()`, so a new request or job (a new request
   object) starts clean, and a nested `event_tracking` with another request object has its own state. Every
   place the module installs a queue (generation initialization, frame rollback reinstall) installs it
   poisoned when the scope is poisoned. The module connects a `clear_events` receiver, connected after
   NetBox's core receiver so it runs second; when the scope is poisoned, it replaces the new `{}` with an
   empty poisoned `JournaledQueue`. So the final `values()` at the tracking boundary raises after any sequence
   of clears, restores and enqueues, including a final clear with no later block. Regressions: the two
   executed round-2 sequences and the final-clear sequence.
2. **Payload semantics stated.** While journaled, an event's payload is the serialization at its last
   enqueue or coalesce, taken with its `postchange` snapshot. A related-row change that does not enqueue
   this object again does not change its payload (native 4.7 serializes at dispatch instead). This is the
   price of an exact rollback; 4.3 already behaves this way.
3. **Wording.** An invariant violation detected inside a block raises inside the atomic; it rolls the block
   back when it reaches the block's exit, which the poisoning makes irrelevant to dispatch safety. Rows
   committed by earlier top-level blocks stay committed; a poisoned scope dispatches none of their events,
   and the request errors or the job ends errored.
4. `PinnedEventContext` is a module-level class in `netbox_kea/event_queue.py`, so a custom pipeline that
   pickles the entry can import it in a worker.

## 10. Round 3 (Codex gpt-6-astra, high, read-only): NOT RATIFIED r3

The three round-2 sequences pass on an r3 model (`InvariantError; dispatched=[]`), also with NetBox's
`event_tracking` source extracted and run. Two new counterexamples on the same mechanism:

| # | Finding | Severity | Verified by Claude | State |
|---|---|---|---|---|
| R3.1 | Nested tracking with another request: the inner violation overwrites `_poisoned_request`; native tracking restores `current_request` but not the plugin variable, so the outer scope dispatches `outer-later-event`. | major | Yes: `event_tracking` resets only its own variables (section 1). | ACCEPTED, removed with the mechanism in r4 |
| R3.2 | Nested tracking with the same request installs a native `{}` that no hook sees; the inner scope dispatches before the outer raises. | major | Yes: same source. | ACCEPTED, removed with the mechanism in r4 |

Also reported: a clear with no current request makes `None is None` read as poisoned; receiver order is an
implementation gate. Both are moot in r4.

Poisoning churned for two rounds (R1.3 -> R2.1 -> R3.1/R3.2): each fix moved the lost-state case to a new
installation path. The general cause is that r2 let a violation happen after the queue was already in a
state the journal did not track, so the error had to be carried to the boundary. r4 removes that cause.

## 11. r4

Changes from r3, verbatim:

1. **Poisoning is removed** (the `_poisoned_request` variable, the poisoned queue, the extra `clear_events`
   receiver, and the "raise from `values()`" rule).
2. **The journal is total or it refuses first.** While a frame is open on a `JournaledQueue`, every dict
   operation either records the before-image of each key it can change or hand out, or raises
   `EventQueueInvariantError(RuntimeError)` **before** it reads or changes anything:
   - journaled (before-image of the one key, O(1)): `__getitem__`, `get`, `__setitem__`, `setdefault`,
     `__delitem__`, `pop`;
   - refused while a frame is open: `__iter__`, `keys`, `values`, `items`, `popitem`, `clear`, `update`,
     `__ior__`, `copy`, `__reduce__`/`__reduce_ex__`;
   - allowed without journaling: `__contains__`, `__len__`, equality.
   With no frame open, every operation is the plain dict operation (the tracking flush reads `values()`
   after the outermost block has closed).
3. **Invariant.** Every reference to an entry that leaves the queue while a frame is open passes through a
   journaled method, and every refused method raises before acting. So the queue after a frame's rollback
   equals the queue at the frame's entry, whatever handler later swallows a refusal. A swallowed refusal
   fails that row (the site reports it) and leaves no untracked state; nothing must be carried to the
   tracking boundary. Fail-fast means: the refusal is raised at the operation that NetBox or the plugin
   performed, inside the block, and the site's row report shows it.
4. **Unexpected queue type** (the ContextVar holds neither a `dict` nor a `JournaledQueue`) raises the same
   error at `atomic()` entry, before the transaction opens.
5. Implementation gate: integration tests on NetBox 4.3 and 4.7 run native `enqueue_event` (create,
   update, m2m, delete, coalesce) inside a frame and assert no refusal, so a NetBox release that starts using
   a refused method fails CI instead of production.

## 12. Round 4 (Codex gpt-6-astra, high, read-only): NOT RATIFIED r4

Executed model with NetBox 4.7's `enqueue_event` and `event_tracking` extracted: 10,000 traces, 24,916
rollback comparisons, **0 restoration violations** with nested tracking outside frames, across clears,
native create/update/delete/coalesce, nested commit/rollback and 42,071 swallowed refusals. R2.1 and R3.1
CLOSED (no poison state remains).

| # | Finding | Severity | Verified by Claude | State |
|---|---|---|---|---|
| R4.1 | `event_tracking` entered **inside** an open frame flushes at its own exit, before the enclosing transaction resolves; 4,218 of 10,000 traces dispatched a later-reverted write. | blocker | Mechanism yes. Reachability no: the plugin's only `event_tracking` is `jobs.py:282`, entered before `_run_sync` opens any transaction; NetBox enters it in request middleware and in its own async jobs, outermost. Native NetBox has the same property without any savepoint (an inner flush dispatches before an outer commit). | ACCEPTED as a stated precondition plus a guard in r5 (2) |
| R4.2 | The operation list is not total on CPython: base-dict calls (`dict.get(q, k)`, `dict.__setitem__`, C `PyDict_*`), `q.__init__`, and `==` that hands an entry to a foreign `__eq__` bypass the journal. | major | Yes, as Python semantics. No such call exists on the native enqueue/clear path (section 1) or in the plugin outside `dhcp_mapping_lifecycle.py:1225-1231`, which r5 deletes. | ACCEPTED: r4's universal claim retracted in r5 (1) |
| R4.3 | A caller that keeps an entry reference across frames, or mutates nested `data`/`snapshots` in place, defeats the shallow before-image. | major | Yes, as semantics. Native enqueue re-fetches `queue[key]` on every call, replaces `postchange` by assignment into the copied `snapshots`, and (with pinning) replaces `data`, never mutating it. | ACCEPTED: contract in r5 (1) |
| R4.4 | `test_dhcp_mapping_recovery.py:3940` iterates the queue inside the block, which r4 refuses. | minor | Yes. | ACCEPTED: r5 (3) allows iteration |

## 13. r5

Changes from r4, verbatim:

1. **Contract, not universality.** The journal guarantees exact restoration for the queue's two producers:
   NetBox's `enqueue_event` (through `handle_changed_object` and `handle_deleted_object`) and the
   `clear_events` receiver, on 4.3 and 4.7. It does not defend against other in-process code that reaches
   the dict through base-class calls, keeps entry references across frames, or mutates entry contents.
   Enforcement: (a) an opengrep rule `kea-events-queue-outside-owner` flags any `events_queue` import or use
   in `netbox_kea/` outside `netbox_kea/event_queue.py` (tests excluded); (b) integration tests on both NetBox
   ends drive native create, update, m2m, delete and coalesce inside frames and compare the dispatched
   events with a run where the reverted block never happened. A NetBox release that changes the producers'
   access pattern fails (b) in CI.
2. **Precondition: no `event_tracking` inside an open frame.** `atomic()` records the current request on
   entry; on exit it raises `EventQueueInvariantError` if `current_request` or the queue's generation
   changed to a scope the frame did not open (the visible sign of a tracking scope entered inside it). Sweep
   evidence: the only plugin `event_tracking` is `jobs.py:282`, outside any transaction. This detects the
   nesting after the fact; it cannot retract an inner flush, which native NetBox also performs before an
   outer commit. Stated as a limit.
3. **Refusals reduced to tripwires on mutators the producers never use**: `__delitem__`, `pop`, `popitem`,
   `clear`, `update`, `setdefault`, `__ior__`, `__init__` after construction, raised before acting while a
   frame is open. Reads (`__iter__`, `keys`, `values`, `items`, `==`, `len`, `in`) are plain dict reads, so
   the existing regression at `test_dhcp_mapping_recovery.py:3940` runs unchanged.
4. Journaled: `__getitem__`, `get`, `__setitem__` (first touch per frame records `copy.copy(entry)` plus a
   copy of `snapshots`; with pinning, `data` is never mutated in place by a producer).

## 14. Round 5 (Codex gpt-6-astra, high, read-only): NOT RATIFIED r5 as written

The brief does not require defending against arbitrary in-process queue mutation (acceptance 1 is "for
each reachable site"). No production path enters tracking inside a plugin frame. R1.3 and R4.4 CLOSED;
R3.2, R4.1, R4.2, R4.3 REFUTED as out of scope (section 0). Fuzz restricted to the r5 contract, seed 4704:

| Extracted NetBox source | Traces | Rollback comparisons | Restoration violations | Reverted-write dispatches |
|---|---:|---:|---:|---:|
| 4.3 | 10,000 | 31,713 | 0 | 0 |
| 4.7 | 10,000 | 31,713 | 0 | 0 |

| # | Finding | Severity | Verified by Claude | State |
|---|---|---|---|---|
| R5.1 | r5 (2)'s exit detector cannot detect nesting: native tracking restores its ContextVar tokens before the frame exits, so the frame sees its own request and queue; and a legitimate clear changes the generation, so a mismatch check rejects 36,949 of 70,592 contract-valid exits. | major | Yes: `event_tracking` resets its tokens in `finally` (section 1). | ACCEPTED: detector removed in r6 |

Implementation gate (not a finding): the opengrep rule must be shown to catch an aliased import,
`context.events_queue`, and a literal `getattr(context, "events_queue")`; computed reflection is a stated
limit.

Split at the round-5 cap: the deferred mechanism is nesting detection (R5.1). The retained core is the
journal with the audited precondition; the reviewer states its restoration correctness does not depend on
the detector, and acceptance 1-5 make no claim about nested tracking. So the core stands alone. It gets one
verdict round.

## 15. r6 (rescoped core)

Changes from r5, verbatim:

1. r5 (2)'s exit detector and its detection claim are removed. The precondition stays: no `event_tracking`
   inside an open frame, audited by the sweep in section 0, and stated in the module docstring as a limit.
2. The opengrep rule `kea-events-queue-outside-owner` matches, in `netbox_kea/` outside
   `netbox_kea/event_queue.py` and the tests: any import of `events_queue` from `netbox.context` (aliased
   or not), any attribute access `$X.events_queue`, and a literal `getattr($X, "events_queue")`. Its fixture
   carries one `ruleid` case per form and `ok` cases for the owner module's path pattern. Computed reflection
   (`getattr(x, name)` with a variable) is a stated limit.

Everything else is r5 sections 13 (1), (3), (4) with r1-r4's accepted changes.

## 16. Round 6 (Codex gpt-6-astra, high, read-only): RATIFY r6

R5.1 CLOSED. No blocker or major within scope. Uncertainty stated by the reviewer: design ratification, not
implementation validation; section 14's fuzz used extracted NetBox source in memory, not the ORM.

Open for implementation: CI's unit-test job runs a single NetBox release (`.github/workflows/ci.yml:18-25`);
the 4.3 floor runs only the browser suite. The 4.3 producer gate in section 13 (1)(b) therefore needs either
a 4.3 unit-test leg or an explicit decision to rely on the extracted-source proof for 4.3.

## 17. Rework brief (r7): no NetBox event internals

### Operator decision (2026-10-06)

r6 swaps the per-request queue object, changes the class of NetBox's `EventContext` entries, and depends on
how `enqueue_event` reads the queue. The operator rejected that: the plugin does its best with NetBox as it
is, and does not depend on NetBox internals. A shortcoming that NetBox's own design causes is accepted and
stated, not worked around.

### Problem (class)

A plugin `atomic()` block makes an event-producing write, a later statement in the same block raises, the
plugin catches the exception, and the request or job continues and flushes. NetBox then dispatches an event
for a write that rolled back. The decision: which module owns "a caught failure does not dispatch an event
for a reverted write" with only the allowed surface, where the seam sits, what is accepted as NetBox's
limit, and what mechanical guard stops new sites.

### Allowed and forbidden NetBox surface

Forbidden in `netbox_kea/` outside tests: reads or writes of `netbox.context.events_queue`; any use of
`extras.events.EventContext` or `enqueue_event`; any assumption about how the queue is keyed, coalesced or
serialized; replacing or wrapping any NetBox callable.

Facts for the designers to dispose of (NetBox 4.7.0 source unless stated):

- `event_tracking` (`netbox/context_managers.py`) is a registered request processor. The plugin already
  enters it for the IPAM job (`netbox_kea/jobs.py:282`), because `JobRunner.handle` has none. In 4.7 it
  flushes only when its block completes without an exception; section 1 records that 4.3 flushes always.
- `core.signals.clear_events`: its one receiver replaces the whole queue. NetBox's own generic views send it
  on a failed write (`netbox/views/generic/object_views.py:357,502,507,638`, `bulk_views.py` 10 sites), and
  the API uses it through `discard_events_on_rollback` (`netbox/api/viewsets/mixins.py`), whose docstring
  says it must not be used in a loop that catches a per-object failure and continues. 4.6.x release notes
  #22934 and #22978 added these discards. The plugin already sends it (`netbox_kea/branching.py:546`). It is
  not in the plugin development docs.
- The plugin `events_pipeline` setting is documented, but NetBox appends plugin handlers after
  `extras.events.process_event_queue` (`netbox/settings.py:949-951,1030-1033`), so a plugin handler runs
  after dispatch and cannot filter.
- Overriding the `EVENTS_PIPELINE` Django setting in tests (`netbox_kea/tests/utils.py:107-113`) is how the
  test suite observes dispatch. That is test-only and allowed.
- `post_save`, `m2m_changed` and `pre_delete` queue the event (section 1). A write that raises before its
  `post_save` queues nothing.

### Evidence: what raises after the event-producing write (develop cde698f2)

| Site | Event-producing writes | Statements after them that can raise | Caught at |
|---|---|---|---|
| `integrations/dhcp_plugin.py:666` `upsert_subnet` | Subnet `save` | `observe_mapping` (KeaDhcpLink write; `_lock_boundary` maps a lock error to `MetadataBusy`; `MappingUnavailable`, `AbortRequest`) | `except Exception` :702 |
| `integrations/dhcp_plugin.py:836` `_upsert_reservation` | HostReservation `save`, `ipv6_addresses.set`, `ipv6_prefixes.remove` (coalesce) | the m2m writes themselves; `_link_reservation` -> `observe_mapping` | `except Exception` :881 |
| `integrations/dhcp_plugin.py:948` `import_reservation_snapshot` | `reconcile()` Prefix writes, then `obj.ipv6_prefixes.set` per HostReservation | each later `reconcile` row, each `set`, any later statement in the loop | `except Exception` :973 |
| `ipam_reconciliation.py:753` `_each_row` via `claim().apply` (:615-625) | `_claim` writes the IPAddress, `sync_mac_address` writes MACAddress | `raise _RowRefused` :624 when a Reservation MAC does not resolve; `_store_link` and the DB | `DatabaseError, _RowRefused, DuplicateNetBoxRowsError, MetadataBusy` :755 |
| `dhcp_mapping_lifecycle.py:1242` `coordinated_import` | the whole import | anything that escapes the import, including the deferred-FK `IntegrityError` at COMMIT | caller; `_events_follow_rollback` :1223 restores the queue today (uses `events_queue`, forbidden) |

Narrow sites and the delete guards are listed in section 1 ("Sites").

### Constraints

- No change to NetBox or netbox_dhcp. NetBox 4.3 through 4.7.
- Per-row tolerance stays: a failed row is reported and the run continues.
- No cost proportional to the queue per row (the IPAM job runs 10^3 to 10^5 rows).
- Fail fast, no silent fallback.
- Finish the replacement: `netbox_kea/event_queue.py`, `_events_follow_rollback` and `_event_copy` are
  deleted, and the r6 opengrep rules are replaced or removed.

### Observable acceptance conditions

1. No `events_queue`, `EventContext` or `enqueue_event` reference in `netbox_kea/` outside tests, enforced
   by an opengrep rule with `ruleid` and `ok` fixtures.
2. For each failure that the plugin itself raises after an event-producing write at a reachable site, a test
   under `event_tracking` with the event recorder asserts that no dispatched event names a reverted write.
   Each test fails on cde698f2 or on the site's pre-fix order.
3. Each failure mode left unfixed as NetBox's limit is listed in this record and in the user documentation,
   with the observable effect (which events dispatch for which reverted writes).
4. A guard stops a new plugin-raised failure from following an event-producing write in a caught block. Its
   limits are stated.
5. CI runs the rollback-event tests on a NetBox 4.3 unit-test leg as well as on 4.7.
6. Per-row tolerance holds, and `test_dhcp_mapping_recovery.py:3931` (nested import, caller's queue) is
   either still passing or replaced by a test of the stated behaviour.

## 18. Design A (r7, Claude, drafted blind to design B)

### Owner and interface

No new module. NetBox owns its event queue, and the plugin owns only the order of its own writes. The rule is
a site convention, and a guard enforces it: **in a block whose exception the plugin catches, every check that
the plugin can refuse runs before the first event-producing write.** After that write, only the database or
another app's receiver can fail the block. Those failures are NetBox's limit.

### Per site

- `_each_row` / `claim().apply` (`ipam_reconciliation.py:615-625`): the Reservation MAC check moves before
  `_claim`. It becomes a pure parse (`EUI(hw_address)`), so `_RowRefused` for an unparseable MAC is raised
  before any write. `sync_mac_address` keeps its own savepoint. Its DB-error `None` after the IP write stops
  being a refusal: the row records the missing MAC as a row warning and keeps the IP. Open: whether a
  Reservation without its MAC row is acceptable, or must stay a refusal (then it is an accepted limit).
- `_claim`'s own `_RowRefused` (:949) already runs before its writes. No change.
- `upsert_subnet` / `_upsert_reservation`: `observe_mapping`'s plugin-raised failures cannot occur here.
  Inside `coordinated_import`, `metadata_scope` is already held, so the inner try-lock re-enters, and the import
  refuses in a branch, so `MappingUnavailable` cannot occur. What remains after the target save is the
  database: an `IntegrityError` on a `KeaDhcpLink` constraint, a lock timeout mapped to `MetadataBusy`, and the
  m2m writes. Accepted limit.
- `import_reservation_snapshot`: a database failure only. Accepted limit.
- `coordinated_import`: `_events_follow_rollback` and `_event_copy` are deleted. When the import's exception
  escapes to the view, the failed request rolled back as a whole. The view then sends `clear_events`, as
  NetBox's own generic views do on a failed write (`object_views.py:357` and others), and as
  `branching.py:546` already does for a lock conflict. A caller that catches the import's failure and
  commits other work keeps the import's events dispatched. Accepted limit.
- `event_queue.py` is deleted. Every `event_queue.atomic` returns to `transaction.atomic`.

### Accepted limits (NetBox's design, no public surface closes them)

A database error, or a receiver of another app, that fails a caught block after an event-producing write
dispatches that write's event, although the write rolled back. On 4.7 that applies only to blocks that the
plugin catches. An exception that leaves `event_tracking` dispatches nothing. On 4.3 the flush is
unconditional.

### Guard

opengrep `kea-refusal-after-event-write`: inside `try: ... with transaction.atomic(...): ... except`, flag a
`raise` that follows `$X.save(...)`, `$X.delete(...)`, `$M.set(...)`, `$M.add(...)`, `$M.remove(...)`,
`$Q.create(...)` or `$Q.get_or_create(...)` in the same block. Limits: it cannot see writes or raises inside
called helpers. Also `kea-no-event-queue-internals`: flag any `events_queue`, `EventContext` or `enqueue_event`
use in `netbox_kea/` outside tests.

### Tests and CI

- One test per moved refusal (the MAC parse) that fails on the old order, and one for the view's
  `clear_events` on a failed import.
- One test per accepted limit that pins the observable effect. It fails when NetBox starts discarding these
  events, which tells us that the limit has gone.
- The r6 journal tests are deleted with the module.
- CI: a unit-test leg on NetBox 4.3 that runs the rollback-event test modules.

## 19. Design B (r7, Codex gpt-6-astra, effort high, read-only, blind)

Inputs isolated: B received sections 1 and 17 only (as separate files), the develop worktree at cde698f2
(which has no `event_queue.py`), and a read-only copy of the NetBox 4.7.0 event sources. It was told not to
read this record. Summary:

- New `netbox_kea/event_transactions.py`: `event_batch()` (operation scope; nested batches join) and
  `event_atomic()` (requires a batch; a rollback marks the batch failed). A failed batch sends `clear_events`
  once at outermost exit.
- Batches around `claim()`, `reconcile()`, `run_dhcp_plugin_import()`, `coordinated_import` (outside
  `metadata_scope`), `sync_mac_address`, and the job's `_run_sync` inside `event_tracking`.
- Accepted limit: **one failed row discards every pending event of the operation**, including committed
  rows, the other family, and caller events. Rows and ObjectChanges stay.
- Guard: forbid raw `transaction.atomic` in production except named infrastructure; forbid the event
  internals; structural rules that require the batch scopes.
- Rejects reordering alone (new targets need a pk before the mapping; receivers and COMMIT can still fail)
  and per-row `event_tracking` (early dispatch under `coordinated_import`'s enclosing transaction; 4.3 differs).
- CI: a `rollback-event-test` matrix on NetBox 4.3.0 and 4.7.0 that runs one dedicated module.

## 20. Merge

Post-blind addition (Claude, after A was recorded, before B was read): **C, per-unit `event_tracking`**. Each
top-level unit (one `_each_row` row with no transaction open, one DHCP family import) runs inside its own
`event_tracking(current_request)`. On 4.7 a unit whose exception leaves the scope dispatches nothing, and a
unit that succeeds dispatches after its COMMIT. That covers every failure kind, database errors included.
Entered only when no connection is in an atomic block. B's rejection of per-row tracking addresses the
savepoint case, which C excludes by that check.

| Decision | A | B | Evidence | Disposition | Consequence |
|---|---|---|---|---|---|
| Failure semantics | Phantom event for a reverted write when the DB or a receiver fails after the event write | No phantom; every pending event of the operation is lost when any row fails | Row failures are routine, not exceptional: `_each_row` catches `MetadataBusy` and `_RowRefused("... busy; retry ... on the next sync")` (`ipam_reconciliation.py:744,755`), and the IPAM job runs every 5 minutes over 10^3 to 10^5 rows. Under B, one busy row discards the webhooks of every committed row in that run, and a retry does not re-emit them (the rows are then unchanged). NetBox's own `discard_events_on_rollback` docstring rejects exactly this use. | **A + C**, open for round 1 | Test that differs: 1000 committed rows plus one busy row. B dispatches 0 events, A + C dispatches 1000 on 4.7. |
| Top-level rows (`_each_row` outside a transaction, the IPAM job and per-row views) | Reorder only; DB-error phantom accepted | Batch discard | C is exact on 4.7 for every failure kind at these rows, and they are the high-volume path. | **C** at top-level units | Cost: one flush per row, so about one EventRule query per event type per row; coalescing across rows is lost (two rows touching one MAC send two events). Measured bound required in round 1 or at implementation. |
| Savepoint rows inside `coordinated_import` | Reorder plugin refusals before the event write; DB errors, receivers and post-write delete guards accepted | Batch discard of the whole import | Inside an open transaction, no allowed surface can drop one savepoint's events without dropping the rest. | **A** | Accepted limit, documented. |
| Whole-family import failure (v4 commits, v6 raises, view catches) | `clear_events` in the view (loses v4's events) | Batch discard (loses v4's events) | `run_dhcp_plugin_import` (`views/dhcp_plugin_sync.py:145-157`) runs each family as a top-level transaction with no outer atomic. | **C**: each family import runs in its own unit scope; v4 dispatches at its exit, v6 dispatches nothing | `test_dhcp_mapping_recovery.py:3931` becomes: a nested import inside a caller transaction is an accepted limit. |
| Reservation MAC refusal (`ipam_reconciliation.py:624`) | Parse check before `_claim` | Covered by batch | `sync_mac_address` returns None for an unparseable MAC or a DB error (`sync.py:103-123`). | **A**: parse before writing; a DB-error `None` keeps refusing the row (C covers it at top level, a savepoint keeps the accepted limit) | Test fails on the old order. |
| `observe_mapping` after the target save | Accepted (only DB errors reach it inside the import) | Covered by batch | Inside `coordinated_import`, `metadata_scope` already holds the advisory lock, so the inner try-lock re-enters; the import refuses in a branch (`refuse_in_branch`), so `MappingUnavailable` cannot occur. | A | |
| Guard | Raise after a write inside a caught atomic; forbidden internals | Forbid raw `atomic` outside the module; forbidden internals; required batch scopes | B's raw-atomic ban forces every new block through one helper, but A + C has no helper for savepoint blocks to route through. | Forbidden internals (both). **Undispositioned**: whether to also require the unit scope at top-level catch sites, and how to detect "top-level" syntactically. | |
| 4.3 | Flush is unconditional: phantom | Same discard | Section 1. | Agreed; 4.3 leg in CI runs the tests with 4.3 expectations | |

### r7 (merged candidate)

1. Delete `event_queue.py`, `_events_follow_rollback`, `_event_copy`, the r6 rules and the r6 tests. All
   sites use `transaction.atomic`.
2. A small owner module `netbox_kea/event_scope.py`, interface `unit_events()`: when `current_request` is
   None, it does nothing (NetBox queues nothing). When any connection is in an atomic block, it does nothing
   (the enclosing transaction's owner decides; accepted limit). Otherwise it enters
   `event_tracking(current_request.get())`. It is entered outside the unit's `transaction.atomic()` and
   inside the site's `try`, so a failure leaves the scope before the site catches it.
3. Sites: `_each_row` wraps each row; `run_dhcp_plugin_import` wraps each family's `import_server_config`.
4. Reorder: the Reservation MAC parse moves before `_claim`. Every other plugin-raised check already precedes
   its first event write (audited: `_claim` :949, cleanup :1244 and :1447).
5. Accepted limits (documented in user docs): inside an import's transaction, a DB error, another app's
   receiver, or a post-write delete guard after an event write dispatches the reverted write's event; a
   caller transaction that rolls back after a unit returns; on 4.3, every unit flushes regardless of failure.
   Webhook change on 4.7: events dispatch per unit, not once per request, and are not coalesced across units.
6. Guards: opengrep forbids `events_queue`, `EventContext` and `enqueue_event` in `netbox_kea/` outside tests.
7. CI: a NetBox 4.3 unit-test leg runs the event-rollback test module.

## 21. r7 Round 1 (Codex gpt-6-astra, high, read-only): NOT RATIFIED r7

Executed: in-memory models with extracted NetBox 4.7 functions and Django's `Atomic`. Nested `event_tracking`
passed: entry makes a fresh queue and query cache; both exits restore queue, cache and request by token; inner
success dispatches at once, inner failure dispatches nothing, the outer event dispatches last. `copy_context`
in `_run_tracked` keeps the caller context. Per-row views open no transaction. Reading `current_request` does
not break section 17's list.

| # | Finding | Severity | Verified by Claude | State |
|---|---|---|---|---|
| R7.1 | "No connection in an atomic block" does not prove the unit commits: with autocommit off, Django's outermost `atomic()` makes a savepoint and does not commit. Alias set must include branch connections. | major | Yes: Django `transaction.py:194-215`; `metadata_scope` already checks both (`dhcp_mapping_lifecycle.py:116-119`); `branching.connection_aliases()` :94. | ACCEPTED, r8 (1) |
| R7.2 | MAC events outside C: `_run_phase` calls `sync_mac_address` after `_each_row` (`ipam_reconciliation.py:785`); its own caught atomic (`sync.py:108`) can create the MAC, fail the description save, roll back and return None. Inside a lease row the same rollback is swallowed and the row's unit succeeds. | blocker | Yes: `sync.py:103-123`, `ipam_reconciliation.py:616-626`. | ACCEPTED, r8 (2) |
| R7.3 | `unit_events` inside `_each_row`'s `try` reports a committed row as failed when the flush raises (`process_event_queue` queries; `flush_events` catches only `ImportError`). A raising `on_commit` hook drops the events of a committed unit. | major | Yes: `extras/events.py:257-290`. No plugin `on_commit` registration (grep). | ACCEPTED, r8 (3); on_commit stated as a limit |
| R7.4 | r7 (4) is false: `_RowRefused` :624 follows a DB-error `None`; `_resolve_mac` -> RuntimeError in `_upsert_reservation` (`dhcp_plugin.py:205,838`); delete guards are plugin code. | major | Yes. | ACCEPTED, r8 (4) narrows the claim |
| R7.5 | The guard row has no disposition; a new `save(); raise` passes the internals rule. | blocker | Yes. | ACCEPTED, r8 (5) |

Also accepted: the busy-refusal evidence in the merge table was wrong (`nowait` is only the cleanup path inside
an enclosing transaction, :1202). Correct evidence for A + C over B: `_claim` refuses "more than one IP
address" (:949) and `_one` raises `DuplicateNetBoxRowsError` (:121) on persistent NetBox data, at top-level
rows. Each recurs on every run until an operator fixes the data, so under B every run loses all its events.
Cost: one EventRule lookup per unit flush (`extras/events.py:257-270`), linear, SQL latency unmeasured.
Coalescing loss changes which rules fire (executed: one batch `created(active)`, per unit
`created(dhcp)` + `updated(active)`).

## 22. r8

Changes from r7, verbatim:

1. **One owner, one interface.** `netbox_kea/event_scope.py` exposes `atomic(using=None)`, a drop-in for
   `transaction.atomic` at every plugin site (B's seam, C's mechanism). At entry it decides once:
   - no `current_request`: plain `transaction.atomic` (NetBox queues nothing);
   - any alias in `branching.connection_aliases()` with an open connection that is in an atomic block or
     not in autocommit: plain `transaction.atomic` (a savepoint, or a caller-owned transaction; accepted
     limit). This test is extracted from `metadata_scope` (`dhcp_mapping_lifecycle.py:116-119`) into one
     helper that both use;
   - otherwise a **unit**: `event_tracking(current_request.get())` around `transaction.atomic`. The block's
     events dispatch after its COMMIT, or not at all when its exception leaves the block (4.7).
   This replaces r7's `unit_events()` and its per-site placement. A top-level transaction whose exception is
   caught far above it (the import view at `views/dhcp_plugin_sync.py:291-304`, the job's per-Server catch at
   `jobs.py:377`, `_complete_observation` in commit 92013163) is covered without naming it.
2. **One event write per MAC sync.** `sync_mac_address` makes at most one event-producing write per call: a
   new MAC is created with its description in one INSERT, and an existing MAC gets one `save()`. A database
   error on that write queues nothing, because `post_save` never runs. This closes R7.2 on every path, inside a
   transaction or not.
3. **Flush errors are not block failures.** Inside a unit, an exception that leaves `event_tracking` after the
   block completed comes from the flush. `atomic()` re-raises it as `EventDispatchError(RuntimeError)` from the
   original. No per-row handler catches that type (`_each_row` catches `DatabaseError, _RowRefused,
   DuplicateNetBoxRowsError, MetadataBusy`), so a committed row is never reported as failed; the run fails, as a
   failed flush fails a native request. Broad handlers (`jobs.py:377`, the import view) log it as an error.
   Limit: an `on_commit` hook that raises drops its unit's events (the plugin registers none).
4. **Ordering claim, narrowed.** Inside a transaction (where `atomic()` is a plain savepoint), no plugin
   refusal follows an event-producing write in a caught block, unless it reports a database failure that
   already happened in that block. Remaining post-write refusals there are accepted limits: `_RowRefused`
   :624 after a DB-error MAC, `_resolve_mac` -> RuntimeError :838, and the post-write delete guards
   (`_unaffected`, `_late_delete_guard`), whose job is to detect a native delete's effect after it happened.
   At units, 4.7 discards all three. Acceptance 2 in section 17 is narrowed to refusals that do not report a
   database failure. The Reservation MAC parse moves before `_claim` (unchanged from r7).
5. **Guards** (bounded: one pass plus at most two refinements against the selected fixtures):
   - `kea-no-event-queue-internals`: `events_queue`, `EventContext`, `enqueue_event` in `netbox_kea/`
     outside tests, aliased imports and attribute access included.
   - `kea-raw-atomic`: `transaction.atomic` (call, `with`, decorator, aliased import) in `netbox_kea/` outside
     `event_scope.py` and tests. Every new block then goes through the owner.
   - `kea-refusal-after-event-write`: inside `try: ... with $A(...): BODY ... except` where `$A` is the owner's
     `atomic`, a `raise` in BODY after `$X.save(...)`, `$X.delete(...)`, `$Q.create(...)`,
     `$Q.get_or_create(...)`, `$Q.update_or_create(...)`, `$M.set(...)`, `$M.add(...)`, `$M.remove(...)`,
     `$M.clear()`. Limit: writes and raises inside called helpers and callbacks (`claim().apply`,
     `observe_mapping`) are not seen.
6. **Stated dependencies.** r8 depends on two NetBox context surfaces, both exported and already used by the
   plugin: reading `current_request`, and entering `event_tracking` nested (a registered request processor,
   `docs/development/application-registry.md:42`). Nested semantics are verified on 4.7 by model (section
   21); the 4.3 CI leg verifies them on 4.3, where the flush is unconditional, so a unit only changes when its
   events dispatch.
7. **Accepted consequences, explicit.** On 4.7, events dispatch per unit, not once per request: an object
   touched by two units sends two events, which can change which event rules fire (example in section 21), and
   outer events dispatch after inner ones. One EventRule lookup per unit flush; implementation measures it
   over 10^4 rows and states the bound in a test.
8. **Unchanged from r7:** deletion of `event_queue.py`, `_events_follow_rollback`, `_event_copy`, the r6 rules
   and tests; the NetBox 4.3 unit-test leg in CI.

## 23. r8 Round 2 (Codex gpt-6-astra, high, read-only): NOT RATIFIED r8

Executed: models with Django 5.2.17's `Atomic` and `get_or_create` control flow and the extracted NetBox and
plugin functions. Inventory: 13 production `atomic()` sites. Unit on the inspected paths: `_complete_observation`
:408, `_each_row` :753 outside imports, `sync.py:108` after `_run_phase`'s row, `config_write.py:665`,
`_lock_boundary` :105 around a family import. Plain: the 8 DHCP-plugin sites inside the import, and every
lifecycle scope under native transactional writes, deletes, m2m signals and branch replay. One-write MAC sync
is feasible: build the defaults through `_update_mac_description` on an unsaved object, `get_or_create`, then
merge and save only an existing changed row (new, existing, unchanged and create-conflict paths executed).

| # | Finding | Severity | Verified by Claude | State |
|---|---|---|---|---|
| R7.1 | Unopened connection configured `AUTOCOMMIT=False` selects unit; Django then makes a savepoint (`base.py:255`, `transaction.py:194`). | (R8.3) major, conditional | Yes, as Django semantics; no such configuration known in deployment. | NOT-CLOSED -> r9 (1) |
| R7.2 | MAC sync rollback. | | | CLOSED (design) |
| R7.3 / R8.2 | `sync_mac_address` catches `Exception` (`sync.py:119`) and swallows `EventDispatchError` from its own unit after `_run_phase` (:785); the run does not fail. | major | Yes. | NOT-CLOSED -> r9 (3) |
| R7.4 / R8.1 | Duplicate MAC rows make `get_or_create` raise `MultipleObjectsReturned` (not a DB error); `apply` writes the IP, the MAC helper returns None, `_RowRefused` :624 follows the write; inside an import the reverted IP's event dispatches. Regression state exists: `test_dhcp_import_ownership.py:594`. | major | Yes. | NOT-CLOSED -> r9 (4) |
| R7.5 | Guard disposition. | | | CLOSED (design). Retarget `kea-mac-sync-write-without-savepoint` (`.opengrep/kea-rules.yaml:455`) to the owner's `atomic`. |
| R8.4 | EventRule lookups are per distinct `(event_type, object_type)` per flush (executed: 20,000 for 10,000 units of IP + MAC). On 4.3 splitting queues also changes coalescing. | minor | Yes: `extras/events.py:257-270`. | ACCEPTED -> r9 (6) |

Also: (a) native netbox-branching enters `event_tracking` inside its merge/revert transaction, so an earlier
change can dispatch before a later failure rolls everything back; r8 selects plain there. Not caused by r8;
it is listed as a limit. (b) An exception can leave `event_tracking` after the block for a reason other than
the flush (executed: a token reset across contexts raises `ValueError`); no inspected lexical path crosses a
context. The completion flag must be set after Django's atomic exits (executed: then body and COMMIT failures
are told apart from exit failures).

## 24. r9

Changes from r8, verbatim:

1. **Unit predicate.** An alias counts as transactional when its connection is open and in an atomic block or
   not in autocommit, **or** when it is unopened and its `settings_dict["AUTOCOMMIT"]` is false. Any
   transactional alias in `branching.connection_aliases()` selects plain. No connection is opened to decide.
2. **Exit errors.** `EventDispatchError(RuntimeError)` is defined as "`event_tracking` raised at exit after the
   block committed", normally the flush. The completion flag is set after `transaction.atomic` exits, so a
   body or COMMIT failure is never wrapped.
3. **No handler swallows it.** A broad handler that can enclose a unit re-raises `EventDispatchError` before
   its own recovery: `sync_mac_address` (`sync.py:119`) does. `jobs.py:377` keeps its per-Server catch: it logs
   the error and counts it in the job report, so the failure is visible. Guard: `kea-dispatch-error-swallowed`
   flags `except Exception`/`except BaseException`/bare `except` without a preceding `except
   EventDispatchError: raise` in a function that calls the owner's `atomic` in the `try` body, with an
   allow-list entry for `jobs.py:377` (it reports). Limit: handlers in callers of that function are not seen.
4. **Read-only MAC pre-check.** For a Reservation row, `apply` resolves its MACs read-only before `_claim`
   writes: parse each address, and refuse the row (`_RowRefused`) when it does not parse or when NetBox
   already has more than one MACAddress row for it. `sync_mac_address` runs after `_claim`, as today. The
   ordering invariant for caught savepoints becomes: no plugin refusal follows an event-producing write in the
   block, **except** (a) a refusal that reports a database failure in that block, (b) a refusal caused by a
   NetBox row that another writer changed after the pre-check (a duplicate MAC created concurrently), and (c)
   the post-write delete guards (`_unaffected`, `_late_delete_guard`), whose purpose is to detect a native
   delete's effect after it happened. All three are accepted limits inside a transaction and are discarded
   at units on 4.7.
5. **Accepted limits list (acceptance 3)** adds native netbox-branching merge/revert: its own nested
   `event_tracking` dispatches per change inside its transaction.
6. **Corrected consequences.** EventRule lookups are one per distinct `(event_type, object_type)` per unit
   flush (about two per IPAM row). On 4.3 the flush is unconditional, so units gain no discard guarantee, and
   splitting the queue still changes coalescing and which rules fire, as on 4.7.
7. Implementation also retargets `kea-mac-sync-write-without-savepoint` to the owner's `atomic`.

Unchanged from r8: (1) owner and modes, (2) one event write per MAC sync, (5) guards, (6) dependencies,
(8) deletions and the 4.3 CI leg.

## 25. r9 Round 3 (Codex gpt-6-astra, high, read-only): RATIFY r9

Verdict A (core: owner `atomic()` and modes, predicate, exit errors, guards, limits, deletions, 4.3 CI leg):
**RATIFY r9**. Verdict B (MAC mechanism: one event write per sync, `EventDispatchError` re-raise, read-only
pre-check, ordering exceptions (a)-(c)): **RATIFY r9**. Every prior finding CLOSED at design level; the
round-2 counterexamples re-executed against r9 (duplicate MAC during an import: only the healthy IP event
dispatches; standalone MAC flush failure: `EventDispatchError` escapes; unopened `AUTOCOMMIT=False` alias:
plain, no owner dispatch).

R9.1 (major if B is deferred): A does not stand alone. Without B's re-raise a MAC flush failure disappears,
and without the one-write MAC sync R7.2 returns through `claim().apply`. So r9 is ratified as one design,
not split, and ships as one increment.

Unverified by the reviewer, owed by implementation: PostgreSQL integration, real opengrep fixtures, NetBox
4.3 execution, SQL latency of per-unit flushes.

### First increment (all of r9) and its observable acceptance

1. `netbox_kea/event_queue.py`, `_events_follow_rollback`, `_event_copy`, the r6 rules and tests are gone;
   `grep` finds no `events_queue`, `EventContext` or `enqueue_event` in `netbox_kea/` outside tests.
2. `netbox_kea/event_scope.py` `atomic()` is the only `transaction.atomic` user in `netbox_kea/`; the
   transactional-alias predicate is one helper shared with `metadata_scope`.
3. Integration tests under `event_tracking` with the event recorder, each red before the change: a top-level
   `_each_row` row that fails after its IP write dispatches nothing for it while a sibling dispatches; a
   failed family import dispatches nothing for that family while the committed family dispatches; the
   completion-receipt case from 92013163; a persistent duplicate MAC inside an import dispatches only the
   healthy IP; a MAC description-save failure queues no MAC event; a flush failure raises
   `EventDispatchError` out of `_each_row` and out of `sync_mac_address`; an `AUTOCOMMIT=False` or open-
   transaction caller gets plain mode.
4. One test per accepted limit pins its observable effect (section 24 (4)-(5)), so a NetBox change that
   removes the limit turns it red.
5. opengrep: `kea-no-event-queue-internals`, `kea-raw-atomic`, `kea-refusal-after-event-write`,
   `kea-dispatch-error-swallowed`, and the retargeted `kea-mac-sync-write-without-savepoint`, each with
   `ruleid`/`ok` fixtures; the scan is clean.
6. A measured bound for 10^4 units (EventRule lookups and wall time) is stated in a test or benchmark.
7. CI runs the event-rollback test module on a NetBox 4.3 unit-test leg as well as 4.7.
8. User documentation lists the accepted limits and the per-unit dispatch consequences.

## 26. Implementation of the first increment (2026-10-06)

**Deviation: units need NetBox 4.7.** r8 (6) assumed that `event_tracking` nests on 4.3 and differs only in its
unconditional flush. It does not. On 4.3.0 through 4.6.6, `event_tracking` ends with `current_request.set(None)`
and `events_queue.set({})`, not a token reset, and has no `finally`. A nested unit therefore drops the caller's
queued events, and every later write in the same request or job records no ObjectChange and queues no event,
because `current_request` is None. Executed on NetBox 4.3.7 with the gate removed:
`test_a_unit_keeps_the_callers_request_queue_and_change_logging` fails with `None is not <WSGIRequest ...>`, and
the caller's event is lost. 4.7.0 is the first release that resets by token in a `finally`. So `atomic()` selects
a unit only when `settings.RELEASE` is 4.7 or later. On 4.3 to 4.6 every block is plain, and the accepted limits
of section 24 (4) apply to top-level blocks as well. This also loses, on those releases, the whole-family
discard that `_events_follow_rollback` gave the DHCP import before. The 4.3 CI leg runs `test_event_scope.py`
with these expectations; it passed locally on NetBox 4.3.7 (12 passed, 8 skipped).

**Measured bound (acceptance 6).** 10^4 units that each update one Site: 10^4 EventRule lookups and 151.0 s of
wall time. The same 10^4 saves in one transaction: 1 lookup and 127.2 s. `UnitCostTest` asserts one lookup per
unit flush in every run, and repeats the 10^4 measurement with `NETBOX_KEA_BENCHMARK=1`.

**Guards.** `kea-dispatch-error-swallowed` is lexical, so it cannot see the per-Server handler at `jobs.py:377`,
whose `try` calls `_sync_one_server`. No allow-list entry is needed; the README of the ruleset names that
handler as the reviewed one. The rule does not check the handler order, and it does not see a tuple that names
`Exception` or a `try` with `finally`.

**MAC pre-check.** The refusal for a hardware address that is not EUI-48 or EUI-64 now precedes `_claim`. A
Reservation row whose `_claim` outcome would be a conflict is now refused for such an address, where before it
was reported as a conflict and its MAC was not synchronized.
