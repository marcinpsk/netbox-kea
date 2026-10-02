<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# IPAM Ownership links under netbox-branching

Status: ratified r3 on 2026-09-30 (design-blind, three adversarial rounds). Ticket #208, ADR 0006, ADR 0007.

## Brief

ADR 0006 adds the IPAM Ownership link: `(server, family, source, object)`, where the object is exactly
one of three nullable foreign keys to `ipam.IPAddress`, `ipam.Prefix` and `ipam.IPRange`, each with
`on_delete=CASCADE`. ADR 0007 keeps every netbox_kea row in main and refuses every plugin write in a
branch. Guard 2 (`BranchabilityPinTest` in `netbox_kea/tests/test_branching.py`) computes the plugin
models that a delete in a branch writes and pins that set as empty. The link's `CASCADE` keys put it in
that set, so the guard fails, and the design must say how the link behaves in a branch.

Constraints:

- ADR 0007 stands: the sync job and every plugin write run in main only.
- ADR 0006 stands: a deleted IPAddress, Prefix or IP Range leaves no dangling link.
- A delete, edit or discard in a branch does not change main's rows before a merge.
- Prefer a mechanical guard over documentation.

Acceptance conditions (observable, in a provisioned branch, CI `branching-test` job):

1. A delete of a linked IPAddress, Prefix or IP Range in a branch leaves main's link rows and main's
   object unchanged.
2. A merge that replays such a delete removes the object and its links in main.
3. Guard 2 names the link and fails with a design-decision message when the chosen behaviour is
   removed or a new model with the same exposure appears.

## Candidates

- **A. Main-only, with refusal.** The resolver keeps the link in main (as today for every plugin
  model). The existing `pre_delete` receiver refuses the cascaded link delete in a branch, so the core
  delete is refused.
- **B. Branchable.** The resolver lets NBB copy the link table into each branch.
- **C. Main-only, database cascade.** `on_delete=DB_CASCADE` (Django 6.0+), so the ORM does not collect
  links and the `ON DELETE CASCADE` constraint lives on main's table only.
- **C'. Main-only, `DO_NOTHING` plus a hand-written `ON DELETE CASCADE` constraint.**
- **D. `PROTECT`.**

## Evidence

- NBB `BranchAwareRouter._get_db` returns `None` (default) for a model that does not support branching
  (`netbox_branching/database.py:24-37`). The branch connection's `search_path` is
  `<branch schema>,<main schema>` (`utilities.py:108`), so a query for a non-replicated table on the
  branch connection reads main's table.
- `get_tables_to_replicate` copies only branchable models' tables and their M2M tables
  (`utilities.py:293-317`).
- `merge` and `revert` run without `activate_branch` and apply on the default connection
  (`models/branches.py:1100-1240`, `models/changes.py:93-180`). `sync` activates the branch
  (`models/branches.py:941`) and applies main's changes on the branch connection (`:743-759`).
- Django 6.1 `_check_on_delete` (`django/db/models/fields/related.py:1123-1158`) raises `fields.E323`
  when a database-level `on_delete` points at a model whose related fields use a Python-level
  `on_delete`. `ipam.IPAddress` has Python-level foreign keys (`vrf`, `tenant`, `nat_inside`), so
  `DB_CASCADE` on the link fails the system check. A plugin cannot silence a check for the operator.
- Django collects a `CASCADE` relation with `pre_delete` receivers connected (no fast delete), and
  sends `pre_delete` for each collected row inside `Collector.delete()`'s atomic block, before any
  delete (`django/db/models/deletion.py`).
- `DB_CASCADE` fails the check in practice: a probe with `Ip.vrf = ForeignKey(PROTECT)` and
  `Link.ip = ForeignKey(Ip, on_delete=DB_CASCADE)` gives `fields.E323` under Django 6.1.0 (executed
  2026-09-30 in the branching test image).
- NetBox's `Interface`, `VMInterface` and `FHRPGroup` have `ip_addresses = GenericRelation(IPAddress)`
  (`dcim/models/device_components.py:1149`, `virtualization/models/virtualmachines.py:475`,
  `ipam/models/fhrp.py:45`), so a delete of a Device, VM, interface or FHRP group collects the assigned
  IP addresses and their links.

## Blind designs (step 2)

- Designer 1: Claude (Opus 5.5), this session, from the brief above.
- Designer 2: Codex `gpt-6-astra`, effort high, `-s read-only`, fresh context, given only the brief,
  the evidence pointers and the NBB 1.2.1 and Django 6.1 sources. It did not see candidate A.
- Designer 1 chose **A**. Designer 2 chose **D** (`PROTECT` on the three IPAM keys, main-only), and
  proposed to change ADR 0006's `CASCADE` clause and the #208 criterion "Deleting a link's object deletes
  the link".

## Divergence table (step 3)

| Decision | Designer 1 | Designer 2 | Evidence | Disposition | Consequence |
|---|---|---|---|---|---|
| `on_delete` of the IPAM keys | `CASCADE` (ADR 0006) | `PROTECT` | GenericRelation cascade from Device/VM/interface/FHRP group; ADR 0006 "Ownership" | **A.** D refuses, in main, every delete of an owned object and of any Device, VM, interface or FHRP group that holds an owned IP address. That changes ADR 0006 and main's behaviour for a branching-only concern. | The call that differs: `IPAddress.delete()` of a linked row in main. A deletes it and its links; D raises `ProtectedError`. |
| Late ownership at merge: a branch deletes an unowned object, main links it later, merge replays the delete | Accepted: the merge deletes the object and, by `CASCADE`, its links | Refused by `PROTECT`, merge fails | NBB `merge` applies the delete on default (`changes.py:147-154`) | **A, to be attacked in round 1.** The replayed delete is an operator delete in main, which ADR 0006 permits. If Kea still reports the object, the next run creates it again with a new ID. | Test: merge of a branch delete of a row that main linked after the branch delete removes the row and its links, and the branch reaches `MERGED`. |
| Late ownership at revert: a branch creates an object, main claims it, revert of the merge deletes it | Accepted, as above | Refused | NBB `undo` of a create deletes (`changes.py:169-176`) | **A**, same reason. | Same shape of test. |
| Guard 2 | Pinned set becomes {link}, and each model in the set must carry the `pre_delete` refusal, proven in a real branch | Set stays empty because `PROTECT` does not write | `test_branching.py:75-139` | Follows the `on_delete` decision: **A**. | Guard 2 keeps its computation, pins {link}, and gains a behavioural test in a provisioned branch. |
| Model-contract guard (on_delete of each key, exactly-one constraint, `server` `CASCADE`) | Not proposed | Proposed | none needed | **Adopted** for A with `CASCADE`: guard 2 alone does not fail when a key moves to `SET_NULL`, which also writes. | A test pins each key's `on_delete`. |
| Readers in a branch see main's ownership | Implicit | Explicit | NBB router (`database.py:24-37`) | **Adopted**: a branch reader (the IP panel) labels ownership as main's. | Documented in the design record; no new code in #208. |
| Lifecycle tests (no link table in the branch, sync, discard, a successful unowned delete + merge + revert) | Not listed | Listed | none | **Adopted** as acceptance conditions. | See Decision. |

## Decision r1 (candidate A)

1. The link model is a plain Django model in `netbox_kea`. Its three IPAM keys are nullable
   `ForeignKey(..., on_delete=CASCADE)`; `server` is `CASCADE`. Exactly-one check constraint, unique per
   `(server, family, source, object)`.
2. The resolver keeps it main-only (`is_branchable` already returns False for every netbox_kea model). NBB
   does not copy its table into a branch. `connect_branch_refusal()` already connects the `pre_save` and
   `pre_delete` refusal to every netbox_kea model, so a delete in a branch of a linked IPAddress, Prefix or
   IP Range collects the link rows through the branch connection's `search_path` (main's table), the
   `pre_delete` receiver raises `BranchActive` inside `Collector.delete()`'s atomic block before any write,
   and `BranchRefusalMiddleware.process_exception` renders the 409 for any view (core views too).
   Consequence: in a branch, an operator cannot delete an owned object, or a Device/VM/interface/FHRP group
   that holds an owned IP address.
3. Merge and revert run on main without an active branch. A replayed delete of an object that main linked
   after the branch deleted it is an operator delete in main: it removes the object and, by `CASCADE`, its
   links. Same for a revert that undoes a branch-created object that main claimed later.
4. Guard 2: `BRANCHABLE_MODELS` is renamed to the set of plugin models that a delete in a branch reaches
   (writes), pinned as `{link}`. Each model in that set must have the `pre_delete` refusal receiver
   connected (asserted). A behavioural test in a provisioned branch proves the refusal. The resolver pin
   (`supports_branching(model) is False` for every plugin model) stays.
5. Model-contract pin: each IPAM key is `CASCADE`, nullable, `db_constraint=True`; `server` is `CASCADE`;
   the exactly-one constraint exists.
6. Migrations: the link table and the confirmation sequence migration set `fake_on_branch = True`
   (guard 4). Readers in a branch see main's ownership.

Acceptance conditions (CI branching job, provisioned branch):
- a1 Delete in a branch of a linked IPAddress, Prefix, IP Range (single and queryset/bulk, UI and REST):
  409, and main's object and links unchanged; the branch object is unchanged.
- a2 Delete in a branch of a Device whose interface holds a linked IP: refused, nothing changed.
- a3 The branch schema has no link table.
- a4 Sync from main after main deleted a linked object succeeds, and the delete reaches the branch.
- a5 Discard leaves main's objects and links unchanged.
- a6 An unowned object deleted in a branch, merged, then reverted: the object returns.
- a7 Late ownership: branch deletes an unowned object, main links it, merge succeeds and removes the
  object and its links in main.
- a8 Guard 2 pins {link}, fails with the design-decision message on a new exposed model, and fails when
  the link's refusal receiver is disconnected.

## Section 0: refuted or out-of-scope claims

| # | Claim | Evidence | Reopen when |
|---|---|---|---|
| R1-1 | A background REST bulk delete (`DELETE /api/ipam/ip-addresses/?background=true` with `X-NetBox-Branch`) deletes main's object and, by `CASCADE`, its links, so candidate A fails | **Mechanism confirmed, blocker refuted for this design.** NetBox 4.7.0 `AsyncAPIJob._build_request` (`netbox/jobs.py:291-325`) carries only host metadata, so NBB's `get_active_branch` (`utilities.py:538-551`) finds no branch in the worker, and the job writes main for every model. The same request deletes an unowned IP address in main too. A, B and D all lose the branch the same way; the link cascade is ADR 0006's result of a main delete. It is an upstream NetBox/NBB defect: recorded as a known limit, and an upstream report is drafted for the operator. | NetBox or NBB carries the branch into `AsyncAPIJob`, or the plugin takes on refusing core background writes |
| R1-2a | A refused bulk delete in a branch leaves `ChangeDiff` rows on the default connection | **Mechanism confirmed, not specific to this design.** NBB writes `ChangeDiff` on default (`signal_receivers.py:160-213`), outside the branch atomic block of the bulk view (`bulk_views.py:1235`). A `ProtectedError` on the second object of a bulk delete leaves the same residue. Known limit, upstream. | The residue appears only with the plugin's refusal |
| R2-5 | A synchronous REST bulk delete in a branch with one refused row rolls back the wrong connection | **Mechanism confirmed, upstream.** NetBox 4.7.0 `perform_bulk_destroy` (`netbox/api/viewsets/mixins.py:742-786`) opens `atomic(using=<branch>)`, catches `AbortRequest`/`ProtectedError` per row, then calls `transaction.set_rollback(True)` without `using`, which targets default. In a request (no default atomic) that raises `TransactionManagementError` inside the branch block, so the branch rolls back and the client gets a 500. A `ProtectedError` row does the same. Main's rows are not written in either case: the refused row's link receiver fires before any write, and the other rows are branch rows. Known limit, upstream report drafted. | The outcome writes main, or NetBox fixes the call and a partial branch commit remains |

## Round 1 (r1 -> r2)

Reviewer: Codex `gpt-6-astra`, effort high, read-only. Verdict: NOT RATIFIED r1, blockers 1, 2, 3.
All CASCADE dispositions (rows 1-3, 5, 6 of the divergence table) agreed.

- Finding 1: see R1-1.
- Finding 2, events and error rendering: **accepted.** `BranchActive` is not one of the exceptions
  that NetBox's delete views catch, so a refused UI bulk delete skips `clear_events` and dispatches
  the event of an object deleted earlier in the rolled-back loop (`bulk_views.py:1264-1280`). NetBox
  documents `AbortRequest` as the way a receiver aborts a request (`utilities/exceptions.py:21-26`), and
  the single-object delete view (`object_views.py:505-509`), the bulk delete view and REST
  (`api/viewsets/__init__.py:249-251`) all catch it and clear events. The `ChangeDiff` part: R1-2a.
- Finding 2, `QuerySet.delete()` ordering: accepted as a limit. It only moves the refusal after the
  IP address's own `pre_delete` receivers, so it adds `ChangeDiff` residue (R1-2a); the refusal
  still fires before any write on the branch connection commits.
- Finding 3: **accepted.** Guard 2 reduces exposure to model labels, so a new `SET_NULL` key to a
  branchable model on the pinned link passes, and `SET_NULL` writes through an UPDATE with no
  `pre_delete`. Guard 2 must pin relations, not models.

## Decision r2

Changes from r1, verbatim:

1. `BranchActive` subclasses NetBox's `AbortRequest` and sets `message`. Core views then roll back,
   clear queued events and show the refusal (UI message, REST 400). `BranchRefusalMiddleware`'s
   `process_exception` keeps rendering the 409 for an uncaught `BranchActive`, as today for plugin
   views.
2. Guard 2 pins **relations**, not models: the set of `(plugin model.field, on_delete)` that a delete in
   a branch reaches, computed transitively as today, pinned as the three IPAM keys of the link with
   `CASCADE`. Every exposed relation must be `CASCADE` (a delete, so the `pre_delete` refusal fires);
   any other writing handler (`SET_NULL`, `SET_DEFAULT`, `SET(...)`, `DB_*`) on an exposed relation fails,
   because it writes main's row with no signal. This replaces the separate model-contract pin for the
   IPAM keys. Each model on an exposed path must have the refusal receiver connected (asserted).
3. Acceptance a1 names synchronous UI, bulk UI and synchronous REST only. The background REST path is
   R1-1.
4. New acceptance conditions: a9, a mixed UI bulk delete (unowned object first, owned second) in a
   branch: refused, both objects remain in the branch, main unchanged, and no delete event is queued
   for dispatch. a10, late-ownership revert: a branch creates an object, merge, main links it, revert
   deletes the object and its links and reaches `READY`.

Unchanged from r1: points 1-3, 5 (now folded into guard 2), 6 of Decision r1, acceptance a2-a8.

## Round 2 (r2 -> r3)

Verdict: NOT RATIFIED r2, blockers 4, 5, 6. Finding 1: REFUTED as a link-design blocker (reviewer
agreed with R1-1). Finding 3: CLOSED. Finding 2: UI events fix agreed, R1-2a agreed, not closed
because of 4 and 5. `BranchActive(AbortRequest)` adds no new catch site in the plugin (the 36 broad
handlers already caught it), and an uncaught one still reaches `process_exception`.

- Finding 4: **accepted.** NetBox renders `AbortRequest.message` through `mark_safe`
  (`object_views.py:508`, `bulk_views.py:1280`), and the text holds `Branch.__str__`, the free-text
  branch name (`NBB models/branches.py:275-276`). A branch named with HTML becomes markup.
- Finding 5: mechanism confirmed in the image; see R2-5. Main is not written, so the design
  constraint holds; the path is removed from a1.
- Finding 6: **accepted.** `test_branching.py` enumerates `get_models()` without auto-created
  models, and `Collector.delete()` sends no signals for an auto-created through model
  (`deletion.py:492`). A plugin `ManyToManyField` to a branchable model is a delete path that no
  receiver can refuse.

## Decision r3

Changes from r2, verbatim:

1. `BranchActive.message` is the refusal text with every dynamic value escaped
   (`django.utils.html.escape`), because NetBox marks `AbortRequest.message` safe. The 409 template
   keeps autoescaping. New acceptance a11: a branch named `<b>probe</b>`; a refused single and bulk UI
   delete show the name as text, and the response has no `<b>` element from it.
2. Guard 2 enumerates plugin models with `include_auto_created=True`. An exposed relation passes
   only when it is a concrete `ForeignKey` with `CASCADE` on a model that is not auto-created and has
   the refusal receiver connected. A plugin M2M to a branchable model (its auto-created through
   model) fails the guard.
3. a1 covers synchronous UI (single and bulk) and synchronous REST single delete. REST bulk delete
   is R2-5: its acceptance is that main's objects and links are unchanged, whatever the status code.

Unchanged: Decision r1 as amended by r2 otherwise.

## Round 3 verdict

Verdict A: RATIFY r3 core (main-only link, `CASCADE` keys, main-side semantics, merge and revert).
Verdict B: RATIFY r3 mechanism (`BranchActive` as `AbortRequest`, the escape contract, guard 2 by
relation). No new findings. Findings 2, 3, 4 and 6 CLOSED; 1 and 5 REFUTED as design blockers
(Section 0). Scope: Decision r1 as amended by r2 and r3, and the Section 0 exclusions.

## Ratified decision (r3)

- The link is a plain `netbox_kea` model, main-only through the resolver. Its three IPAM keys are
  nullable concrete `ForeignKey(..., on_delete=CASCADE)`; `server` is `CASCADE`.
- In a branch, a delete that collects a link row (a linked IP address, Prefix or IP Range, or a Device,
  VM, interface or FHRP group holding a linked IP address) is refused by the `pre_delete` receiver
  before any write. Merge and revert run on main, where `CASCADE` removes the links.
- `BranchActive` subclasses NetBox's `AbortRequest`, and its `message` escapes every dynamic value.
- Guard 2 pins relations `(model.field, on_delete)`, includes auto-created models, and accepts only
  concrete `CASCADE` keys on models with the refusal receiver.
- Known limits, upstream: R1-1 (background REST jobs lose the branch), R1-2a (`ChangeDiff` residue),
  R2-5 (REST bulk delete rolls back default).

## Acceptance (implementation, CI branching job)

a1 synchronous UI (single and bulk) and synchronous REST single delete of a linked IP address,
Prefix and IP Range in a branch: refused, main's object and links unchanged, branch object unchanged;
also `QuerySet.delete()` under `activate_branch`. a2 a Device whose interface holds a linked IP address.
a3 the branch schema has no link table. a4 sync after main deleted a linked object. a5 discard.
a6 unowned delete, merge, revert. a7 late ownership at merge: `MERGED`, object and links gone in main.
a8 guard 2 fails for an added `SET_NULL` relation, an auto-created M2M and a disconnected receiver.
a9 mixed UI bulk delete (unowned first): refused, both rows remain in the branch, no delete event
dispatched. a10 late ownership at revert: `READY`, object and links gone in main. a11 a branch named
`<b>probe</b>`: refused single and bulk UI deletes, followed redirects show the name as text. REST
bulk delete (R2-5): main's objects and links unchanged whatever the status, with and without an
enclosing default atomic block.

## Next

Implement #208 with this decision on `feat/ipam-ownership`. The first increment is the link model,
its migration and this refusal and guard work, with a1-a11 in the branching job.
