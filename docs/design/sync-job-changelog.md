<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Change tracking for IPAM sync jobs

## Decision brief

The job owns its change-tracking context. Its worker receives no HTTP request.
The manual Sync now view owns selection of the initiating actor.

The supported NetBox releases are 4.3 to 4.7. The job must retain its main-only
branch refusal. Change tracking must not replay HTTP request processors or pass
browser request data through Redis. A failed run must retain change records for
committed writes and restore its previous context. Native event dispatch must
retain NetBox's success-only behavior.

Observable acceptance conditions:

- The real job runner records creates, updates and deletes of IPAM objects, and
  MAC address changes, without an outer request context.
- One fresh request ID groups all writes of one execution, across all servers.
- Manual runs record the initiating user. Scheduled runs have a defined actor.
- Unchanged IPAM objects generate no update records.
- Refused branch runs perform no reads or writes and create no synthetic actor.
- A later execution cannot inherit the previous execution's actor or event queue.

## Evidence

NetBox `JobRunner.handle()` does not apply request processors. The native
`event_tracking` context sets and restores the request, event queue and query
cache. It dispatches events only after successful exit. Core change signals
create `ObjectChange` rows while that context is active.

Although `ObjectChange.user` is nullable, `ObjectChange.save()` reads
`self.user.username` when the static user name is empty. A bare missing actor
cannot use this path. An anonymous user cannot be assigned to the user foreign
key. The scheduled actor needs an explicit design.

Native change tracking records each actual mutation. The existing legacy VRF
adoption path can save one IP address twice, once to move its VRF and once to
apply current lease facts. Both records preserve the intermediate state.

## Candidate shapes

- Use a dedicated noninteractive user for scheduled executions and native event
  tracking for all executions.
- Retain a nullable scheduled actor and provide its static attribution within a
  job-scoped change-record adapter.

The competing designs and ratification verdict will be recorded before code
implementation.

## Blind comparison and candidate r1

The coordinator and an independent GPT-6.1 Sol designer (high reasoning)
produced proposals from the same factual brief. Neither read the other's proposal
before completion. Their designs agreed on job ownership, a disabled reserved
user, worker-local request construction, native event tracking, and fail-fast
branch refusal.

| Decision | Coordinator | Independent designer | Disposition |
| --- | --- | --- | --- |
| Scheduled actor | Reserved noninteractive user | Reserved noninteractive user | Use native actor semantics. Do not alter scheduled Job.user. |
| Request shape | Django HttpRequest | Native NetBoxFakeRequest | Use HttpRequest. Its standard collections and methods exist without copying browser data. |
| Context cleanup across releases | Run native tracking in a copied context | Preserve individual context variable tokens | Use contextvars.copy_context().run(). It isolates all native context changes without enumerating release-specific variables. |
| Change count | Record each real mutation | Record each real mutation | Keep intermediate records from legacy adoption. Do not coalesce the changelog. |

### Candidate r1

Keep _fail_in_branch() first. Resolve the actor after refusal. For a manual run,
use Job.user. For a run without a user, lazily create or reuse netbox-kea-sync.
Create that user inactive, without staff or superuser status, with an unusable
password and no groups or permissions. Reject an existing account that fails
those conditions. Do not modify an existing account. A deleted manual actor
leaves Job.user null and uses the same documented system attribution.

The job builds an HttpRequest with a fixed plugin path, an empty browser
payload, the actor and a fresh UUID. Only the native serialized Job and existing sync
arguments cross the queue. The manual view adds user=request.user to enqueue.

Run the existing sync inside native event_tracking in a copied Python context.
Leave the branch context intact for the initial refusal. A copied context retains
that value and isolates native request, queue and cache changes from the caller.
Discarding it after return or exception also protects NetBox 4.3, whose tracking
context does not restore state on exceptions. This adds no whole-run transaction.
Native records for committed writes remain after failure. Native mutation events
are dispatched only after a successful run.

Each execution owns one fresh request ID across all Servers. An unchanged object
has no new update record. Each actual create, update or delete has its native
record. A row with two legitimate mutations has two records in the same group.

### Section 0: refuted claims

None. Candidate r1 awaits independent adversarial ratification.

## Ratification round 1 and candidate r2

Astra high, in a read-only sandbox, returned NEEDS-WORK for r1. It confirmed
context isolation with an executed proof using the official NetBox 4.3 tracking
implementation. It found one actor lifecycle blocker: Redis serializes a Job
instance, so deleting its user does not update the queued instance. JobRunner
saves that stale user foreign key before run() starts.

The implementer's preparation also confirmed a delete retry defect in native
4.7 tracking. A per-row database error after pre_delete rolls back the deletion
and change record, but leaves the signature in NetBox's thread-local history.
A later job can delete that same row without its delete record. A copied context
isolates ContextVars, not that history. NetBox 4.3 has no such history.

### Changes from r1

- Refresh the queued Job's user field from the database before delegating to
  JobRunner.handle(). This clears any serialized user relation cache. Native Job
  lifecycle bookkeeping still runs normally. Branch refusal remains first before
  sync reads, actor provisioning and IPAM writes, as in the existing job.
- Reset native delete history before tracking and in finally on releases that
  provide core.signals.clear_signal_history(). This narrow native hook avoids
  broad request_finished teardown and unrelated receivers. Absence on NetBox
  4.3 means there is no suppression state to reset. Do not introduce a new
  worker thread, alter native receivers, or close connections.
- Validate NetBox object_permissions as well as inherited user_permissions and
  groups on reserved account reuse.

### Additional acceptance predicates

Enqueue through the real Sync now view and serialize through real Redis. Delete
the initiating user before executing the queued callable. The run must use
system attribution and must not restore the deleted foreign key.

Seed a stale owned IP address. A PostgreSQL delete trigger rejects its first
cleanup. That failed run retains the row and no committed delete record. Remove
the trigger and run the actual job again. The successful retry must remove the
row and write its native delete record.

### Section 0: dispositions

- The possible context-isolation defect is REFUTED by the executed 4.3 proof.
  Reopen if execution becomes asynchronous or native tracking mutates an
  inherited queue before replacing it.
- Stale serialized actor: NOT-CLOSED pending ratification of r2.
- Delete history across retries: NOT-CLOSED pending ratification of r2.

Candidate r2 retains the rest of r1 unchanged.

## Ratified scope and first increment

Astra high, read-only, returned RATIFY r2 for actor attribution, native changelog,
main-only refusal, context isolation and delete bookkeeping. Its executed proofs
confirmed relation-cache refresh, copied-context cleanup for the supplied 4.3
and 4.7 implementations, and the native delete-history reset. Actual PostgreSQL
and Redis behavior remains an implementation gate.

The stale serialized actor and delete retry findings are CLOSED by the ratified
design. The first implementable increment runs one real scheduled Job through
JobRunner.handle and records a created IP address with system attribution.

Implementation uses the native NetBox user fields. NetBox 4.7 has no separate staff
field, so do not pass is_staff to its constructor. NetBox 4.3 has the flag
and defaults it to false. An inactive non-superuser with
an unusable password and no groups or permissions has no login or access grant.
If the configured user model exposes a staff flag, reject that privilege on reuse.
Respect native case-insensitive username uniqueness when checking the reserved
name. Reject an incompatible account with any casing instead of creating a second
account. No incompatible account is modified.

Next actions: implement vertical red/green slices at the ticket's real JobRunner
and Sync now POST seams; validate real Redis serialization and PostgreSQL rollback;
review the final diff independently against standards and the supplemented issue.


## Supported-floor implementation evidence

The isolated NetBox 4.3.7 setup passed 87 native model tests. Running the new job
tests then exposed an existing logging API assumption: its JobRunner constructor
sets only job, whereas 4.7 also initializes logger. Nine job scenarios failed with
a missing logger before synchronization completed.

The job initializes its existing class logger after the native constructor.
It uses the same logger name and level as NetBox 4.7, so that release retains
its native JobLogHandler. NetBox 4.3 uses normal system logging and the existing
per-server summary. Do not invent persistent log storage absent from that release.

The test harness also assumed a query-count helper introduced after the supported
floor. It now patches that helper only where present. The binding check still
runs on the release that owns the recorded baseline. The real missing-helper
setup errors and binding assertion were observed before these changes.
